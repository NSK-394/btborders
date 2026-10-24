# Building a single-file executable (PyInstaller)

Result: one `btb.exe` (Windows) or `btb` (Linux) you can copy to the other computer - no Python needed there.
PyInstaller does **not** cross-compile: build on Windows for Windows, on Linux for Linux.

## Windows

```powershell
uv sync                      # installs deps + pyinstaller (dev group)
uv run pyinstaller --onefile --name btb --collect-submodules pynput btb_entry.py --noconfirm
.\dist\btb.exe test-loopback # sanity check the frozen build (should end with ALL CHECKS PASSED)
```

## Linux

```bash
uv sync
uv run pyinstaller --onefile --name btb --collect-submodules pynput btb_entry.py --noconfirm
./dist/btb test-loopback
```

Without `uv`: `pip install . pyinstaller` then run the same `pyinstaller ...` command.

## Notes

* `btb_entry.py` is the entry point because the `btb` console-script created by pip does not exist inside a frozen app.
* `--collect-submodules pynput` makes sure the platform backend (`win32` / `xorg` / `darwin`) is bundled; pynput chooses it at import time, which PyInstaller's static analysis can miss.
* Expect roughly 15-20 MB. Antivirus tools sometimes flag *any* unsigned PyInstaller exe, and this program legitimately installs global keyboard/mouse hooks, so that is doubly likely. Build it yourself from this source rather than downloading one.
* Config and the pairing key live in the per-user config directory (see README), not next to the exe.
* Generated folders `build/`, `dist/` and `btb.spec` are git-ignored.
