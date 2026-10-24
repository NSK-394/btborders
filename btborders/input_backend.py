"""Input abstraction: what the engine needs from the OS, with a mock for tests.

  InputSource  - capture side (sender): hooks, suppression, cursor warp
  InputSink    - injection side (receiver): move/click/scroll/key + release-all

The engine (server.py / client.py) only talks to these interfaces, so the integration test
runs the full protocol with MockSource/MockSink and no real devices.
"""

from __future__ import annotations

import abc
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from .core.edge import Bounds

PLATFORM_ID = 0 if sys.platform == "win32" else (2 if sys.platform == "darwin" else 1)


@dataclass
class CaptureHandlers:
    on_move: Callable[[int, int], None]                 # absolute cursor position
    on_button: Callable[[int, bool], None]              # protocol.Button value, down?
    on_scroll: Callable[[int, int], None]
    on_key: Callable[[str, bool, int | None], None]     # layout-independent name, down?, vk


class InputSource(abc.ABC):
    @abc.abstractmethod
    def start(self, handlers: CaptureHandlers) -> None: ...

    @abc.abstractmethod
    def stop(self) -> None: ...

    @abc.abstractmethod
    def set_suppress(self, on: bool) -> None:
        """While on, local apps must not see the physical mouse/keyboard events."""

    @abc.abstractmethod
    def warp(self, x: int, y: int) -> None: ...

    @abc.abstractmethod
    def bounds(self) -> Bounds: ...


class InputSink(abc.ABC):
    @abc.abstractmethod
    def move_to(self, x: int, y: int) -> None: ...

    @abc.abstractmethod
    def button(self, button: int, down: bool) -> None: ...

    @abc.abstractmethod
    def scroll(self, dx: int, dy: int) -> None: ...

    @abc.abstractmethod
    def key(self, name: str, down: bool, vk: int | None, use_vk: bool) -> None: ...

    @abc.abstractmethod
    def bounds(self) -> Bounds: ...


def modifier_family(name: str) -> str:
    """'key:ctrl_l' / 'key:ctrl_r' / 'key:ctrl' -> 'ctrl' so left/right variants match."""
    if name.startswith("key:"):
        base = name[4:]
        for fam in ("ctrl", "alt", "shift", "cmd"):
            if base == fam or base.startswith(fam + "_"):
                return "alt" if fam == "alt" and base == "alt_gr" else fam
        return base
    return name[5:].lower() if name.startswith("char:") else name


# ---------------------------------------------------------------------------- mocks
class MockSource(InputSource):
    """Pretends to be a physical mouse/keyboard. Mirrors real suppress semantics:
    while suppressed the OS cursor does not move, but the hook still reports the position
    the cursor *would* have moved to (cursor + delta)."""

    def __init__(self, width: int = 1920, height: int = 1080):
        self._bounds = Bounds(width, height)
        self.x, self.y = width // 2, height // 2
        self.suppressed = False
        self.handlers: CaptureHandlers | None = None
        self.warps: list[tuple[int, int]] = []
        self._lock = threading.Lock()

    def start(self, handlers: CaptureHandlers) -> None:
        self.handlers = handlers

    def stop(self) -> None:
        self.handlers = None

    def set_suppress(self, on: bool) -> None:
        self.suppressed = on

    def warp(self, x: int, y: int) -> None:
        self.warps.append((x, y))
        self.x, self.y = x, y

    def bounds(self) -> Bounds:
        return self._bounds

    # -- test drivers -----------------------------------------------------------
    def hardware_move(self, dx: int, dy: int) -> None:
        with self._lock:
            nx = min(max(self.x + dx, 0), self._bounds.width - 1)
            ny = min(max(self.y + dy, 0), self._bounds.height - 1)
            if not self.suppressed:
                self.x, self.y = nx, ny
        self.handlers.on_move(nx, ny)

    def move_to(self, x: int, y: int) -> None:
        """Teleport the (unsuppressed) cursor, like a real mouse sweep reaching a pixel."""
        self.x, self.y = x, y
        self.handlers.on_move(x, y)

    def click(self, button: int, down: bool) -> None:
        self.handlers.on_button(button, down)

    def wheel(self, dx: int, dy: int) -> None:
        self.handlers.on_scroll(dx, dy)

    def key(self, name: str, down: bool, vk: int | None = None) -> None:
        self.handlers.on_key(name, down, vk)


@dataclass
class MockSink(InputSink):
    """Records everything injected, in order."""

    width: int = 1366
    height: int = 768
    log: list[tuple] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _rec(self, *item) -> None:
        with self._lock:
            self.log.append(item)

    def move_to(self, x: int, y: int) -> None:
        self._rec("move", x, y)

    def button(self, button: int, down: bool) -> None:
        self._rec("button", button, down)

    def scroll(self, dx: int, dy: int) -> None:
        self._rec("scroll", dx, dy)

    def key(self, name: str, down: bool, vk: int | None, use_vk: bool) -> None:
        self._rec("key", name, down)

    def bounds(self) -> Bounds:
        return Bounds(self.width, self.height)

    def events(self, kind: str) -> list[tuple]:
        with self._lock:
            return [e for e in self.log if e[0] == kind]
