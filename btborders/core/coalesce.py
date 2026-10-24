"""Mouse-move coalescing.

Mice report at 500-1000 Hz, far more than a Bluetooth link needs. We sum deltas as they
arrive and a sender thread drains the sum at ~125 Hz (MAX_MOVE_HZ). Nothing is lost:
the *sum* of the deltas is preserved, only the packet count shrinks.
"""

from __future__ import annotations

import threading

from .protocol import Move

MAX_MOVE_HZ = 125
FLUSH_INTERVAL = 1.0 / MAX_MOVE_HZ
_I16_MAX = 32767


class MoveCoalescer:
    """Thread-safe accumulator for relative mouse movement."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dx = 0
        self._dy = 0

    def add(self, dx: int, dy: int) -> None:
        with self._lock:
            self._dx += int(dx)
            self._dy += int(dy)

    def clear(self) -> None:
        with self._lock:
            self._dx = self._dy = 0

    def take(self) -> list[Move]:
        """Drain the accumulator. Returns [] when idle, else one or more Move packets
        (more than one only if the sum overflows a signed 16-bit field)."""
        with self._lock:
            dx, dy = self._dx, self._dy
            self._dx = self._dy = 0
        out: list[Move] = []
        while dx or dy:
            sx = max(-_I16_MAX, min(_I16_MAX, dx))
            sy = max(-_I16_MAX, min(_I16_MAX, dy))
            out.append(Move(sx, sy))
            dx -= sx
            dy -= sy
        return out
