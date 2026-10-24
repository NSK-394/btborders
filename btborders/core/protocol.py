"""Wire protocol: compact binary event packets.

Every packet is  <1 byte type> <payload>.  Payloads use fixed ``struct`` layouts so a
mouse move is only 5 bytes (before encryption adds its 8-byte counter + 16-byte tag).

Mouse deltas are *relative* (dx, dy) so differing screen resolutions do not matter.
Keys are sent by *name* ("char:a", "key:ctrl_l", "vk:96") rather than by raw scancode, so
the mapping does not depend on the sender's keyboard layout.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

PROTOCOL_VERSION = 1
MAX_CLIPBOARD_BYTES = 64 * 1024  # v1 cap for clipboard text


class ProtocolError(ValueError):
    """Raised when a packet cannot be decoded."""


class PacketType(IntEnum):
    HELLO = 1
    MOVE = 2
    BUTTON = 3
    SCROLL = 4
    KEY = 5
    CLIPBOARD = 6
    SWITCH = 7
    HEARTBEAT = 8
    HEARTBEAT_ACK = 9


class Edge(IntEnum):
    LEFT = 0
    RIGHT = 1
    TOP = 2
    BOTTOM = 3

    @property
    def opposite(self) -> "Edge":
        return _OPPOSITE[self]


_OPPOSITE = {Edge.LEFT: Edge.RIGHT, Edge.RIGHT: Edge.LEFT, Edge.TOP: Edge.BOTTOM,
             Edge.BOTTOM: Edge.TOP}


class SwitchAction(IntEnum):
    ENTER = 0  # sender -> receiver: "you now own the input"
    LEAVE = 1  # either direction: "control goes back to the sender"


class Button(IntEnum):
    LEFT = 0
    RIGHT = 1
    MIDDLE = 2
    X1 = 3
    X2 = 4


# ---------------------------------------------------------------- packet classes
@dataclass(frozen=True)
class Hello:
    version: int
    width: int
    height: int


@dataclass(frozen=True)
class Move:
    dx: int
    dy: int


@dataclass(frozen=True)
class ButtonEvent:
    button: int
    down: bool


@dataclass(frozen=True)
class Scroll:
    dx: int
    dy: int


@dataclass(frozen=True)
class KeyEvent:
    name: str  # "char:a" | "key:ctrl_l" | "vk:96"
    down: bool


@dataclass(frozen=True)
class Clipboard:
    text: str


@dataclass(frozen=True)
class Switch:
    action: SwitchAction
    edge: Edge
    pos: float  # 0.0-1.0 position along the edge (fraction of screen height/width)


@dataclass(frozen=True)
class Heartbeat:
    seq: int
    ts: float  # sender's monotonic clock; only the sender interprets it


@dataclass(frozen=True)
class HeartbeatAck:
    seq: int
    ts: float  # echoed back unchanged so the sender can compute RTT


Packet = (Hello | Move | ButtonEvent | Scroll | KeyEvent | Clipboard | Switch | Heartbeat
          | HeartbeatAck)

_HELLO = struct.Struct("!BHH")
_XY = struct.Struct("!hh")
_BTN = struct.Struct("!BB")
_SWITCH = struct.Struct("!BBf")
_BEAT = struct.Struct("!Id")


def encode(pkt: Packet) -> bytes:
    """Serialise a packet to bytes."""
    if isinstance(pkt, Hello):
        return bytes([PacketType.HELLO]) + _HELLO.pack(pkt.version, pkt.width, pkt.height)
    if isinstance(pkt, Move):
        return bytes([PacketType.MOVE]) + _XY.pack(_i16(pkt.dx), _i16(pkt.dy))
    if isinstance(pkt, ButtonEvent):
        return bytes([PacketType.BUTTON]) + _BTN.pack(pkt.button, int(pkt.down))
    if isinstance(pkt, Scroll):
        return bytes([PacketType.SCROLL]) + _XY.pack(_i16(pkt.dx), _i16(pkt.dy))
    if isinstance(pkt, KeyEvent):
        name = pkt.name.encode("utf-8")
        if len(name) > 64:
            raise ProtocolError("key name too long")
        return bytes([PacketType.KEY, int(pkt.down)]) + name
    if isinstance(pkt, Clipboard):
        data = pkt.text.encode("utf-8")
        if len(data) > MAX_CLIPBOARD_BYTES:
            raise ProtocolError("clipboard text exceeds cap")
        return bytes([PacketType.CLIPBOARD]) + data
    if isinstance(pkt, Switch):
        return bytes([PacketType.SWITCH]) + _SWITCH.pack(pkt.action, pkt.edge, pkt.pos)
    if isinstance(pkt, Heartbeat):
        return bytes([PacketType.HEARTBEAT]) + _BEAT.pack(pkt.seq, pkt.ts)
    if isinstance(pkt, HeartbeatAck):
        return bytes([PacketType.HEARTBEAT_ACK]) + _BEAT.pack(pkt.seq, pkt.ts)
    raise ProtocolError(f"cannot encode {type(pkt).__name__}")


def decode(data: bytes) -> Packet:
    """Parse bytes into a packet. Raises ProtocolError on anything malformed."""
    if not data:
        raise ProtocolError("empty packet")
    try:
        ptype = PacketType(data[0])
    except ValueError:
        raise ProtocolError(f"unknown packet type {data[0]}") from None
    body = data[1:]
    try:
        if ptype is PacketType.HELLO:
            return Hello(*_HELLO.unpack(body))
        if ptype is PacketType.MOVE:
            return Move(*_XY.unpack(body))
        if ptype is PacketType.BUTTON:
            btn, down = _BTN.unpack(body)
            return ButtonEvent(btn, bool(down))
        if ptype is PacketType.SCROLL:
            return Scroll(*_XY.unpack(body))
        if ptype is PacketType.KEY:
            if len(body) < 2:
                raise ProtocolError("short key packet")
            return KeyEvent(body[1:].decode("utf-8"), bool(body[0]))
        if ptype is PacketType.CLIPBOARD:
            if len(body) > MAX_CLIPBOARD_BYTES:
                raise ProtocolError("clipboard text exceeds cap")
            return Clipboard(body.decode("utf-8"))
        if ptype is PacketType.SWITCH:
            action, edge, pos = _SWITCH.unpack(body)
            return Switch(SwitchAction(action), Edge(edge), pos)
        if ptype is PacketType.HEARTBEAT:
            return Heartbeat(*_BEAT.unpack(body))
        if ptype is PacketType.HEARTBEAT_ACK:
            return HeartbeatAck(*_BEAT.unpack(body))
    except (struct.error, UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"malformed {ptype.name} packet: {exc}") from exc
    raise ProtocolError("unhandled packet type")  # pragma: no cover


def _i16(v: int) -> int:
    """Clamp to the signed 16-bit range used on the wire."""
    return max(-32768, min(32767, int(v)))
