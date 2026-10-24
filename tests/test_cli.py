import pytest

from btborders import cli
from btborders.core import crypto
from btborders.config import Config, load_psk
from btborders.selftest import run_loopback


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BTB_HOME", str(tmp_path))
    return tmp_path


def test_loopback_selftest_passes():
    lines = []
    assert run_loopback(out=lines.append) is True
    assert "ALL CHECKS PASSED" in lines[-1]


def test_cli_test_loopback_exit_code(capsys):
    assert cli.main(["test-loopback"]) == 0
    assert "ALL CHECKS PASSED" in capsys.readouterr().out


def test_pair_generates_then_other_side_imports(home, capsys, monkeypatch):
    assert cli.main(["pair", "--no-pick", "--mac", "aa-bb-cc-dd-ee-ff", "--edge", "left"]) == 0
    out = capsys.readouterr().out
    psk = load_psk(Config.load().key_path)
    code = crypto.psk_to_code(psk)
    assert code in out
    assert Config.load().peer_mac == "AA:BB:CC:DD:EE:FF"
    assert Config.load().receiver_edge == "left"

    # "other computer": a different home directory importing the same code
    other = home / "other"
    monkeypatch.setenv("BTB_HOME", str(other))
    assert cli.main(["pair", "--code", code, "--no-pick"]) == 0
    assert load_psk(Config.load().key_path) == psk


def test_pair_rejects_mistyped_code(capsys):
    assert cli.main(["pair", "--code", "AAAA-BBBB", "--no-pick"]) == 2


def test_pair_does_not_overwrite_without_new(capsys):
    cli.main(["pair", "--no-pick"])
    first = load_psk(Config.load().key_path)
    cli.main(["pair", "--no-pick"])
    assert load_psk(Config.load().key_path) == first
    cli.main(["pair", "--no-pick", "--new"])
    assert load_psk(Config.load().key_path) != first


def test_status_runs_without_instance(capsys):
    assert cli.main(["status"]) == 0
    assert "no running instance" in capsys.readouterr().out


def test_serve_without_key_fails_cleanly(capsys):
    assert cli.main(["serve", "--no-ui"]) == 2
    assert "pairing key" in capsys.readouterr().err


def test_status_snapshot_and_file(tmp_path):
    import json
    from btborders.status import Status
    s = Status(role="client")
    s.update(connected=True, latency_ms=1.5)
    s.bump("events_sent", 3)
    snap = s.snapshot()
    assert snap["connected"] and snap["events_sent"] == 3 and "_lock" not in snap
    s.write(tmp_path / "s.json")
    assert json.loads((tmp_path / "s.json").read_text())["latency_ms"] == 1.5


# ---- regression: `btb connect` / `btb serve` crashed in logging setup ("'stream' or 'filename'
# should not be specified together with 'handlers'") on the default TUI path.
import logging

from btborders import cli as _cli


def _bare_root():
    """Empty the root logger NOW (pytest re-attaches its handlers when the test body starts,
    and basicConfig() only validates its arguments when the root has no handlers)."""
    logging.getLogger().handlers.clear()


@pytest.fixture
def fake_input(monkeypatch):
    """Run the REAL serve/connect startup code, but with mock devices instead of OS hooks."""
    from btborders import pynput_backend as pb
    from btborders.input_backend import MockSink, MockSource
    monkeypatch.setattr(pb, "PynputSource", lambda: MockSource())
    monkeypatch.setattr(pb, "PynputSink", lambda: MockSink())
    monkeypatch.setattr(pb, "prepare_process", lambda: None)
    # pytest pre-installs root handlers, and basicConfig() only validates its arguments when the
    # root logger has NONE - so start from a bare root, exactly like a real `btb` process.
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    root.handlers.clear()
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.handlers.extend(saved)
    root.setLevel(level)


@pytest.mark.parametrize("cmd", ["connect", "serve"])
@pytest.mark.parametrize("ui", [True, False])
def test_serve_and_connect_startup_do_not_crash(cmd, ui, fake_input, capsys):
    assert _cli.main(["pair", "--no-pick"]) == 0
    _bare_root()
    argv = [cmd, "--transport", "tcp", "--tcp-port", "0" if cmd == "serve" else "47991",
            "--run-for", "0.6"]
    if not ui:
        argv.append("--no-ui")
    assert _cli.main(argv) == 0
    assert "bye" in capsys.readouterr().out


@pytest.mark.parametrize("to_console", [True, False])
def test_setup_logging_is_idempotent(to_console, fake_input):
    _bare_root()
    root = logging.getLogger()
    before = len(root.handlers)
    for _ in range(3):
        _cli._setup_logging(verbose=True, to_console=to_console)
    assert len(root.handlers) == before + 1  # replaced, never stacked
    assert root.level == logging.DEBUG
    kind = logging.StreamHandler if to_console else logging.FileHandler
    assert isinstance(_cli._installed_handlers[0], kind)


def test_connect_help_and_serve_help(capsys):
    for cmd in ("connect", "serve"):
        with pytest.raises(SystemExit) as e:
            _cli.main([cmd, "--help"])
        assert e.value.code == 0
    assert "--run-for" not in capsys.readouterr().out  # hidden option stays hidden


def test_ui_renders_on_legacy_codepage_console():
    """Regression: '○' crashed the TUI when the console encoding was cp1252."""
    import io
    import threading
    from rich.console import Console
    from btborders.status import Status
    from btborders.ui import run_ui

    class Cp1252(io.TextIOWrapper):
        pass

    raw = io.BytesIO()
    console = Console(file=Cp1252(raw, encoding="cp1252", errors="strict"), force_terminal=True)
    stop = threading.Event()
    threading.Timer(0.4, stop.set).start()
    run_ui(Status(role="client"), stop, console=console)
    console.file.flush()
    assert b"waiting for peer" in raw.getvalue()
