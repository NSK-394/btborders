"""Receiver: replays the sender's input on this machine.

The receiver keeps a *virtual cursor* integrated from the sender's relative deltas. When that
cursor is pushed back over the edge it entered from, we send switch(leave) and stop injecting.
Safety rules: we only inject while "active" (between switch(enter) and switch(leave)), and we
release every held key/button whenever control ends for any reason - including a dead link.
"""

from __future__ import annotations

import logging
import threading
import time

from . import input_backend as ib
from .clipboard import ClipboardSync, MemoryClipboard
from .config import Config
from .core import crypto, protocol as pr
from .core.channel import AuthError, SecureChannel
from .core.edge import VirtualCursor
from .core.transport import (TransportError, TransportTimeout, backoff_delays, dial,
                             dial_with_backoff)
from .server import HEARTBEAT_TIMEOUT
from .status import Status

log = logging.getLogger("btb.client")


class Client:
    def __init__(self, sink: ib.InputSink, cfg: Config | None = None, clipboard=None,
                 status: Status | None = None, clock=time.monotonic,
                 heartbeat_timeout: float = HEARTBEAT_TIMEOUT,
                 handshake_timeout: float = 5.0):
        self.sink = sink
        self.cfg = cfg or Config()
        self.clock = clock
        self.hb_timeout = heartbeat_timeout
        self.handshake_timeout = handshake_timeout
        self.status = status or Status(role="client")
        self.sync = ClipboardSync(clipboard or MemoryClipboard())
        self.cursor = VirtualCursor(sink.bounds())
        self.active = False
        self._use_vk = False
        self._held_keys: dict[str, int | None] = {}
        self._held_buttons: set[int] = set()
        self._last_rx = 0.0

    # ------------------------------------------------------------------ lifecycle
    def run_forever(self, psk: bytes, stop: threading.Event, on_wait=None) -> None:
        """Dial the sender, reconnecting with exponential backoff until `stop` is set."""
        delays = backoff_delays()
        while not stop.is_set():
            transport = dial_with_backoff(lambda: dial(self.cfg), stop, on_wait, delays)
            if transport is None:
                return
            ok = self.run_connection(SecureChannel(transport, psk, "client"), stop)
            if ok:
                delays = backoff_delays()  # a healthy session resets the backoff
            stop.wait(next(delays) if not ok else 0.5)

    def run_connection(self, channel: SecureChannel, stop: threading.Event) -> bool:
        hello = pr.Hello(pr.PROTOCOL_VERSION, self.cursor.bounds.width,
                         self.cursor.bounds.height, ib.PLATFORM_ID)
        try:
            peer = channel.handshake(hello, self.handshake_timeout)
        except (AuthError, TransportError) as exc:
            log.warning("handshake failed: %s", exc)
            self.status.update(message=f"handshake failed: {exc}")
            channel.close()
            return False
        self._use_vk = peer.platform == ib.PLATFORM_ID  # raw vk codes only valid same-OS
        self._last_rx = self.clock()
        self.status.update(connected=True, peer=channel.transport.name, message="connected")
        try:
            while not stop.is_set():
                try:
                    pkt = channel.recv(timeout=0.1)
                except TransportTimeout:
                    if self.clock() - self._last_rx > self.hb_timeout:
                        log.warning("sender silent for %.1fs - releasing input", self.hb_timeout)
                        self.status.update(message="heartbeat lost")
                        break
                    continue
                except (TransportError, crypto.CryptoError, pr.ProtocolError) as exc:
                    log.info("connection ended: %s", exc)
                    break
                self._last_rx = self.clock()
                self.status.bump("events_received")
                self._handle(pkt, channel)
        finally:
            self.release_all()          # never leave a key or button stuck down
            self.active = False
            self.status.update(connected=False, active="local", latency_ms=None)
            channel.close()
        return True

    # -------------------------------------------------------------------- dispatch
    def _handle(self, pkt: pr.Packet, channel: SecureChannel) -> None:
        if isinstance(pkt, pr.Heartbeat):
            channel.send(pr.HeartbeatAck(pkt.seq, pkt.ts))
        elif isinstance(pkt, pr.Switch):
            if pkt.action is pr.SwitchAction.ENTER:
                x, y = self.cursor.enter(pkt.edge, pkt.pos)
                self.sink.move_to(x, y)
                self.active = True
                self.status.update(active="local", message="receiving input")
            else:  # sender took control back (hotkey etc.)
                self.release_all()
                self.active = False
                self.status.update(active="remote", message="idle")
        elif isinstance(pkt, pr.Clipboard):
            self.sync.incoming(pkt.text)
        elif not self.active:
            return  # defence in depth: input packets outside a session are ignored
        elif isinstance(pkt, pr.Move):
            leaving = self.cursor.apply(pkt.dx, pkt.dy)
            self.sink.move_to(*self.cursor.position)
            if leaving:
                self._leave(channel)
        elif isinstance(pkt, pr.ButtonEvent):
            (self._held_buttons.add if pkt.down else self._held_buttons.discard)(pkt.button)
            self.sink.button(pkt.button, pkt.down)
        elif isinstance(pkt, pr.Scroll):
            self.sink.scroll(pkt.dx, pkt.dy)
        elif isinstance(pkt, pr.KeyEvent):
            if pkt.down:
                self._held_keys[pkt.name] = pkt.vk
            else:
                self._held_keys.pop(pkt.name, None)
            self.sink.key(pkt.name, pkt.down, pkt.vk, self._use_vk)

    def _leave(self, channel: SecureChannel) -> None:
        """Virtual cursor crossed back over the entry edge: hand control to the sender."""
        frac = self.cursor.leave_fraction()
        self.release_all()
        self.active = False
        text = self.sync.outgoing()
        try:
            if text:
                channel.send(pr.Clipboard(text))
            channel.send(pr.Switch(pr.SwitchAction.LEAVE, self.cursor.return_edge, frac))
        except TransportError:
            pass  # link is dying; the main loop will notice
        self.status.update(active="remote", message="returned control")

    def release_all(self) -> None:
        for name, vk in list(self._held_keys.items()):
            self.sink.key(name, False, vk, self._use_vk)
        for b in list(self._held_buttons):
            self.sink.button(b, False)
        self._held_keys.clear()
        self._held_buttons.clear()
