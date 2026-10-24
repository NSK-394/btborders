"""End-to-end: Server and Client in one process over TCPLoopbackTransport + mock input."""

import threading

import pytest

from btborders.client import Client
from btborders.config import Config
from btborders.core import crypto, protocol as pr
from btborders.core.channel import SecureChannel
from btborders.core.edge import ARRIVAL_INSET
from btborders.core.transport import (SocketAcceptor, TCPLoopbackTransport, TransportError,
                                      backoff_delays, dial_with_backoff, listen_tcp)
from btborders.input_backend import MockSink, MockSource
from btborders.server import Server

from conftest import Rig, wait_until


def positions_at_events(log):
    """For every non-move event, the receiver cursor position at that moment."""
    pos, out = None, []
    for item in log:
        if item[0] == "move":
            pos = (item[1], item[2])
        else:
            out.append((item, pos))
    return out


def test_crossing_edge_enters_remote_and_maps_position(rig):
    rig.cross_to_remote(y=540)
    assert rig.source.suppressed  # local input is blocked while remote
    first = rig.sink.events("move")[0]
    assert first[1] == ARRIVAL_INSET  # appears at the receiver's LEFT edge
    assert first[2] == pytest.approx(540 / 1079 * 767, abs=2)  # proportional vertical spot


def test_events_arrive_in_order_with_cursor_positions(rig):
    rig.cross_to_remote()
    x0, y0 = rig.sink.events("move")[0][1:]
    rig.source.hardware_move(10, 0)
    rig.source.click(pr.Button.LEFT, True)
    rig.source.hardware_move(0, 10)
    rig.source.click(pr.Button.LEFT, False)
    rig.source.wheel(0, -3)
    rig.source.key("char:a", True, 65)
    rig.source.key("char:a", False, 65)
    assert wait_until(lambda: len(rig.sink.events("key")) == 2)

    seq = positions_at_events(rig.sink.log)
    kinds = [e[0][0] for e in seq]
    assert kinds == ["button", "button", "scroll", "key", "key"]
    # Click happened after the first move but before the second (ordering preserved
    # even though moves are coalesced):
    assert seq[0] == (("button", pr.Button.LEFT, True), (x0 + 10, y0))
    assert seq[1] == (("button", pr.Button.LEFT, False), (x0 + 10, y0 + 10))
    assert seq[2][0] == ("scroll", 0, -3)
    assert seq[3][0] == ("key", "char:a", True) and seq[4][0] == ("key", "char:a", False)


def test_many_small_moves_are_coalesced_but_sum_exactly(rig):
    rig.cross_to_remote()
    x0, y0 = rig.sink.events("move")[0][1:]
    for _ in range(300):
        rig.source.hardware_move(1, 1)
    assert wait_until(lambda: rig.sink.events("move")[-1][1:] == (x0 + 300, y0 + 300))
    assert len(rig.sink.events("move")) < 300  # packets were batched


def test_cursor_returns_via_receiver_edge_and_keys_released(rig):
    rig.cross_to_remote(y=270)
    rig.source.key("key:shift", True)
    rig.source.click(pr.Button.LEFT, True)
    assert wait_until(lambda: ("button", pr.Button.LEFT, True) in rig.sink.events("button"))
    rig.source.hardware_move(-50, 0)  # push back over the receiver's left edge
    assert wait_until(lambda: rig.server.mode == "local" and not rig.client.active)
    assert not rig.source.suppressed
    # Held shift + mouse button were released on the receiver:
    assert ("key", "key:shift", False) in rig.sink.events("key")
    assert ("button", pr.Button.LEFT, False) in rig.sink.events("button")
    # Sender cursor reappears at its right edge, at the matching vertical position:
    x, y = rig.source.warps[-1]
    assert x > 1900 and y == pytest.approx(270, abs=3)
    # And input after returning is no longer forwarded:
    n = len(rig.sink.log)
    rig.source.key("char:z", True)
    rig.source.click(pr.Button.RIGHT, True)
    assert not wait_until(lambda: len(rig.sink.log) > n, timeout=0.3)


def test_can_cross_again_after_returning(rig):
    rig.cross_to_remote()
    rig.source.hardware_move(-50, 0)
    assert wait_until(lambda: rig.server.mode == "local")
    import time
    time.sleep(0.4)  # let the anti-bounce cooldown expire
    rig.cross_to_remote()


