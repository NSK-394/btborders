"""Discovery parsers, config persistence, serial framing, RFCOMM error mapping."""

import json
import threading

import pytest

from btborders import config as cfgmod
from btborders.core import crypto, protocol as pr
from btborders.core.channel import SecureChannel
from btborders.core.transport import (BluetoothRFCOMMTransport, SerialTransport, TransportError,
                                      listen_rfcomm)
from btborders.discovery import (BtDevice, normalize_mac, parse_bluetoothctl,
                                 parse_system_profiler, parse_windows, pick_device)


# ---------------------------------------------------------------------------- discovery
def test_normalize_mac():
    assert normalize_mac("aa-bb-cc-dd-ee-ff") == "AA:BB:CC:DD:EE:FF"
    assert normalize_mac("123456789ABC") == "12:34:56:78:9A:BC"
    with pytest.raises(ValueError):
        normalize_mac("not a mac")


def test_parse_windows_many_dedup_and_single_object():
    many = json.dumps([
        {"FriendlyName": "Example Speaker",
         "InstanceId": "BTHENUM\\DEV_123456789ABC\\7&0000000&0&BLUETOOTHDEVICE_123456789ABC"},
        {"FriendlyName": "Example Speaker",
         "InstanceId": "BTHENUM\\DEV_123456789ABC\\other-service-entry"},
        {"FriendlyName": None, "InstanceId": "BTHENUM\\DEV_DEADBEEF0001\\x"},
        {"FriendlyName": "Not a device", "InstanceId": "USB\\VID_1234"},
    ])
    devs = parse_windows(many)
    assert devs == [BtDevice("12:34:56:78:9A:BC", "Example Speaker"),
                    BtDevice("DE:AD:BE:EF:00:01", "DE:AD:BE:EF:00:01")]
    one = json.dumps({"FriendlyName": "Laptop", "InstanceId": "BTHENUM\\DEV_001122334455\\x"})
    assert parse_windows(one) == [BtDevice("00:11:22:33:44:55", "Laptop")]
    assert parse_windows("") == []


def test_parse_bluetoothctl():
    text = "Device AA:BB:CC:DD:EE:FF My Laptop\nDevice 11:22:33:44:55:66 Phone X\nnoise\n"
    assert parse_bluetoothctl(text) == [BtDevice("AA:BB:CC:DD:EE:FF", "My Laptop"),
                                        BtDevice("11:22:33:44:55:66", "Phone X")]


def test_parse_system_profiler():
    data = {"SPBluetoothDataType": [{"device_connected": [
        {"MacBook": {"device_address": "AA-BB-CC-DD-EE-FF", "device_minorType": "Computer"}}]}]}
    assert parse_system_profiler(json.dumps(data)) == [BtDevice("AA:BB:CC:DD:EE:FF", "MacBook")]


def test_pick_device_by_number_mac_and_cancel():
    devs = [BtDevice("AA:BB:CC:DD:EE:FF", "A"), BtDevice("11:22:33:44:55:66", "B")]
    quiet = lambda *_: None  # noqa: E731
    assert pick_device(devs, lambda _: "2", quiet) == devs[1]
    assert pick_device(devs, lambda _: "aa-bb-cc-dd-ee-ff", quiet) == devs[0]
    assert pick_device(devs, lambda _: "", quiet) is None
    answers = iter(["9", "zzz", "1"])
    assert pick_device(devs, lambda _: next(answers), quiet) == devs[0]
    assert pick_device([], lambda _: "1", quiet) is None


# -------------------------------------------------------------------------------- config
def test_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("BTB_HOME", str(tmp_path))
    assert cfgmod.Config.load() == cfgmod.Config()  # defaults when no file
    c = cfgmod.Config(peer_mac="AA:BB:CC:DD:EE:FF", rfcomm_channel=7, receiver_edge="left",
                      hotkey_return="ctrl+shift+x", serial_port="C:\\weird \"port\"")
    path = c.save()
    assert path == tmp_path / "config.toml"
    assert cfgmod.Config.load() == c


def test_config_rejects_bad_edge(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[layout]\nreceiver_edge = "diagonal"\n')
    with pytest.raises(ValueError):
        cfgmod.Config.load(p)


def test_psk_file_roundtrip(tmp_path):
    psk = crypto.generate_psk()
    path = tmp_path / "sub" / "pairing.key"
    cfgmod.save_psk(psk, path)
    assert cfgmod.load_psk(path) == psk
    with pytest.raises(FileNotFoundError):
        cfgmod.load_psk(tmp_path / "missing.key")


# ------------------------------------------------------------------------------ transports
def test_serial_framing_and_secure_channel_over_loop_url():
    """pyserial's loop:// echoes writes back as reads, so one end can exercise framing."""
    t = SerialTransport("loop://")
    t.send(b"hello")
    t.send(b"x" * 3000)
    assert t.recv(timeout=2) == b"hello"
    assert t.recv(timeout=2) == b"x" * 3000
    t.close()


def test_serial_open_failure_is_transport_error():
    with pytest.raises(TransportError):
        SerialTransport("definitely-not-a-port")


def test_rfcomm_errors_are_transport_errors():
    assert isinstance(BluetoothRFCOMMTransport.available(), bool)
    try:
        listen_rfcomm(4).close()  # fine if an adapter is present...
    except TransportError as exc:
        assert "RFCOMM" in str(exc)  # ...otherwise a readable error, never a raw OSError
