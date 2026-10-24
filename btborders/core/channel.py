"""SecureChannel = Transport + handshake + AES-GCM Session + packet codec.

Connection setup:
  1. both sides send  "BTB1" || 16 random bytes  (cleartext, <=64 bytes accepted),
  2. both derive directional keys (crypto.Session),
  3. both send an encrypted Hello. A peer without the PSK cannot produce a valid one and is
     dropped before any input event is ever looked at.
"""

from __future__ import annotations

import threading

from . import crypto, protocol
from .transport import MAX_PREAUTH_FRAME, Transport, TransportClosed, TransportError


class AuthError(Exception):
    """Peer failed the handshake (wrong key, wrong protocol, garbage)."""


class SecureChannel:
    def __init__(self, transport: Transport, psk: bytes, role: str):
        self.transport = transport
        self._psk = psk
        self._role = role
        self._session: crypto.Session | None = None
        self._tx_lock = threading.Lock()  # seal() order must equal wire order
        self.peer_hello: protocol.Hello | None = None

    def handshake(self, own_hello: protocol.Hello, timeout: float = 5.0) -> protocol.Hello:
        """Run the full setup. Raises AuthError if the peer is not who we paired with."""
        own_nonce = crypto.new_nonce()
        try:
            self.transport.send(crypto.HANDSHAKE_MAGIC + own_nonce)
            frame = self.transport.recv(timeout, max_len=MAX_PREAUTH_FRAME)
        except TransportClosed:
            raise
        except TransportError as exc:
            raise AuthError(f"bad handshake: {exc}") from exc
        magic_len = len(crypto.HANDSHAKE_MAGIC)
        if len(frame) != magic_len + crypto.NONCE_BYTES or not frame.startswith(
                crypto.HANDSHAKE_MAGIC):
            raise AuthError("peer did not speak the BTBorders handshake")
        try:
            self._session = crypto.Session(self._psk, self._role, own_nonce, frame[magic_len:])
        except crypto.CryptoError as exc:
            raise AuthError(str(exc)) from exc

        self.send(own_hello)
        try:
            pkt = self.recv(timeout, max_len=MAX_PREAUTH_FRAME)
        except (crypto.CryptoError, protocol.ProtocolError) as exc:
            raise AuthError(f"peer failed authentication: {exc}") from exc
        except TransportError as exc:
            raise AuthError(f"no valid hello from peer: {exc}") from exc
        if not isinstance(pkt, protocol.Hello) or pkt.version != protocol.PROTOCOL_VERSION:
            raise AuthError("unexpected hello / protocol version mismatch")
        self.peer_hello = pkt
        return pkt

    def send(self, pkt: protocol.Packet) -> None:
        assert self._session is not None, "handshake first"
        data = protocol.encode(pkt)
        with self._tx_lock:
            self.transport.send(self._session.seal(data))

    def recv(self, timeout: float | None = None, max_len: int | None = None) -> protocol.Packet:
        """Raises TransportTimeout (link fine, nothing yet), TransportClosed, CryptoError,
        or ProtocolError."""
        assert self._session is not None, "handshake first"
        kwargs = {} if max_len is None else {"max_len": max_len}
        frame = self.transport.recv(timeout, **kwargs)
        return protocol.decode(self._session.open(frame))

    def close(self) -> None:
        self.transport.close()