def test_emergency_hotkey_returns_instantly_and_releases(rig):
    rig.cross_to_remote()
    rig.source.key("char:a", True)
    rig.source.key("key:ctrl_l", True)
    rig.source.key("key:alt_l", True)
    rig.source.key("char:q", True)  # completes ctrl+alt+q
    assert rig.server.mode == "local"  # synchronous: no waiting on the network
    assert not rig.source.suppressed
    assert wait_until(lambda: not rig.client.active)
    keys = rig.sink.events("key")
    assert ("key", "char:q", True) not in keys  # the hotkey itself is never forwarded
    for name in ("char:a", "key:ctrl_l", "key:alt_l"):
        assert ("key", name, False) in keys  # everything that was held got released


def test_heartbeat_loss_returns_control_to_sender():
    r = Rig(hb_timeout=0.4)
    r.server.start_capture()
    a, b = TCPLoopbackTransport.pair()
    # A peer that authenticates but then goes completely silent (never acks heartbeats).
    peer = SecureChannel(b, r.psk, "client")
    t = threading.Thread(target=lambda: peer.handshake(pr.Hello(1, 1366, 768)))
    t.start()
    srv = threading.Thread(target=r.server.serve_connection,
                           args=(SecureChannel(a, r.psk, "server"), r.stop))
    srv.start()
    t.join()
    assert wait_until(lambda: r.server.status.connected)
    r.source.move_to(1919, 500)
    assert wait_until(lambda: r.server.mode == "remote")
    assert wait_until(lambda: r.server.mode == "local", timeout=3)  # watchdog fired
    assert not r.source.suppressed
    r.stop.set()
    srv.join(timeout=3)
    assert not r.server.status.connected
    a.close(), b.close()


def test_client_watchdog_releases_keys_when_sender_goes_silent():
    psk = crypto.generate_psk()
    sink = MockSink()
    client = Client(sink, Config(), heartbeat_timeout=0.4, handshake_timeout=1.0)
    a, b = TCPLoopbackTransport.pair()
    fake_sender = SecureChannel(a, psk, "server")
    stop = threading.Event()
    th = threading.Thread(target=client.run_connection,
                          args=(SecureChannel(b, psk, "client"), stop))
    th.start()
    fake_sender.handshake(pr.Hello(1, 1920, 1080))
    fake_sender.send(pr.Switch(pr.SwitchAction.ENTER, pr.Edge.RIGHT, 0.5))
    fake_sender.send(pr.KeyEvent("key:ctrl_l", True))
    fake_sender.send(pr.ButtonEvent(pr.Button.LEFT, True))
    assert wait_until(lambda: len(sink.events("button")) == 1)
    # ... and then the sender vanishes without a word.
    assert wait_until(lambda: ("key", "key:ctrl_l", False) in sink.events("key"), timeout=3)
    assert ("button", pr.Button.LEFT, False) in sink.events("button")
    assert not client.active
    th.join(timeout=3)
    a.close()


def test_client_ignores_input_when_not_active():
    psk = crypto.generate_psk()
    sink = MockSink()
    client = Client(sink, Config(), handshake_timeout=1.0)
    a, b = TCPLoopbackTransport.pair()
    fake = SecureChannel(a, psk, "server")
    stop = threading.Event()
    th = threading.Thread(target=client.run_connection, args=(SecureChannel(b, psk, "client"), stop))
    th.start()
    fake.handshake(pr.Hello(1, 1920, 1080))
    fake.send(pr.KeyEvent("char:x", True))          # no switch(enter) yet -> must be ignored
    fake.send(pr.Move(5, 5))
    fake.send(pr.Heartbeat(1, 0.0))
    assert wait_until(lambda: isinstance(fake.recv(timeout=1), pr.HeartbeatAck), timeout=2)
    assert sink.log == []
    stop.set()
    th.join(timeout=3)
    a.close()


