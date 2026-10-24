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
