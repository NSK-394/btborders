"""config.toml in the per-user directory, plus the pairing key file.

Locations (override the whole directory with the BTB_HOME environment variable):
  Windows  %APPDATA%\\btborders
  macOS    ~/Library/Application Support/btborders
  Linux    $XDG_CONFIG_HOME/btborders  (default ~/.config/btborders)
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from .core import crypto
from .core.protocol import Edge

EDGE_NAMES = {"left": Edge.LEFT, "right": Edge.RIGHT, "top": Edge.TOP, "bottom": Edge.BOTTOM}


def config_dir() -> Path:
    override = os.environ.get("BTB_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "btborders"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "btborders"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "btborders"


def config_path() -> Path:
    return config_dir() / "config.toml"


# (section, key) for each field, so the TOML file stays grouped and readable.
_SECTIONS = {
    "peer_mac": ("peer", "mac"),
    "transport_kind": ("transport", "kind"),
    "rfcomm_channel": ("transport", "rfcomm_channel"),
    "serial_port": ("transport", "serial_port"),
    "serial_baud": ("transport", "serial_baud"),
    "tcp_host": ("transport", "tcp_host"),
    "tcp_port": ("transport", "tcp_port"),
    "receiver_edge": ("layout", "receiver_edge"),
    "hotkey_return": ("hotkeys", "return_local"),
    "key_file": ("security", "key_file"),
}


@dataclass
class Config:
    peer_mac: str = ""                    # Bluetooth MAC of the other machine
    transport_kind: str = "bluetooth"     # bluetooth | serial | tcp
    rfcomm_channel: int = 4               # RFCOMM channel (1-30)
    serial_port: str = ""                 # e.g. COM5 or /dev/rfcomm0
    serial_baud: int = 115200
    tcp_host: str = "127.0.0.1"           # tcp is for testing only
    tcp_port: int = 47474
    receiver_edge: str = "right"          # which side of the SENDER's screen the receiver is on
    hotkey_return: str = "ctrl+alt+q"     # emergency "give me my input back" hotkey
    key_file: str = ""                    # defaults to <config dir>/pairing.key

    @property
    def edge(self) -> Edge:
        try:
            return EDGE_NAMES[self.receiver_edge.lower()]
        except KeyError:
            raise ValueError(f"layout.receiver_edge must be one of {sorted(EDGE_NAMES)}") from None

    @property
    def key_path(self) -> Path:
        return Path(self.key_file) if self.key_file else config_dir() / "pairing.key"

    # -- persistence ------------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        cfg = cls()
        if not path.exists():
            return cfg
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        for f in fields(cls):
            section, key = _SECTIONS[f.name]
            if key in data.get(section, {}):
                setattr(cfg, f.name, type(getattr(cfg, f.name))(data[section][key]))
        cfg.edge  # validate early
        return cfg

    def save(self, path: Path | None = None) -> Path:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        out: dict[str, list[str]] = {}
        for f in fields(self):
            section, key = _SECTIONS[f.name]
            val = getattr(self, f.name)
            text = str(val).lower() if isinstance(val, bool) else (
                str(val) if isinstance(val, int) else '"' + str(val).replace("\\", "\\\\")
                .replace('"', '\\"') + '"')
            out.setdefault(section, []).append(f"{key} = {text}")
        body = "".join(f"[{s}]\n" + "\n".join(lines) + "\n\n" for s, lines in out.items())
        path.write_text(body, encoding="utf-8", newline="\n")
        return path


# ----------------------------------------------------------------------- key storage
def save_psk(psk: bytes, path: Path) -> None:
    """Store the PSK as its pairing code. File is created owner-only (0600) where the OS
    supports it; on Windows it inherits the user-profile ACL (private to your account)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(crypto.psk_to_code(psk) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_psk(path: Path) -> bytes:
    if not path.exists():
        raise FileNotFoundError(f"no pairing key at {path} - run `btb pair` first")
    return crypto.code_to_psk(path.read_text(encoding="utf-8"))
