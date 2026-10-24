"""List the Bluetooth devices this computer is already paired with, and pick one.

Python's stdlib cannot enumerate paired devices, so we ask the OS:
  Windows  PowerShell Get-PnpDevice (the MAC is embedded in the instance id)
  Linux    `bluetoothctl devices Paired`   (BlueZ)
  macOS    `system_profiler SPBluetoothDataType -json`
Pairing itself is done in the OS Bluetooth settings (see README) - this module only reads.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

_MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


class DiscoveryError(Exception):
    pass


@dataclass(frozen=True)
class BtDevice:
    mac: str
    name: str


def normalize_mac(text: str) -> str:
    """'aa-bb-cc-dd-ee-ff' / 'AABBCCDDEEFF' / 'aa:bb:...' -> 'AA:BB:CC:DD:EE:FF'."""
    t = text.strip()
    if re.fullmatch(r"[0-9A-Fa-f]{12}", t):
        t = ":".join(t[i:i + 2] for i in range(0, 12, 2))
    if not _MAC_RE.match(t):
        raise ValueError(f"not a Bluetooth MAC address: {text!r}")
    return t.replace("-", ":").upper()


# -------------------------------------------------------------------------- parsers
def parse_windows(text: str) -> list[BtDevice]:
    """Input: JSON from `Get-PnpDevice ... | Select FriendlyName,InstanceId | ConvertTo-Json`."""
    text = text.strip()
    if not text:
        return []
    data = json.loads(text)
    if isinstance(data, dict):  # PowerShell emits a bare object when there is one result
        data = [data]
    seen: dict[str, BtDevice] = {}
    for item in data:
        m = re.search(r"DEV_([0-9A-Fa-f]{12})", item.get("InstanceId") or "")
        if m:
            mac = normalize_mac(m.group(1))
            seen.setdefault(mac, BtDevice(mac, (item.get("FriendlyName") or "").strip() or mac))
    return list(seen.values())


def parse_bluetoothctl(text: str) -> list[BtDevice]:
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*Device\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s*(.*)$", line)
        if m:
            out.append(BtDevice(m.group(1).upper(), m.group(2).strip() or m.group(1)))
    return out


def parse_system_profiler(text: str) -> list[BtDevice]:
    data = json.loads(text)
    out: list[BtDevice] = []

    def walk(node):
        if isinstance(node, dict):
            for key, val in node.items():
                if isinstance(val, dict) and "device_address" in val:
                    try:
                        out.append(BtDevice(normalize_mac(val["device_address"]), key))
                    except ValueError:
                        pass
                else:
                    walk(val)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out


# --------------------------------------------------------------------------- drivers
def _run(cmd: list[str]) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except FileNotFoundError as exc:
        raise DiscoveryError(f"{cmd[0]} not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DiscoveryError(f"{cmd[0]} timed out") from exc
    if res.returncode != 0:
        raise DiscoveryError(f"{cmd[0]} failed: {res.stderr.strip() or res.returncode}")
    return res.stdout


def list_paired() -> list[BtDevice]:
    """Paired devices known to this OS. Raises DiscoveryError if the OS tooling is missing."""
    if sys.platform == "win32":
        ps = ("Get-PnpDevice -Class Bluetooth | Where-Object { $_.InstanceId -like "
              "'BTHENUM\\DEV_*' } | Select-Object FriendlyName,InstanceId | ConvertTo-Json -Compress")
        return parse_windows(_run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps]))
    if sys.platform == "darwin":
        return parse_system_profiler(_run(["system_profiler", "SPBluetoothDataType", "-json"]))
    try:  # newer BlueZ
        return parse_bluetoothctl(_run(["bluetoothctl", "devices", "Paired"]))
    except DiscoveryError:
        return parse_bluetoothctl(_run(["bluetoothctl", "paired-devices"]))  # older BlueZ


def pick_device(devices: list[BtDevice], input_fn: Callable[[str], str] = input,
                print_fn: Callable[[str], None] = print) -> BtDevice | None:
    """Numbered menu. Returns None if the user cancels."""
    if not devices:
        print_fn("No paired Bluetooth devices found. Pair the other computer in your OS "
                 "Bluetooth settings first (see README).")
        return None
    for i, d in enumerate(devices, 1):
        print_fn(f"  {i}. {d.name}  [{d.mac}]")
    while True:
        answer = input_fn("Number of the other computer (or a MAC address, blank to cancel): ")
        answer = answer.strip()
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(devices):
            return devices[int(answer) - 1]
        try:
            mac = normalize_mac(answer)
            return next((d for d in devices if d.mac == mac), BtDevice(mac, mac))
        except ValueError:
            print_fn("Please enter a listed number or a valid MAC.")
