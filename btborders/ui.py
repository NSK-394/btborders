"""Tiny rich TUI: connected?, latency, which computer currently owns the input."""

from __future__ import annotations

import threading

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .status import Status


def _unicode_ok(console: Console) -> bool:
    """Can this console's encoding show our symbols? (legacy Windows codepages cannot)"""
    try:
        "●○…".encode(console.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def render(status: Status, hotkey: str = "ctrl+alt+q", unicode_ok: bool = True) -> Panel:
    on, off, dots = ("●", "○", "…") if unicode_ok else ("*", "o", "...")
    s = status.snapshot()
    sender = s["role"] == "server"
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Role", "Sender (owns the mouse + keyboard)" if sender
                  else "Receiver (input is injected here)")
    table.add_row("Link", Text(f"{on} connected", style="green") if s["connected"]
                  else Text(f"{off} waiting for peer{dots}", style="yellow"))
    table.add_row("Peer", s["peer"] or "-")
    lat = s["latency_ms"]
    style = "green" if lat is not None and lat < 30 else "yellow"
    table.add_row("Latency", Text("-" if lat is None else f"{lat:.1f} ms", style=style))
    here = s["active"] == "local"
    table.add_row("Input is on", Text("THIS computer" if here else "the OTHER computer",
                                      style="bold green" if here else "bold magenta"))
    table.add_row("Packets", f"sent {s['events_sent']}  /  received {s['events_received']}")
    table.add_row("Status", s["message"] or "-")
    foot = Text(f"Emergency: {hotkey.upper()} returns control here instantly.  "
                "Ctrl+C quits.", style="dim")
    return Panel(Group(table, Text(""), foot), title="BTBorders", border_style="cyan")


def run_ui(status: Status, stop: threading.Event, hotkey: str = "ctrl+alt+q",
           console: Console | None = None) -> None:
    """Block, redrawing ~4x/second, until `stop` is set (or the user hits Ctrl+C)."""
    console = console or Console()
    uni = _unicode_ok(console)
    with Live(render(status, hotkey, uni), console=console, refresh_per_second=4) as live:
        while not stop.wait(0.25):
            live.update(render(status, hotkey, uni))
