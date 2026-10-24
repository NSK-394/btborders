import pytest

from btborders.core.coalesce import MoveCoalescer
from btborders.core.edge import (ARRIVAL_INSET, Bounds, VirtualCursor, at_edge, edge_fraction,
                                 point_on_edge)
from btborders.core.protocol import Edge, Move

B = Bounds(1920, 1080)


@pytest.mark.parametrize("edge,x,y,expected", [
    (Edge.RIGHT, 1919, 500, True), (Edge.RIGHT, 1900, 500, False),
    (Edge.LEFT, 0, 500, True), (Edge.LEFT, 1, 500, False),
    (Edge.TOP, 500, 0, True), (Edge.BOTTOM, 500, 1079, True), (Edge.BOTTOM, 500, 1000, False),
])
def test_at_edge(edge, x, y, expected):
    assert at_edge(x, y, B, edge) is expected


def test_fraction_maps_across_resolutions():
    frac = edge_fraction(1919, 539.5, B, Edge.RIGHT)
    assert frac == pytest.approx(0.5, abs=0.01)
    _, y = point_on_edge(Edge.LEFT, frac, Bounds(1366, 768))
    assert y == pytest.approx(383, abs=2)


def test_point_on_edge_is_inset():
    assert point_on_edge(Edge.LEFT, 0.5, B)[0] == ARRIVAL_INSET
    assert point_on_edge(Edge.RIGHT, 0.5, B)[0] == 1919 - ARRIVAL_INSET
    assert point_on_edge(Edge.TOP, 0.0, B) == (0, ARRIVAL_INSET)


def test_virtual_cursor_enter_opposite_edge():
    v = VirtualCursor(Bounds(1366, 768))
    x, y = v.enter(Edge.RIGHT, 0.5)  # sender left via its RIGHT edge -> we enter on LEFT
    assert x == ARRIVAL_INSET and y == pytest.approx(383, abs=2)
    assert v.return_edge is Edge.LEFT


def test_virtual_cursor_moves_and_clamps():
    v = VirtualCursor(Bounds(100, 100))
    v.enter(Edge.RIGHT, 0.5)
    assert v.apply(40, 10) is False
    assert v.position == (ARRIVAL_INSET + 40, 50 + 10)
    assert v.apply(10_000, 10_000) is False  # clamped on the far edges, not a return
    assert v.position == (99, 99)


def test_virtual_cursor_returns_over_entry_edge():
    v = VirtualCursor(Bounds(100, 100))
    v.enter(Edge.RIGHT, 0.25)
    assert v.apply(-ARRIVAL_INSET, 0) is False  # exactly on the edge is not past it
    assert v.apply(-1, 0) is True
    assert v.leave_fraction() == pytest.approx(0.25, abs=0.02)


@pytest.mark.parametrize("exit_edge,delta", [
    (Edge.LEFT, (1, 0)), (Edge.TOP, (0, 1)), (Edge.BOTTOM, (0, -1)),
])
def test_virtual_cursor_other_layouts(exit_edge, delta):
    # receiver return edge is opposite of exit edge; pushing back toward it must trigger
    v = VirtualCursor(Bounds(100, 100))
    v.enter(exit_edge, 0.5)
    assert v.apply(delta[0] * 200, delta[1] * 200) is True


def test_coalescer_sums_and_preserves_total():
    c = MoveCoalescer()
    for _ in range(1000):
        c.add(1, -2)
    assert c.take() == [Move(1000, -2000)]
    assert c.take() == []  # drained


def test_coalescer_splits_on_overflow():
    c = MoveCoalescer()
    c.add(70000, -5)
    moves = c.take()
    assert len(moves) == 3
    assert sum(m.dx for m in moves) == 70000 and sum(m.dy for m in moves) == -5
    assert all(-32768 <= m.dx <= 32767 for m in moves)


def test_coalescer_cancelling_moves_send_nothing():
    c = MoveCoalescer()
    c.add(5, 5)
    c.add(-5, -5)
    assert c.take() == []
