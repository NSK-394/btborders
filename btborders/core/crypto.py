"""Pairing secrets and the AES-GCM secure session.

Threat model: anyone in Bluetooth range who can open the RFCOMM channel could try to inject
keystrokes. So *every* frame is authenticated with a key derived from a pre-shared secret
(PSK) that only the two paired machines know. A peer that cannot produce a valid frame is
dropped.

Per connection both sides exchange a random nonce in the clear and derive two directional
keys with HKDF(psk, salt=server_nonce||client_nonce). Consequences:
  * a recording of an old session is useless in a new one (different keys),
  * a frame cannot be reflected back at its sender (different key per direction),
  * within a session a strictly increasing counter rejects replays and reordering.
"""

from __future__ import annotations

import base64
import hmac
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PSK_BYTES = 32
NONCE_BYTES = 16
HANDSHAKE_MAGIC = b"BTB1"
_COUNTER = struct.Struct("!Q")


class CryptoError(Exception):
    """Authentication failed, or the frame was replayed / reordered / malformed."""


# ------------------------------------------------------------------- pairing codes
def generate_psk() -> bytes:
    return os.urandom(PSK_BYTES)


def psk_to_code(psk: bytes) -> str:
    """Human-typeable pairing code: base32 in groups of 4, with a 2-byte checksum so typos
    are caught on import."""
    check = _checksum(psk)
    raw = base64.b32encode(psk + check).decode().rstrip("=")
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def code_to_psk(code: str) -> bytes:
    cleaned = "".join(code.upper().split()).replace("-", "")
    cleaned += "=" * (-len(cleaned) % 8)
    try:
        raw = base64.b32decode(cleaned)
    except Exception as exc:
        raise CryptoError("pairing code is not valid base32") from exc
    if len(raw) != PSK_BYTES + 2:
        raise CryptoError("pairing code has the wrong length")
    psk, check = raw[:PSK_BYTES], raw[PSK_BYTES:]
    if not hmac.compare_digest(check, _checksum(psk)):
        raise CryptoError("pairing code checksum mismatch (typo?)")
    return psk


def _checksum(psk: bytes) -> bytes:
    h = hashes.Hash(hashes.SHA256())
    h.update(b"btb-code-check" + psk)
    return h.finalize()[:2]


# ------------------------------------------------------------------- secure session
def new_nonce() -> bytes:
    return os.urandom(NONCE_BYTES)


class Session:
    """Encrypts outgoing and decrypts incoming frames for one connection.

    role is "server" (the sender / physical-input machine) or "client" (the receiver).
    """

    def __init__(self, psk: bytes, role: str, own_nonce: bytes, peer_nonce: bytes):
        if role not in ("server", "client"):
            raise ValueError("role must be 'server' or 'client'")
        if len(psk) != PSK_BYTES:
            raise ValueError("PSK must be 32 bytes")
        if own_nonce == peer_nonce:
            # Reflection attack: attacker echoes our own nonce back at us.
            raise CryptoError("peer nonce equals our nonce")
        server_nonce, client_nonce = ((own_nonce, peer_nonce) if role == "server"
                                      else (peer_nonce, own_nonce))
        okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=server_nonce + client_nonce,
                   info=b"btborders v1 session keys").derive(psk)
        s2c, c2s = okm[:32], okm[32:]
        send_key, recv_key = (s2c, c2s) if role == "server" else (c2s, s2c)
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._send_counter = 0
        self._last_recv = -1

    def seal(self, plaintext: bytes) -> bytes:
        """Return counter(8) || ciphertext+tag. Counter doubles as the GCM nonce."""
        header = _COUNTER.pack(self._send_counter)
        self._send_counter += 1
        return header + self._send.encrypt(b"\0\0\0\0" + header, plaintext, header)

    def open(self, frame: bytes) -> bytes:
        if len(frame) < _COUNTER.size + 16:
            raise CryptoError("frame too short")
        header, body = frame[:_COUNTER.size], frame[_COUNTER.size:]
        (counter,) = _COUNTER.unpack(header)
        # Authenticate FIRST, then update replay state (so forged frames cannot burn counters).
        try:
            plaintext = self._recv.decrypt(b"\0\0\0\0" + header, body, header)
        except InvalidTag:
            raise CryptoError("authentication failed") from None
        if counter <= self._last_recv:
            raise CryptoError("replayed or out-of-order frame")
        self._last_recv = counter
        return plaintext
