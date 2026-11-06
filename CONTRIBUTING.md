# Contributing

Bug reports, platform test results, and pull requests are welcome.

## Setup and tests

```bash
uv sync                 # or: pip install -e . pytest
uv run pytest           # full suite, no Bluetooth or real input needed
uv run btb test-loopback
```

All tests must pass on Windows and Linux before a PR is merged (CI runs both).

## Rules for tests

* Tests never install real keyboard/mouse hooks and never inject real input.
  Use the mock `InputSource` / `InputSink` in `btborders/input_backend.py`.
* End-to-end tests run `Server` and `Client` over `TCPLoopbackTransport`, not Bluetooth.
* Anything that needs real hardware or a display must skip itself cleanly
  (`pytest.importorskip`, `pytest.mark.skipif`) so CI stays green on headless runners.
* Never put real Bluetooth MAC addresses, pairing codes, or keys in tests or docs.
  Use placeholders like `AA:BB:CC:DD:EE:FF`.

## Code style

* PEP 8, type hints on public functions, small focused modules.
* Capture callbacks must never block on the network: they only enqueue packets.
* Any change to the protocol or crypto needs tests for the failure paths
  (tampering, replay, wrong key, oversized frames), not just the happy path.

## Platform reports

If you run BTBorders on a setup not listed in the README's tested platforms table
(for example macOS), please open an issue with what worked and what did not.
