# Security policy

BTBorders turns bytes received over a radio link into real keystrokes and mouse input, so
security bugs here can be serious. Thank you for reporting them responsibly.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately through GitHub: go to the repository's **Security** tab and click
**Report a vulnerability** (GitHub private vulnerability reporting). Include:

* what you found and why it matters (for example: input injection without the pairing key,
  replay or reordering accepted, key material leaked, crash from a crafted frame)
* steps or a proof of concept to reproduce it
* the BTBorders version, OS, and transport used (`bluetooth`, `serial`, `tcp`)

You should get a first reply within 7 days. Fixes are released as soon as practical, and you
will be credited in the release notes unless you ask not to be.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Scope

In scope: the pairing code and key handling (`btborders/core/crypto.py`, `btborders/config.py`),
the handshake and secure channel (`btborders/core/channel.py`), frame parsing
(`btborders/core/protocol.py`, `btborders/core/transport.py`), and input injection on the
receiver (`btborders/client.py`, `btborders/pynput_backend.py`).

Out of scope: radio jamming or denial of service by a nearby attacker, a compromised peer
computer, and a stolen `pairing.key`. These are documented limits in the README's security model.
