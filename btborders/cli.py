"""`btb` command line.

  btb pair            make (or import) the shared secret and choose the peer's Bluetooth MAC
  btb devices         list paired Bluetooth devices
  btb serve           run on the computer WITH the physical mouse/keyboard (sender)
  btb connect         run on the computer that should RECEIVE input (receiver)
  btb status          show config and the last-known state of a running instance
  btb test-loopback   try the whole flow on one machine, no Bluetooth needed
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time

from . import config as cfgmod
from . import discovery
from .core import crypto
from .core.transport import TransportError, make_acceptor
from .status import Status


# ------------------------------------------------------------------------------ helpers
def _add_link_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("link overrides (otherwise config.toml is used)")
    g.add_argument("--transport", choices=["bluetooth", "serial", "tcp"])
    g.add_argument("--mac", help="peer Bluetooth MAC address")
    g.add_argument("--channel", type=int, help="RFCOMM channel (1-30)")
    g.add_argument("--serial-port", help="e.g. COM5 or /dev/rfcomm0")
    g.add_argument("--tcp-port", type=int, help="port for the (testing-only) tcp transport")
    g.add_argument("--edge", choices=sorted(cfgmod.EDGE_NAMES),
                   help="which side of the SENDER's screen the receiver is on")
    p.add_argument("--no-ui", action="store_true", help="plain log output instead of the TUI")
    p.add_argument("--run-for", type=float, help=argparse.SUPPRESS)  # exit after N seconds (tests)
    p.add_argument("-v", "--verbose", action="store_true")


def _load_config(args) -> cfgmod.Config:
    cfg = cfgmod.Config.load()
    for attr, val in (("transport_kind", getattr(args, "transport", None)),
                      ("peer_mac", getattr(args, "mac", None)),
                      ("rfcomm_channel", getattr(args, "channel", None)),
                      ("serial_port", getattr(args, "serial_port", None)),
                      ("tcp_port", getattr(args, "tcp_port", None)),
                      ("receiver_edge", getattr(args, "edge", None))):
        if val is not None:
            setattr(cfg, attr, discovery.normalize_mac(val) if attr == "peer_mac" else val)
    cfg.edge  # validate
    return cfg


_LOG_FORMAT = "%(asctime)s %(name)s: %(message)s"
_installed_handlers: list[logging.Handler] = []


def _setup_logging(verbose: bool, to_console: bool) -> None:
    """Configure the root logger. Idempotent: handlers we installed earlier are replaced, never
    stacked, so calling serve/connect twice cannot duplicate log lines.

    Handlers are built explicitly (never basicConfig(stream=..., handlers=...) - that
    combination raises ValueError). With the TUI active, console output would corrupt the
    display, so logs go to <config dir>/btb.log instead.
    """
    root = logging.getLogger()
    for h in _installed_handlers:
        root.removeHandler(h)
        h.close()
    _installed_handlers.clear()

    if to_console:
        handler: logging.Handler = logging.StreamHandler(sys.stderr)
    else:
        try:
            path = cfgmod.config_dir() / "btb.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(path, encoding="utf-8")
        except OSError:
            handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(handler)
    _installed_handlers.append(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def _status_path(role: str):
    return cfgmod.config_dir() / f"status-{role}.json"


def _show_pairing_code(code: str, out=print) -> None:
    out("\nPairing code (type or scan this on the OTHER computer):\n")
    out(f"    {code}\n")
    try:
        import qrcode  # optional extra: pip install btborders[qr]
        qr = qrcode.QRCode(border=1)
        qr.add_data(code)
        qr.print_ascii(invert=True)
    except ImportError:
        out("(install the optional 'qr' extra to also show a QR code)")


# --------------------------------------------------------------------------------- pair
def cmd_pair(args) -> int:
    cfg = cfgmod.Config.load()
    key_path = cfg.key_path
    if args.code:
        try:
            psk = crypto.code_to_psk(args.code)
        except crypto.CryptoError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        cfgmod.save_psk(psk, key_path)
        print(f"Pairing key imported and saved to {key_path}")
    elif key_path.exists() and not args.new:
        print(f"A pairing key already exists at {key_path}.")
        _show_pairing_code(crypto.psk_to_code(cfgmod.load_psk(key_path)))
        print("(use --new to replace it with a fresh one)")
    else:
        psk = crypto.generate_psk()
        cfgmod.save_psk(psk, key_path)
        print(f"New pairing key saved to {key_path}")
        _show_pairing_code(crypto.psk_to_code(psk))
        print("On the other computer run:  btb pair --code <the code above>")

    # Choose which Bluetooth device is "the other computer".
    if args.mac:
        cfg.peer_mac = discovery.normalize_mac(args.mac)
    elif not args.no_pick and cfg.transport_kind == "bluetooth" and sys.stdin.isatty():
        try:
            dev = discovery.pick_device(discovery.list_paired())
        except discovery.DiscoveryError as exc:
            print(f"Could not list Bluetooth devices ({exc}). Re-run with --mac AA:BB:..")
            dev = None
        if dev:
            cfg.peer_mac = dev.mac
    if args.edge:
        cfg.receiver_edge = args.edge
    path = cfg.save()
    print(f"Config saved to {path}" + (f"  (peer {cfg.peer_mac})" if cfg.peer_mac else ""))
    return 0


def cmd_devices(args) -> int:
    try:
        devs = discovery.list_paired()
    except discovery.DiscoveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not devs:
        print("No paired Bluetooth devices found.")
    for d in devs:
        print(f"{d.mac}  {d.name}")
    return 0


# ------------------------------------------------------------------------- serve/connect
def _run_engine(args, role: str) -> int:
    from . import pynput_backend as pb
    from .clipboard import ClipboardSync, PyperclipClipboard  # noqa: F401
    from .ui import run_ui

    cfg = _load_config(args)
    _setup_logging(args.verbose, to_console=args.no_ui)
    try:
        psk = cfgmod.load_psk(cfg.key_path)
    except (FileNotFoundError, crypto.CryptoError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    warn = pb.session_warning()
    if warn:
        print(f"warning: {warn}", file=sys.stderr)

    pb.prepare_process()
    status = Status(role=role)
    stop = threading.Event()
    clipboard = PyperclipClipboard()

    if role == "server":
        from .server import Server
        engine = Server(pb.PynputSource(), cfg, clipboard, status)
        try:
            acceptor = make_acceptor(cfg)
        except TransportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        engine.start_capture()
        status.update(message=f"listening on {acceptor.desc}")
        target = lambda: engine.serve_forever(acceptor, psk, stop)  # noqa: E731
        cleanup = lambda: (engine.stop_capture(), acceptor.close())  # noqa: E731
    else:
        from .client import Client
        engine = Client(pb.PynputSink(), cfg, clipboard, status)
        status.update(message=f"dialling {cfg.peer_mac or cfg.serial_port or cfg.tcp_host}…")
        target = lambda: engine.run_forever(  # noqa: E731
            psk, stop, on_wait=lambda e, w: status.update(message=f"{e} - retry in {w:.1f}s"))
        cleanup = lambda: None  # noqa: E731

    if args.run_for:
        threading.Timer(args.run_for, stop.set).start()
    worker = threading.Thread(target=target, daemon=True, name="btb-engine")
    worker.start()
    persist = threading.Thread(target=_persist_status, args=(status, role, stop), daemon=True)
    persist.start()
    try:
        if args.no_ui:
            last = ""
            while not stop.wait(1.0):
                line = json.dumps({k: v for k, v in status.snapshot().items() if k != "updated"})
                if line != last:
                    print(line, flush=True)
                    last = line
        else:
            run_ui(status, stop, cfg.hotkey_return)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        cleanup()          # releases suppression + any held input before we exit
        worker.join(timeout=3)
        _status_path(role).unlink(missing_ok=True)
    print("bye")
    return 0


def _persist_status(status: Status, role: str, stop: threading.Event) -> None:
    while not stop.wait(1.0):
        status.write(_status_path(role))


def cmd_serve(args) -> int:
    return _run_engine(args, "server")


def cmd_connect(args) -> int:
    return _run_engine(args, "client")


# -------------------------------------------------------------------------------- status
def cmd_status(args) -> int:
    cfg = cfgmod.Config.load()
    print(f"config file : {cfgmod.config_path()}  ({'found' if cfgmod.config_path().exists() else 'not created yet'})")
    print(f"pairing key : {cfg.key_path}  ({'present' if cfg.key_path.exists() else 'MISSING - run btb pair'})")
    print(f"transport   : {cfg.transport_kind}  peer={cfg.peer_mac or '-'}  channel={cfg.rfcomm_channel}")
    print(f"layout      : receiver is on the {cfg.receiver_edge.upper()} of the sender's screen")
    print(f"hotkey      : {cfg.hotkey_return}")
    found = False
    for role in ("server", "client"):
        path = _status_path(role)
        if not path.exists():
            continue
        try:
            s = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        age = time.time() - s.get("updated", 0)
        found = True
        lat = s.get("latency_ms")
        print(f"\nrunning {role}: {'CONNECTED' if s['connected'] else 'not connected'}"
              f" | latency {'-' if lat is None else f'{lat:.1f} ms'}"
              f" | input on {'this computer' if s['active'] == 'local' else 'the other computer'}"
              f" | {s['message']} (updated {age:.0f}s ago)")
    if not found:
        print("\nno running instance found (start one with `btb serve` or `btb connect`)")
    return 0


def cmd_loopback(args) -> int:
    from .selftest import run_loopback
    if args.live:
        print("LIVE mode: your real cursor will be moved a few hundred pixels. Starting in 2s…")
        time.sleep(2)
    return 0 if run_loopback(live=args.live) else 1


# ---------------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="btb", description="Share one mouse and keyboard between "
                                "two computers over Bluetooth.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("pair", help="create/import the shared secret, choose the peer device")
    pp.add_argument("--code", help="import the pairing code shown by the other computer")
    pp.add_argument("--new", action="store_true", help="replace any existing key")
    pp.add_argument("--mac", help="peer Bluetooth MAC (skips the device menu)")
    pp.add_argument("--no-pick", action="store_true", help="do not offer the device menu")
    pp.add_argument("--edge", choices=sorted(cfgmod.EDGE_NAMES))
    pp.set_defaults(fn=cmd_pair)

    sub.add_parser("devices", help="list paired Bluetooth devices").set_defaults(fn=cmd_devices)

    ps = sub.add_parser("serve", help="SENDER: the computer with the physical mouse/keyboard")
    _add_link_options(ps)
    ps.set_defaults(fn=cmd_serve)

    pc = sub.add_parser("connect", help="RECEIVER: the computer that gets the input")
    _add_link_options(pc)
    pc.set_defaults(fn=cmd_connect)

    sub.add_parser("status", help="show config and running state").set_defaults(fn=cmd_status)

    pl = sub.add_parser("test-loopback", help="self-test on one machine, no Bluetooth")
    pl.add_argument("--live", action="store_true",
                    help="also move your REAL cursor on the receiving side")
    pl.set_defaults(fn=cmd_loopback)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (ValueError, TransportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
