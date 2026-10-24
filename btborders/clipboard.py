"""Clipboard text sync (v1: text only, size-capped, no files)."""

from __future__ import annotations

from .core.protocol import MAX_CLIPBOARD_BYTES


class MemoryClipboard:
    """In-memory stand-in used by tests and the loopback self-test."""

    def __init__(self, text: str = ""):
        self.text = text

    def get(self) -> str:
        return self.text

    def set(self, text: str) -> None:
        self.text = text


class PyperclipClipboard:
    """The real system clipboard via pyperclip. Failures never break input sharing."""

    def get(self) -> str:
        import pyperclip
        try:
            return pyperclip.paste() or ""
        except Exception:
            return ""

    def set(self, text: str) -> None:
        import pyperclip
        try:
            pyperclip.copy(text)
        except Exception:
            pass


class ClipboardSync:
    """Decides when clipboard text should travel, and avoids echo loops."""

    def __init__(self, backend):
        self.backend = backend
        # None = nothing synced yet, so what you copied *before* the first crossing travels too.
        self._last: str | None = None

    def outgoing(self) -> str | None:
        """Text to send if the clipboard changed since we last synced, else None."""
        text = self.backend.get()
        if text == self._last or not text:
            return None
        if len(text.encode("utf-8")) > MAX_CLIPBOARD_BYTES:
            self._last = text  # too big: skip it, but do not retry on every switch
            return None
        self._last = text
        return text

    def incoming(self, text: str) -> None:
        if len(text.encode("utf-8")) > MAX_CLIPBOARD_BYTES:
            return
        self._last = text  # remember it so we do not bounce it straight back
        self.backend.set(text)
