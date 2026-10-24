import threading
import time

import pytest

from btborders.client import Client
from btborders.clipboard import MemoryClipboard
from btborders.config import Config
from btborders.core import crypto
from btborders.core.channel import SecureChannel
from btborders.core.transport import TCPLoopbackTransport
from btborders.input_backend import MockSink, MockSource
from btborders.server import Server


def wait_until(cond, timeout=3.0, interval=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(interval)
    return bool(cond())


class Rig:
    """A server and a client wired together over a TCP loopback pair with mock input."""

    def __init__(self, hb_timeout=2.0, edge="right", server_clip="", client_clip=""):
        self.psk = crypto.generate_psk()
        self.cfg = Config(receiver_edge=edge)
        self.source = MockSource(1920, 1080)
        self.sink = MockSink(1366, 768)
        self.sclip, self.cclip = MemoryClipboard(server_clip), MemoryClipboard(client_clip)
        self.server = Server(self.source, self.cfg, self.sclip, heartbeat_timeout=hb_timeout,
                             heartbeat_interval=0.1, handshake_timeout=1.0)
        self.client = Client(self.sink, self.cfg, self.cclip, heartbeat_timeout=hb_timeout,
                             handshake_timeout=1.0)
        self.stop = threading.Event()
        self.s_tr, self.c_tr = TCPLoopbackTransport.pair()
        self.threads = []

    def start(self):
        self.server.start_capture()
        self.threads = [
            threading.Thread(target=self.server.serve_connection,
                             args=(SecureChannel(self.s_tr, self.psk, "server"), self.stop)),
            threading.Thread(target=self.client.run_connection,
                             args=(SecureChannel(self.c_tr, self.psk, "client"), self.stop)),
        ]
        for t in self.threads:
            t.start()
        assert wait_until(lambda: self.server.status.connected and self.client.status.connected)
        return self

    def cross_to_remote(self, y=540):
        """Sweep the sender's cursor into the right edge and wait for the receiver to take over."""
        self.source.move_to(1919, y)
        assert wait_until(lambda: self.server.mode == "remote" and self.client.active)

    def close(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=3)
        self.s_tr.close()
        self.c_tr.close()


@pytest.fixture
def rig():
    r = Rig().start()
    yield r
    r.close()
