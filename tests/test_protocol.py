import pytest

from btborders.core import protocol as p

SAMPLES = [
    p.Hello(1, 1920, 1080),
    p.Move(-5, 300),
    p.Move(-32768, 32767),
    p.ButtonEvent(p.Button.RIGHT, True),
    p.ButtonEvent(p.Button.LEFT, False),
    p.Scroll(0, -3),
    p.KeyEvent("char:a", True),
    p.KeyEvent("key:ctrl_l", False),
    p.KeyEvent("char:é", True),
    p.Clipboard("héllo wörld ✓"),
    p.Clipboard(""),
    p.Switch(p.SwitchAction.ENTER, p.Edge.RIGHT, 0.25),
    p.Heartbeat(7, 12.5),
    p.HeartbeatAck(7, 12.5),
]


@pytest.mark.parametrize("pkt", SAMPLES)
def test_roundtrip(pkt):
    assert p.decode(p.encode(pkt)) == pkt


def test_move_is_small():
    assert len(p.encode(p.Move(1, 1))) == 5


def test_move_clamps_to_i16():
    assert p.decode(p.encode(p.Move(99999, -99999))) == p.Move(32767, -32768)


@pytest.mark.parametrize("bad", [b"", b"\xff", b"\x02\x00", b"\x05", b"\x05\x01",
                                 b"\x07\x09\x00\x00\x00\x00\x00", b"\x06\xff\xfe"])
def test_malformed_rejected(bad):
    with pytest.raises(p.ProtocolError):
        p.decode(bad)


def test_clipboard_cap():
    with pytest.raises(p.ProtocolError):
        p.encode(p.Clipboard("x" * (p.MAX_CLIPBOARD_BYTES + 1)))
    with pytest.raises(p.ProtocolError):
        p.decode(bytes([p.PacketType.CLIPBOARD]) + b"x" * (p.MAX_CLIPBOARD_BYTES + 1))
    assert p.decode(p.encode(p.Clipboard("x" * p.MAX_CLIPBOARD_BYTES)))


def test_edge_opposite():
    assert p.Edge.LEFT.opposite is p.Edge.RIGHT
    assert p.Edge.TOP.opposite is p.Edge.BOTTOM