def test_wrong_key_peer_is_dropped_and_cannot_inject():
    psk = crypto.generate_psk()
    source = MockSource()
    server = Server(source, Config(), handshake_timeout=1.0)
    server.start_capture()
    a, b = TCPLoopbackTransport.pair()
    attacker = SecureChannel(b, crypto.generate_psk(), "client")  # does not know the PSK
    result = {}
    th = threading.Thread(target=lambda: result.setdefault(
        "ok", server.serve_connection(SecureChannel(a, psk, "server"), threading.Event())))
    th.start()
    try:
        attacker.handshake(pr.Hello(1, 100, 100), timeout=1.0)
    except Exception:
        pass
    th.join(timeout=3)
    assert result["ok"] is False
    assert not server.status.connected
    source.move_to(1919, 500)  # even touching the edge does nothing without a peer
    assert server.mode == "local" and not source.suppressed
    a.close(), b.close()


def test_garbage_and_silent_peers_are_dropped():
    psk = crypto.generate_psk()
    for payload in (None, b"GET / HTTP/1.1\r\n\r\n", b"BTB1" + b"\0" * 5):
        server = Server(MockSource(), Config(), handshake_timeout=0.5)
        a, b = TCPLoopbackTransport.pair()
        if payload:
            b.send(payload)
        ok = server.serve_connection(SecureChannel(a, psk, "server"), threading.Event())
        assert ok is False
        a.close(), b.close()


def test_oversized_preauth_frame_rejected_without_buffering():
    a, b = TCPLoopbackTransport.pair()
    b._write((10_000_000).to_bytes(4, "big"))  # hostile length prefix
    with pytest.raises(TransportError):
        a.recv(timeout=1, max_len=64)
    a.close(), b.close()


def test_clipboard_syncs_on_switch_both_ways():
    r = Rig(server_clip="from sender").start()
    try:
        r.cross_to_remote()
        assert wait_until(lambda: r.cclip.text == "from sender")
        r.cclip.set("copied on receiver")
        r.source.hardware_move(-50, 0)
        assert wait_until(lambda: r.sclip.text == "copied on receiver")
    finally:
        r.close()


def test_vertical_layout_top_edge():
    r = Rig(edge="bottom").start()  # receiver sits below the sender
    try:
        r.source.move_to(960, 1079)
        assert wait_until(lambda: r.server.mode == "remote" and r.client.active)
        x, y = r.sink.events("move")[0][1:]
        assert y == ARRIVAL_INSET and x == pytest.approx(960 / 1919 * 1365, abs=2)
        r.source.hardware_move(0, -50)  # push back up over the receiver's TOP edge
        assert wait_until(lambda: r.server.mode == "local")
    finally:
        r.close()


def test_reconnect_with_backoff_and_session_recovers():
    psk = crypto.generate_psk()
    acceptor = listen_tcp("127.0.0.1", 0)
    port = acceptor._l.getsockname()[1]
    cfg = Config(transport_kind="tcp", tcp_host="127.0.0.1", tcp_port=port)
    server = Server(MockSource(), cfg, heartbeat_interval=0.1, handshake_timeout=1.0)
    client = Client(MockSink(), cfg, handshake_timeout=1.0)
    stop = threading.Event()
    ts = [threading.Thread(target=server.serve_forever, args=(acceptor, psk, stop)),
          threading.Thread(target=client.run_forever, args=(psk, stop))]
    for t in ts:
        t.start()
    try:
        assert wait_until(lambda: client.status.connected and server.status.connected, 5)
        server._channel.close()  # simulate the link dropping
        assert wait_until(lambda: not client.status.connected, 3)
        assert wait_until(lambda: client.status.connected and server.status.connected, 8)
    finally:
        stop.set()
        for t in ts:
            t.join(timeout=5)
        acceptor.close()


def test_backoff_is_exponential_and_capped():
    gen = backoff_delays(initial=1, factor=2, maximum=8, jitter=0)
    assert [next(gen) for _ in range(6)] == [1, 2, 4, 8, 8, 8]


def test_dial_with_backoff_retries_until_success():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TransportError("down")
        return "transport"

    waits = []
    got = dial_with_backoff(flaky, threading.Event(), on_wait=lambda e, w: waits.append(w),
                            delays=iter([0.01, 0.01, 0.01]))
    assert got == "transport" and len(calls) == 3 and len(waits) == 2


def test_dial_with_backoff_stops_when_asked():
    stop = threading.Event()
    stop.set()
    assert dial_with_backoff(lambda: 1 / 0, stop) is None
