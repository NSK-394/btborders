"""Live status shared between the engine (server/client) and the UI / `btb status`."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class Status:
    role: str = "server"            # server = sender, client = receiver
    connected: bool = False
    peer: str = ""
    latency_ms: float | None = None  # smoothed round-trip time
    active: str = "local"           # which machine currently receives input: local | remote
    message: str = ""
    events_sent: int = 0
    events_received: int = 0
    updated: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def update(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)
            self.updated = time.time()

    def bump(self, name: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + n)

    def snapshot(self) -> dict:
        with self._lock:
            # not asdict(): it would try to deep-copy the lock
            return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "_lock"}

    def write(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.snapshot()), encoding="utf-8")
        except OSError:
            pass
