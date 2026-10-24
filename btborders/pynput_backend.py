"""Real input capture/injection using pynput.

Platform notes
  Windows  low-level hooks (WH_MOUSE_LL / WH_KEYBOARD_LL). pynput checks `listener.suppress`
           on every event, so we can flip suppression on and off at runtime. We opt in to
           per-monitor DPI awareness so coordinates are physical pixels on both ends.
  macOS    same suppress mechanism (event taps); needs Accessibility permission. Best effort.
  Linux    X11 via XRecord/XTest. pynput only applies `suppress=True` when a listener *starts*
           (it grabs the pointer/keyboard), so to toggle suppression at runtime we start and
           stop a dedicated pair of grabbing listeners. Wayland does not allow global capture
           or injection from ordinary clients - see README for the evdev/uinput route.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys

from pynput import keyboard, mouse

from .core.edge import Bounds
from .core.protocol import Button
from .input_backend import CaptureHandlers, InputSink, InputSource

log = logging.getLogger("btb.input")

_BUTTONS = {mouse.Button.left: Button.LEFT, mouse.Button.right: Button.RIGHT,
            mouse.Button.middle: Button.MIDDLE}
for _n, _b in (("x1", Button.X1), ("x2", Button.X2)):
    if hasattr(mouse.Button, _n):  # only exists on Windows
        _BUTTONS[getattr(mouse.Button, _n)] = _b
_BUTTONS_REV = {v: k for k, v in _BUTTONS.items()}


# ------------------------------------------------------------------ platform helpers
def prepare_process() -> None:
    """Call once at startup. Windows: DPI awareness + 1 ms timer resolution."""
    if sys.platform != "win32":
        return
    try:  # 2 = PROCESS_PER_MONITOR_DPI_AWARE: coordinates are real pixels, not scaled
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            log.warning("could not set DPI awareness; cursor mapping may be off on scaled displays")
    try:  # Default Windows timer tick is ~15 ms, too coarse for 125 Hz flushing
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


def screen_bounds() -> Bounds:
    """Primary-monitor size in pixels (v1 limitation: multi-monitor edges use the primary)."""
    if sys.platform == "win32":
        u = ctypes.windll.user32
        return Bounds(u.GetSystemMetrics(0), u.GetSystemMetrics(1))
    if sys.platform == "darwin":
        import Quartz
        r = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        return Bounds(int(r.size.width), int(r.size.height))
    from Xlib import display
    s = display.Display().screen()
    return Bounds(s.width_in_pixels, s.height_in_pixels)


def session_warning() -> str | None:
    """Explain up-front when global input capture cannot work."""
    if sys.platform.startswith("linux") and (
            os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
            or os.environ.get("WAYLAND_DISPLAY")):
        return ("Wayland session detected: global mouse/keyboard capture and injection are "
                "blocked for normal apps. Log in with an 'X11' / 'Xorg' session, or see the "
                "README section on the evdev/uinput fallback.")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return "No DISPLAY set - btb needs an X11 session."
    return None


# ------------------------------------------------------------------ key <-> name
def key_to_name(key) -> tuple[str, int | None]:
    """pynput key -> (layout-independent name, raw vk). Special keys use their enum name."""
    if isinstance(key, keyboard.Key):
        return f"key:{key.name}", getattr(key.value, "vk", None)
    vk = getattr(key, "vk", None)
    ch = getattr(key, "char", None)
    if ch and len(ch) == 1 and ch.isprintable():
        return f"char:{ch}", vk
    # With Ctrl held, Windows reports control characters ('\x11' for Ctrl+Q). Recover the
    # letter/digit from the virtual-key code so hotkeys like Ctrl+Alt+Q still match.
    if sys.platform == "win32" and vk is not None and (0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A):
        return f"char:{chr(vk).lower()}", vk
    return (f"vk:{vk}" if vk is not None else "vk:0"), vk


def name_to_key(name: str, vk: int | None, use_vk: bool):
    kind, _, val = name.partition(":")
    if kind == "key":
        k = getattr(keyboard.Key, val, None)
        if k is not None:
            return k  # special keys by name work across operating systems
    if use_vk and vk is not None:
        return keyboard.KeyCode.from_vk(vk)  # same OS: exact key, shortcuts work
    if kind == "char" and val:
        return keyboard.KeyCode.from_char(val)
    if kind == "vk" and val.isdigit():
        return keyboard.KeyCode.from_vk(int(val))
    return None


# ------------------------------------------------------------------------- source
class PynputSource(InputSource):
    def __init__(self) -> None:
        self._mouse_l: mouse.Listener | None = None
        self._kb_l: keyboard.Listener | None = None
        self._grabbers: list = []              # X11 only
        self._mouse_ctl = mouse.Controller()
        self._kb_ctl = keyboard.Controller()
        self._held_keys: set = set()
        self._held_buttons: set = set()
        self._snap_keys: set = set()
        self._snap_buttons: set = set()
        self._bounds = screen_bounds()

    def bounds(self) -> Bounds:
        return self._bounds

    def start(self, h: CaptureHandlers) -> None:
        def on_move(x, y, injected=False):
            if not injected:
                h.on_move(int(x), int(y))

        def on_click(x, y, button, pressed, injected=False):
            if injected or button not in _BUTTONS:
                return
            (self._held_buttons.add if pressed else self._held_buttons.discard)(button)
            h.on_button(_BUTTONS[button], pressed)

        def on_scroll(x, y, dx, dy, injected=False):
            if not injected:
                h.on_scroll(int(dx), int(dy))

        def on_press(key, injected=False):
            if injected:
                return
            self._held_keys.add(key)
            name, vk = key_to_name(key)
            h.on_key(name, True, vk)

        def on_release(key, injected=False):
            if injected:
                return
            self._held_keys.discard(key)
            name, vk = key_to_name(key)
            h.on_key(name, False, vk)

        self._mouse_l = mouse.Listener(on_move=on_move, on_click=on_click, on_scroll=on_scroll)
        self._kb_l = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._mouse_l.start()
        self._kb_l.start()
        self._mouse_l.wait()
        self._kb_l.wait()

    def stop(self) -> None:
        self.set_suppress(False)
        for lst in (self._mouse_l, self._kb_l):
            if lst is not None:
                lst.stop()

    def set_suppress(self, on: bool) -> None:
        if on:
            # Remember what is physically held *now*: their key-ups will be swallowed while
            # suppressed, so we must release them locally afterwards or they stay stuck down.
            self._snap_keys = set(self._held_keys)
            self._snap_buttons = set(self._held_buttons)
        if sys.platform.startswith("linux"):
            self._set_suppress_x11(on)
        else:
            for lst in (self._mouse_l, self._kb_l):
                if lst is not None:
                    lst._suppress = on  # read per-event by the win32 and darwin listeners
        if not on:
            for k in self._snap_keys:
                try:
                    self._kb_ctl.release(k)
                except Exception:
                    pass
            for b in self._snap_buttons:
                try:
                    self._mouse_ctl.release(b)
                except Exception:
                    pass
            self._snap_keys.clear()
            self._snap_buttons.clear()

    def _set_suppress_x11(self, on: bool) -> None:
        if on and not self._grabbers:
            self._grabbers = [mouse.Listener(suppress=True), keyboard.Listener(suppress=True)]
            for g in self._grabbers:
                g.start()
            for g in self._grabbers:
                g.wait()
        elif not on and self._grabbers:
            for g in self._grabbers:
                g.stop()
            self._grabbers = []

    def warp(self, x: int, y: int) -> None:
        self._mouse_ctl.position = (x, y)


# -------------------------------------------------------------------------- sink
class PynputSink(InputSink):
    def __init__(self) -> None:
        self._mouse = mouse.Controller()
        self._kb = keyboard.Controller()
        self._bounds = screen_bounds()

    def bounds(self) -> Bounds:
        return self._bounds

    def move_to(self, x: int, y: int) -> None:
        self._mouse.position = (x, y)

    def button(self, button: int, down: bool) -> None:
        b = _BUTTONS_REV.get(Button(button))
        if b is not None:
            (self._mouse.press if down else self._mouse.release)(b)

    def scroll(self, dx: int, dy: int) -> None:
        self._mouse.scroll(dx, dy)

    def key(self, name: str, down: bool, vk: int | None, use_vk: bool) -> None:
        k = name_to_key(name, vk, use_vk)
        if k is None:
            log.debug("cannot map key %r", name)
            return
        try:
            (self._kb.press if down else self._kb.release)(k)
        except Exception as exc:  # e.g. a character this layout cannot type
            log.debug("inject %r failed: %s", name, exc)
