"""`btb test-loopback`: run sender + receiver on ONE machine, no Bluetooth needed.

Both engines run in this process, wired over a TCP loopback pair and encrypted exactly like a
real link. The sender's physical input is *scripted* (we never hook your real mouse), and the
receiver either records events (default, completely safe) or with live=True moves your real
cursor in a small pattern so you can watch it work.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .client import Client
from .clipboard import MemoryClipboard
from .config import Config
from .core import crypto, protocol as pr
from .core.channel import SecureChannel
from .core.transport import TCPLoopbackTransport
from .input_backend import MockSink, MockSource
from .server import Server


def _wait(cond: Callable[[], bool], timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def run_loopback(live: bool = False, out: Callable[[str], None] = print) -> bool:
    checks: list[tuple[str, bool]] = []

    def check(label: str, ok: bool) -> bool:
        checks.append((label, ok))
        out(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        return ok

    psk = crypto.generate_psk()
    cfg = Config(receiver_edge="right")
    source = MockSource(1920, 1080)
    if live:
        from .pynput_backend import PynputSink, prepare_process
        prepare_process()
        sink = PynputSink()
        recorder = None
    else:
        sink = recorder = MockSink(1366, 768)
    sclip, cclip = MemoryClipboard("clipboard from sender"), MemoryClipboard()
    server = Server(source, cfg, sclip, heartbeat_interval=0.1)
    client = Client(sink, cfg, cclip)
    stop = threading.Event()
    s_tr, c_tr = TCPLoopbackTransport.pair()
    server.start_capture()
    threads = [
        threading.Thread(target=server.serve_connection,
                         args=(SecureChannel(s_tr, psk, "server"), stop)),
        threading.Thread(target=client.run_connection,
                         args=(SecureChannel(c_tr, psk, "client"), stop)),
    ]
    for t in threads:
        t.start()
    try:
        out("Encrypted loopback link (AES-GCM over TCP 127.0.0.1)")
        if not check("handshake + authentication", _wait(
                lambda: server.status.connected and client.status.connected)):
            return False

        source.move_to(1919, 540)  # sender cursor sweeps into its right edge
        check("edge crossing hands control to the receiver",
              _wait(lambda: server.mode == "remote" and client.active))
        check("sender local input is suppressed while remote", source.suppressed)
        check("clipboard text followed the cursor", _wait(
            lambda: cclip.text == "clipboard from sender"))

        for _ in range(20):  # a smooth 200 px sweep to the right
            source.hardware_move(10, 0)
            time.sleep(0.01)
        if recorder:
            source.click(pr.Button.LEFT, True)
            source.click(pr.Button.LEFT, False)
            source.key("char:h", True)
            source.key("char:h", False)
            source.wheel(0, -2)
            check("moves, click, keys, scroll arrived in order", _wait(
                lambda: [e[0] for e in recorder.log if e[0] != "move"]
                == ["button", "button", "key", "key", "scroll"]))
            check("mouse deltas were coalesced", len(recorder.events("move")) < 21)
        else:
            time.sleep(0.3)

        # Receiver hits its entry edge going back -> control returns to the sender.
        source.hardware_move(-400, 0)
        check("pushing back over the receiver edge returns control",
              _wait(lambda: server.mode == "local" and not client.active))
        check("sender input restored", not source.suppressed)

        time.sleep(0.4)  # let the anti-bounce cooldown pass
        source.move_to(1919, 300)
        _wait(lambda: server.mode == "remote")
        source.key("key:ctrl_l", True)
        source.key("key:alt_l", True)
        source.key("char:q", True)  # emergency hotkey
        check("emergency hotkey (Ctrl+Alt+Q) returns control instantly",
              server.mode == "local" and not source.suppressed)

        check("round-trip latency measured", _wait(lambda: server.status.latency_ms is not None))
        lat = server.status.latency_ms
        if lat is not None:
            out(f"  latency (loopback, includes encryption): {lat:.2f} ms")
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=3)
        s_tr.close()
        c_tr.close()
    ok = all(v for _, v in checks)
    out("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    return ok
