"""Byte-pipe transports. Every transport moves *frames* (length-prefixed byte strings).

RFCOMM, TCP and serial are all byte *streams*, so message boundaries must be added by us:
each frame is  <4-byte big-endian length> <payload>.  Encryption/auth happens one layer up
(channel.py) - transports are deliberately dumb.

    Transport (abstract)
      +- SocketTransport            shared socket logic
      |    +- TCPLoopbackTransport     testing without Bluetooth
      |    +- BluetoothRFCOMMTransport Bluetooth Classic serial profile
      +- SerialTransport            COM port / /dev/rfcomm0 (pyserial)
"""

from __future__ import annotations

import abc
import random
import select
import socket
import struct
import threading
import time
from collections.abc import Iterator

MAX_FRAME = 256 * 1024          # post-auth ceiling (clipboard is capped at 64 KB)
MAX_PREAUTH_FRAME = 64          # before authentication, accept almost nothing
_LEN = struct.Struct("!I")


class TransportError(Exception):
    pass


class TransportClosed(TransportError):
    """The peer went away or the link failed."""


class TransportTimeout(TransportError):
    """No complete frame arrived in time (the link is still up)."""


class Transport(abc.ABC):
    name = "transport"

    def __init__(self) -> None:
        self._buf = bytearray()
        self._send_lock = threading.Lock()
        self._closed = False

    # -- subclass hooks ---------------------------------------------------------
    @abc.abstractmethod
    def _write(self, data: bytes) -> None: ...

    @abc.abstractmethod
    def _read_some(self, timeout: float | None) -> bytes:
        """Return >=1 bytes, raise TransportTimeout or TransportClosed."""

    @abc.abstractmethod
    def _close(self) -> None: ...

    # -- public API -------------------------------------------------------------
    def send(self, frame: bytes) -> None:
        if len(frame) > MAX_FRAME:
            raise TransportError("frame too large")
        with self._send_lock:
            if self._closed:
                raise TransportClosed("transport closed")
            self._write(_LEN.pack(len(frame)) + frame)

    def recv(self, timeout: float | None = None, max_len: int = MAX_FRAME) -> bytes:
        """Return one whole frame. Partial data survives a TransportTimeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if len(self._buf) >= _LEN.size:
                (n,) = _LEN.unpack_from(self._buf)
                if n > max_len:  # reject BEFORE buffering a hostile length
                    raise TransportError(f"frame length {n} exceeds limit {max_len}")
                if len(self._buf) >= _LEN.size + n:
                    frame = bytes(self._buf[_LEN.size:_LEN.size + n])
                    del self._buf[:_LEN.size + n]
                    return frame
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TransportTimeout()
            if self._closed:
                raise TransportClosed("transport closed")
            self._buf += self._read_some(remaining)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._close()
            except Exception:
                pass


# ------------------------------------------------------------------------- sockets
class SocketTransport(Transport):
    def __init__(self, sock: socket.socket, name: str = "socket"):
        super().__init__()
        self._sock = sock
        self.name = name
        sock.settimeout(5.0)  # bounds sendall() to a stalled peer; recv uses select()

    def _write(self, data: bytes) -> None:
        try:
            self._sock.sendall(data)
        except (OSError, ValueError) as exc:
            raise TransportClosed(str(exc)) from exc

    def _read_some(self, timeout: float | None) -> bytes:
        try:
            ready, _, _ = select.select([self._sock], [], [], timeout)
            if not ready:
                raise TransportTimeout()
            data = self._sock.recv(4096)
        except (OSError, ValueError) as exc:
            raise TransportClosed(str(exc)) from exc
        if not data:
            raise TransportClosed("peer closed the connection")
        return data

    def _close(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()


class TCPLoopbackTransport(SocketTransport):
    """TCP on 127.0.0.1 - lets us exercise the whole stack with no Bluetooth hardware."""

    @classmethod
    def pair(cls) -> tuple["TCPLoopbackTransport", "TCPLoopbackTransport"]:
        """Two connected endpoints in one process (used by tests and `btb test-loopback`)."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        cli = socket.create_connection(srv.getsockname())
        conn, _ = srv.accept()
        srv.close()
        for s in (cli, conn):
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(conn, "tcp-loopback:server"), cls(cli, "tcp-loopback:client")

    @classmethod
    def connect(cls, host: str, port: int) -> "TCPLoopbackTransport":
        try:
            s = socket.create_connection((host, port), timeout=5)
        except OSError as exc:  # refused / unreachable: let dial_with_backoff retry
            raise TransportClosed(f"cannot reach {host}:{port}: {exc}") from exc
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(s, f"tcp:{host}:{port}")


class BluetoothRFCOMMTransport(SocketTransport):
    """Bluetooth Classic RFCOMM via the stdlib socket module (Windows, Linux, macOS*).

    *macOS builds of CPython often lack AF_BLUETOOTH; use SerialTransport there.
    """

    @staticmethod
    def available() -> bool:
        return hasattr(socket, "AF_BLUETOOTH") and hasattr(socket, "BTPROTO_RFCOMM")

    @classmethod
    def connect(cls, mac: str, channel: int) -> "BluetoothRFCOMMTransport":
        if not cls.available():
            raise TransportError("this Python build has no Bluetooth RFCOMM support; "
                                 "use transport.kind = 'serial'")
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        try:
            s.settimeout(10)
            s.connect((mac, channel))
        except OSError as exc:
            s.close()
            raise TransportClosed(f"RFCOMM connect to {mac} ch{channel} failed: {exc}") from exc
        return cls(s, f"rfcomm:{mac}/{channel}")


