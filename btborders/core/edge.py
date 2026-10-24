"""Screen-edge geometry: when does the cursor leave, where does it appear, how does it return.

Terminology (receiver_edge = which side of the *sender's* screen the receiver sits on):

    +-----------+ +-----------+
    |  SENDER   | | RECEIVER  |      receiver_edge = RIGHT
    |        -->| |<--        |      sender exits via its RIGHT edge,
    +-----------+ +-----------+      receiver enters at its LEFT edge (the opposite).

Positions along an edge travel as a 0..1 fraction so screens of different sizes line up
proportionally (a cursor 30% down the sender's right edge appears 30% down the receiver's
left edge).
"""

from __future__ import annotations

from dataclasses import dataclass

from .protocol import Edge

# Pixels inside the edge where a cursor is dropped on arrival, so it does not instantly
# re-trigger a switch.
ARRIVAL_INSET = 3


@dataclass(frozen=True)
class Bounds:
    width: int
    height: int


def at_edge(x: float, y: float, b: Bounds, edge: Edge) -> bool:
    """True when the cursor is on the outermost pixel row/column of ``edge``."""
    if edge is Edge.RIGHT:
        return x >= b.width - 1
    if edge is Edge.LEFT:
        return x <= 0
    if edge is Edge.BOTTOM:
        return y >= b.height - 1
    return y <= 0  # TOP


def edge_fraction(x: float, y: float, b: Bounds, edge: Edge) -> float:
    """Position along the edge as a 0..1 fraction."""
    if edge in (Edge.LEFT, Edge.RIGHT):
        return _clamp01(y / max(1, b.height - 1))
    return _clamp01(x / max(1, b.width - 1))


def point_on_edge(edge: Edge, frac: float, b: Bounds, inset: int = ARRIVAL_INSET
                  ) -> tuple[int, int]:
    """Pixel coordinates on ``edge`` (slightly inset) at fraction ``frac`` along it."""
    frac = _clamp01(frac)
    if edge is Edge.LEFT:
        return inset, round(frac * (b.height - 1))
    if edge is Edge.RIGHT:
        return b.width - 1 - inset, round(frac * (b.height - 1))
    if edge is Edge.TOP:
        return round(frac * (b.width - 1)), inset
    return round(frac * (b.width - 1)), b.height - 1 - inset  # BOTTOM


class VirtualCursor:
    """Receiver-side cursor model driven purely by relative deltas.

    The receiver cannot know where the *sender's* mouse is, only the deltas it receives, so
    it integrates them. When the cursor is pushed past the edge it entered from, we report
    that control should go back.
    """

    def __init__(self, bounds: Bounds):
        self.bounds = bounds
        self.x = 0.0
        self.y = 0.0
        self.return_edge = Edge.LEFT

    def enter(self, sender_exit_edge: Edge, frac: float) -> tuple[int, int]:
        """Place the cursor after a switch(enter). Returns the pixel position."""
        self.return_edge = sender_exit_edge.opposite
        x, y = point_on_edge(self.return_edge, frac, self.bounds)
        self.x, self.y = float(x), float(y)
        return x, y

    def apply(self, dx: int, dy: int) -> bool:
        """Integrate a delta. Returns True if the cursor was pushed back over the return edge."""
        nx, ny = self.x + dx, self.y + dy
        b, e = self.bounds, self.return_edge
        leaving = ((e is Edge.LEFT and nx < 0) or (e is Edge.RIGHT and nx > b.width - 1)
                   or (e is Edge.TOP and ny < 0) or (e is Edge.BOTTOM and ny > b.height - 1))
        self.x = min(max(nx, 0.0), b.width - 1.0)
        self.y = min(max(ny, 0.0), b.height - 1.0)
        return leaving

    @property
    def position(self) -> tuple[int, int]:
        return round(self.x), round(self.y)

    def leave_fraction(self) -> float:
        return edge_fraction(self.x, self.y, self.bounds, self.return_edge)


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))
