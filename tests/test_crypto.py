import pytest

from btborders.core import crypto as c


def make_pair(psk=None, peer_psk=None):
    psk = psk or c.generate_psk()
    sn, cn = c.new_nonce(), c.new_nonce()
    server = c.Session(psk, "server", sn, cn)
    client = c.Session(peer_psk or psk, "client", cn, sn)
    return server, client


def test_roundtrip_both_directions():
    s, cl = make_pair()
    assert cl.open(s.seal(b"hello")) == b"hello"
    assert s.open(cl.seal(b"world")) == b"world"


def test_wrong_key_rejected():
    s, cl = make_pair(peer_psk=c.generate_psk())
    with pytest.raises(c.CryptoError):
        cl.open(s.seal(b"x"))


def test_tamper_rejected():
    s, cl = make_pair()
    frame = bytearray(s.seal(b"move 10 10"))
    frame[-1] ^= 1  # flip a bit in the tag
    with pytest.raises(c.CryptoError):
        cl.open(bytes(frame))
    frame = bytearray(s.seal(b"move 10 10"))
    frame[3] ^= 1  # tamper with the counter header (it is also authenticated data)
    with pytest.raises(c.CryptoError):
        cl.open(bytes(frame))


def test_replay_rejected():
    s, cl = make_pair()
    f = s.seal(b"click")
    cl.open(f)
    with pytest.raises(c.CryptoError):
        cl.open(f)


def test_out_of_order_rejected():
    s, cl = make_pair()
    f1, f2 = s.seal(b"1"), s.seal(b"2")
    cl.open(f2)
    with pytest.raises(c.CryptoError):
        cl.open(f1)


def test_forged_frame_does_not_burn_counter():
    s, cl = make_pair()
    good = s.seal(b"ok")
    forged = (2**40).to_bytes(8, "big") + b"\0" * 20
    with pytest.raises(c.CryptoError):
        cl.open(forged)
    assert cl.open(good) == b"ok"  # a huge forged counter did not poison replay state


def test_reflection_rejected():
    """A frame we sent must not be accepted if echoed back (directional keys)."""
    s, _ = make_pair()
    with pytest.raises(c.CryptoError):
        s.open(s.seal(b"echo"))


def test_session_keys_differ_between_connections():
    psk = c.generate_psk()
    s1, _ = make_pair(psk)
    _, cl2 = make_pair(psk)
    with pytest.raises(c.CryptoError):
        cl2.open(s1.seal(b"recorded in old session"))


def test_same_nonce_rejected():
    n = c.new_nonce()
    with pytest.raises(c.CryptoError):
        c.Session(c.generate_psk(), "server", n, n)


def test_short_frame():
    _, cl = make_pair()
    with pytest.raises(c.CryptoError):
        cl.open(b"abc")


def test_pairing_code_roundtrip_and_typo():
    psk = c.generate_psk()
    code = c.psk_to_code(psk)
    assert c.code_to_psk(code) == psk
    assert c.code_to_psk(code.lower().replace("-", " ")) == psk
    typo = ("A" if code[0] != "A" else "B") + code[1:]
    with pytest.raises(c.CryptoError):
        c.code_to_psk(typo)
    with pytest.raises(c.CryptoError):
        c.code_to_psk("TOOSHORT")