class SerialTransport(Transport):
    """A serial port, e.g. a Windows Bluetooth 'Outgoing COM port' or /dev/rfcomm0."""

    def __init__(self, port: str, baud: int = 115200):
        super().__init__()
        import serial  # imported lazily so the rest works without pyserial present

        self.name = f"serial:{port}"
        try:
            # serial_for_url accepts plain port names AND URLs like loop:// (used by the tests)
            self._ser = serial.serial_for_url(port, baud, timeout=0.1, write_timeout=5)
        except (serial.SerialException, OSError, ValueError) as exc:
            raise TransportClosed(f"cannot open {port}: {exc}") from exc

    def _write(self, data: bytes) -> None:
        try:
            self._ser.write(data)
            self._ser.flush()
        except Exception as exc:
            raise TransportClosed(str(exc)) from exc

    def _read_some(self, timeout: float | None) -> bytes:
        try:
            self._ser.timeout = 0.1 if timeout is None else max(0.0, min(timeout, 0.1))
            data = self._ser.read(1)
            if not data:
                raise TransportTimeout()
            waiting = self._ser.in_waiting
            return data + (self._ser.read(waiting) if waiting else b"")
        except TransportTimeout:
            raise
        except Exception as exc:
            raise TransportClosed(str(exc)) from exc

    def _close(self) -> None:
        self._ser.close()


# ------------------------------------------------------------------------ acceptors
class Acceptor(abc.ABC):
    """Server-side 'wait for the peer' half. accept() returns None on timeout."""

    @abc.abstractmethod
    def accept(self, timeout: float) -> Transport | None: ...

    @abc.abstractmethod
    def close(self) -> None: ...


class SocketAcceptor(Acceptor):
    def __init__(self, listener: socket.socket, make, desc: str):
        self._l, self._make, self.desc = listener, make, desc

    def accept(self, timeout: float) -> Transport | None:
        try:
            ready, _, _ = select.select([self._l], [], [], timeout)
            if not ready:
                return None
            conn, _addr = self._l.accept()
        except OSError:
            return None
        return self._make(conn)

    def close(self) -> None:
        self._l.close()


class SerialAcceptor(Acceptor):
    """Serial has no 'accept': we simply (re)open the port."""

    def __init__(self, port: str, baud: int):
        self.port, self.baud, self.desc = port, baud, f"serial {port}"

    def accept(self, timeout: float) -> Transport | None:
        try:
            return SerialTransport(self.port, self.baud)
        except TransportError:
            time.sleep(min(timeout, 1.0))
            return None

    def close(self) -> None:
        pass


def listen_tcp(host: str = "127.0.0.1", port: int = 0) -> SocketAcceptor:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(1)

    def make(conn: socket.socket) -> Transport:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return TCPLoopbackTransport(conn, "tcp:server")

    return SocketAcceptor(s, make, f"tcp {host}:{s.getsockname()[1]}")


def listen_rfcomm(channel: int) -> SocketAcceptor:
    if not BluetoothRFCOMMTransport.available():
        raise TransportError("this Python build has no Bluetooth RFCOMM support")
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    try:
        # 00:00:00:00:00:00 = any local adapter. Windows requires an explicit channel 1-30.
        s.bind(("00:00:00:00:00:00", channel))
        s.listen(1)
    except OSError as exc:
        s.close()
        raise TransportError(
            f"cannot listen on RFCOMM channel {channel}: {exc}. Is Bluetooth turned on, is an "
            "adapter present, and is the channel free?") from exc
    return SocketAcceptor(s, lambda c: BluetoothRFCOMMTransport(c, f"rfcomm:ch{channel}"),
                          f"rfcomm channel {channel}")


# ------------------------------------------------------------------ factories (config)
def make_acceptor(cfg) -> Acceptor:
    kind = cfg.transport_kind
    if kind == "bluetooth":
        return listen_rfcomm(cfg.rfcomm_channel)
    if kind == "serial":
        return SerialAcceptor(cfg.serial_port, cfg.serial_baud)
    if kind == "tcp":
        return listen_tcp(cfg.tcp_host, cfg.tcp_port)
    raise TransportError(f"unknown transport kind {kind!r}")


def dial(cfg) -> Transport:
    kind = cfg.transport_kind
    if kind == "bluetooth":
        if not cfg.peer_mac:
            raise TransportError("no peer MAC configured - run `btb discover` / `btb pair`")
        return BluetoothRFCOMMTransport.connect(cfg.peer_mac, cfg.rfcomm_channel)
    if kind == "serial":
        return SerialTransport(cfg.serial_port, cfg.serial_baud)
    if kind == "tcp":
        return TCPLoopbackTransport.connect(cfg.tcp_host, cfg.tcp_port)
    raise TransportError(f"unknown transport kind {kind!r}")


# ------------------------------------------------------------------------- reconnect
def backoff_delays(initial: float = 0.5, factor: float = 2.0, maximum: float = 15.0,
                   jitter: float = 0.1) -> Iterator[float]:
    """0.5, 1, 2, 4, 8, 15, 15, ... (each +/- jitter so two retrying peers desynchronise)."""
    delay = initial
    while True:
        yield delay * (1 + random.uniform(-jitter, jitter))
        delay = min(delay * factor, maximum)


def dial_with_backoff(dial_fn, stop: threading.Event, on_wait=None,
                      delays: Iterator[float] | None = None) -> Transport | None:
    """Keep trying dial_fn() until it works or `stop` is set (then returns None)."""
    delays = delays or backoff_delays()
    while not stop.is_set():
        try:
            return dial_fn()
        except TransportError as exc:
            wait = next(delays)
            if on_wait:
                on_wait(exc, wait)
            stop.wait(wait)
    return None
