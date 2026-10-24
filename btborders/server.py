"""Sender: runs on the machine that owns the physical mouse and keyboard.

Modes
  LOCAL   input behaves normally; we only watch for the cursor reaching the receiver's edge
  REMOTE  local input is suppressed; deltas/clicks/keys are streamed to the receiver

Threads
  capture threads  (OS hooks)   in-memory work only: update state, enqueue packets. They must
                                never block on the network - with input suppressed, a hook
                                stuck on a dead link would freeze the user's local mouse.
  writer thread                 drains the packet queue to the channel, flushes coalesced
                                moves at <=125 Hz, sends heartbeats
  rx thread (serve_connection)  reads acks / switch(leave) / clipboard, runs the watchdog
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from . import input_backend as ib
from .clipboard import ClipboardSync, MemoryClipboard
from .config import Config
from .core import crypto, protocol as pr
from .core.channel import AuthError, SecureChannel
from .core.coalesce import FLUSH_INTERVAL, MoveCoalescer
from .core.edge import Bounds, at_edge, edge_fraction, point_on_edge
from .core.transport import TransportClosed, TransportError, TransportTimeout
from .status import Status

log = logging.getLogger("btb.server")

HEARTBEAT_INTERVAL = 0.5   # seconds between heartbeats
HEARTBEAT_TIMEOUT = 2.0    # silence longer than this = link dead -> take control back
SWITCH_COOLDOWN = 0.3      # after returning, ignore the edge briefly so we do not bounce


def parse_hotkey(spec: str) -> frozenset[str]:
    """'ctrl+alt+q' -> {'ctrl','alt','q'}"""
    parts = frozenset(p.strip().lower() for p in spec.split("+") if p.strip())
    if not parts:
        raise ValueError("empty hotkey")
    return parts


class Server:
    def __init__(self, source: ib.InputSource, cfg: Config, clipboard=None,
                 status: Status | None = None, clock=time.monotonic,
                 heartbeat_interval: float = HEARTBEAT_INTERVAL,
                 heartbeat_timeout: float = HEARTBEAT_TIMEOUT,
                 handshake_timeout: float = 5.0):
        self.source = source
        self.cfg = cfg
        self.edge = cfg.edge
        self.hotkey = parse_hotkey(cfg.hotkey_return)
        self.bounds: Bounds = source.bounds()
        self.clock = clock
        self.hb_interval = heartbeat_interval
        self.hb_timeout = heartbeat_timeout
        self.handshake_timeout = handshake_timeout
        self.status = status or Status(role="server")
        self.sync = ClipboardSync(clipboard or MemoryClipboard())

        self._lock = threading.RLock()       # guards mode / channel; never held across I/O
        self._q_lock = threading.Lock()      # makes "flush moves then enqueue X" atomic
        self._outq: queue.SimpleQueue = queue.SimpleQueue()
        self._coalescer = MoveCoalescer()
        self._last_flush = 0.0
        self._mode = "local"
        self._channel: SecureChannel | None = None
        self._peer_hello: pr.Hello | None = None
        self._cooldown_until = 0.0
        self._entry_frac = 0.5
        self._pressed: set[str] = set()      # modifier families / keys physically down
        self._remote_keys: dict[str, int | None] = {}
        self._remote_buttons: set[int] = set()
        self._last_rx = 0.0
        self._max_jump = max(self.bounds.width, self.bounds.height) // 2

    # ------------------------------------------------------------------ lifecycle
    def start_capture(self) -> None:
        self.source.start(ib.CaptureHandlers(self._on_move, self._on_button, self._on_scroll,
                                             self._on_key))

    def stop_capture(self) -> None:
        self.return_local("shutdown")
        self.source.stop()

    @property
    def mode(self) -> str:
        return self._mode

    def serve_forever(self, acceptor, psk: bytes, stop: threading.Event) -> None:
        """Accept one peer at a time, forever (until `stop`). Reconnects are the peer's job."""
        while not stop.is_set():
            transport = acceptor.accept(0.5)
            if transport is None:
                continue
            self.serve_connection(SecureChannel(transport, psk, "server"), stop)

    def serve_connection(self, channel: SecureChannel, stop: threading.Event) -> bool:
        """Handle one connection until it ends. Returns True if the peer authenticated."""
        hello = pr.Hello(pr.PROTOCOL_VERSION, self.bounds.width, self.bounds.height,
                         ib.PLATFORM_ID)
        try:
            peer = channel.handshake(hello, self.handshake_timeout)
        except (AuthError, TransportError) as exc:
            log.warning("dropping peer (%s): %s", channel.transport.name, exc)
            self.status.update(message=f"rejected peer: {exc}")
            channel.close()
            return False

        with self._lock:
            self._channel, self._peer_hello = channel, peer
            self._last_rx = self.clock()
            self._drain_queue()
        self.status.update(connected=True, peer=channel.transport.name, message="connected")
        writer = threading.Thread(target=self._writer, args=(channel,), daemon=True,
                                  name="btb-writer")
        writer.start()
        try:
            self._rx_loop(channel, stop)
        finally:
            self._drop_connection(channel)
            writer.join(timeout=2)
        return True

    # ------------------------------------------------------------------ rx side
    def _rx_loop(self, channel: SecureChannel, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                pkt = channel.recv(timeout=0.1)
            except TransportTimeout:
                if self.clock() - self._last_rx > self.hb_timeout:
                    log.warning("heartbeat missed for %.1fs - taking control back",
                                self.hb_timeout)
                    self.status.update(message="heartbeat lost")
                    return
                continue
            except (TransportError, crypto.CryptoError, pr.ProtocolError) as exc:
                log.info("connection ended: %s", exc)
                return
            self._last_rx = self.clock()
            self.status.bump("events_received")
            if isinstance(pkt, pr.HeartbeatAck):
                rtt = (time.perf_counter() - pkt.ts) * 1000.0  # perf_counter: monotonic() is ~15 ms coarse on Windows
                prev = self.status.latency_ms
                self.status.update(latency_ms=rtt if prev is None else prev * 0.8 + rtt * 0.2)
            elif isinstance(pkt, pr.Switch) and pkt.action is pr.SwitchAction.LEAVE:
                # Receiver cursor hit its return edge: bring control home at the matching spot.
                self.return_local("peer returned control", frac=pkt.pos, notify=False)
            elif isinstance(pkt, pr.Clipboard):
                self.sync.incoming(pkt.text)

    # ---------------------------------------------------------------- writer side
    def _writer(self, channel: SecureChannel) -> None:
        seq = 0
        next_beat = 0.0
        try:
            while self._channel is channel:
                try:
                    pkt = self._outq.get(timeout=FLUSH_INTERVAL)
                    channel.send(pkt)
                    self.status.bump("events_sent")
                except queue.Empty:
                    pass
                with self._q_lock:
                    self._queue_moves()
                now = self.clock()
                if now >= next_beat:
                    channel.send(pr.Heartbeat(seq, time.perf_counter()))
                    seq += 1
                    next_beat = now + self.hb_interval
        except TransportError as exc:
            log.info("writer stopped: %s", exc)
            channel.close()  # wakes the rx loop, which tears everything down

    def _drain_queue(self) -> None:
        with self._q_lock:
            self._coalescer.clear()
            while True:
                try:
                    self._outq.get_nowait()
                except queue.Empty:
                    break

    def _queue_moves(self) -> None:  # caller holds _q_lock
        for m in self._coalescer.take():
            self._outq.put(m)
        self._last_flush = self.clock()

    def _emit(self, pkt: pr.Packet) -> None:
        """Enqueue in order: any pending mouse movement goes out *before* this packet."""
        with self._q_lock:
            self._queue_moves()
            self._outq.put(pkt)

    # ------------------------------------------------------------ mode transitions
    def _enter_remote(self, x: int, y: int) -> None:
        with self._lock:
            if (self._channel is None or self._mode == "remote"
                    or self.clock() < self._cooldown_until):
                return
            frac = edge_fraction(x, y, self.bounds, self.edge)
            self._entry_frac = frac
            self._mode = "remote"
            self._drain_queue()
            text = self.sync.outgoing()
            if text:
                self._emit(pr.Clipboard(text))
            self._emit(pr.Switch(pr.SwitchAction.ENTER, self.edge, frac))
            self.source.set_suppress(True)
            self.source.warp(self.bounds.width // 2, self.bounds.height // 2)
        self.status.update(active="remote", message="controlling the other machine")
        log.info("entered REMOTE mode at %.0f%% along the %s edge", frac * 100, self.edge.name)

    def return_local(self, reason: str, frac: float | None = None, notify: bool = True) -> bool:
        """Give control back to this machine. Safe to call from any thread, any time."""
        with self._lock:
            if self._mode == "local":
                return False
            self._mode = "local"
            self._coalescer.clear()
            if notify and self._channel is not None:
                # Release anything the receiver still thinks is held, then tell it we are back.
                for name, vk in self._remote_keys.items():
                    self._emit(pr.KeyEvent(name, False, vk))
                for b in self._remote_buttons:
                    self._emit(pr.ButtonEvent(b, False))
                self._emit(pr.Switch(pr.SwitchAction.LEAVE, self.edge, 0.5))
            self._remote_keys.clear()
            self._remote_buttons.clear()
            self.source.set_suppress(False)
            if frac is None:
                self.source.warp(self.bounds.width // 2, self.bounds.height // 2)
            else:
                self.source.warp(*point_on_edge(self.edge, frac, self.bounds))
            self._cooldown_until = self.clock() + SWITCH_COOLDOWN
        self.status.update(active="local", message=reason)
        log.info("back to LOCAL: %s", reason)
        return True

    def _drop_connection(self, channel: SecureChannel) -> None:
        with self._lock:
            if self._channel is channel:
                self.return_local("connection lost", notify=False)
                self._channel = None
        self.status.update(connected=False, latency_ms=None)
        channel.close()

    # ------------------------------------------------------------- capture handlers
    def _on_move(self, x: int, y: int) -> None:
        if self._mode == "local":
            if at_edge(x, y, self.bounds, self.edge):
                self._enter_remote(x, y)
            return
        cx, cy = self.bounds.width // 2, self.bounds.height // 2
        dx, dy = x - cx, y - cy
        if dx == 0 and dy == 0:
            return  # echo of our own warp
        if abs(dx) > self._max_jump or abs(dy) > self._max_jump:
            self.source.warp(cx, cy)  # stale pre-warp event, not real movement
            return
        self._coalescer.add(dx, dy)
        self.source.warp(cx, cy)
        if self.clock() - self._last_flush >= FLUSH_INTERVAL:
            with self._q_lock:
                self._queue_moves()

    def _on_button(self, button: int, down: bool) -> None:
        if self._mode != "remote":
            return
        (self._remote_buttons.add if down else self._remote_buttons.discard)(button)
        self._emit(pr.ButtonEvent(button, down))

    def _on_scroll(self, dx: int, dy: int) -> None:
        if self._mode == "remote":
            self._emit(pr.Scroll(dx, dy))

    def _on_key(self, name: str, down: bool, vk: int | None) -> None:
        family = ib.modifier_family(name)
        if down:
            self._pressed.add(family)
            if family in self.hotkey and self.hotkey <= self._pressed:
                self.return_local("emergency hotkey")
                return  # never forward the hotkey itself
        else:
            self._pressed.discard(family)
        if self._mode != "remote":
            return
        if down:
            self._remote_keys[name] = vk
        else:
            self._remote_keys.pop(name, None)
        self._emit(pr.KeyEvent(name, down, vk))
