# BTBorders

Share **one mouse and keyboard between two computers over Bluetooth** - a Mouse-without-Borders
style tool that needs no Wi-Fi and no router. Push the cursor off the edge of computer A's
screen and it appears on computer B; typing follows the cursor; copied text follows too.

```
 SENDER (has the real mouse + keyboard)              RECEIVER
 btb serve                                           btb connect
 ┌────────────┐   Bluetooth RFCOMM, AES-GCM          ┌────────────┐
 │  screen A  │ ───────── encrypted frames ───────▶  │  screen B  │
 │        ──▶ │ ◀──────── heartbeat / leave ───────  │ ◀──        │
 └────────────┘                                      └────────────┘
```

* **Encrypted + authenticated** - every packet is AES-GCM with a key only your two machines share.
* **Relative mouse deltas** - different resolutions just work; the cursor lines up proportionally along the edge.
* **Safe by default** - emergency hotkey (**Ctrl+Alt+Q**) and automatic return if the link drops (2 s without a heartbeat). Held keys/buttons are always released.
* **Low latency** - mouse moves are coalesced to ≤125 Hz, packets are 5 bytes + 24 bytes of crypto overhead.
* **Clipboard text** (≤64 KB) travels when the cursor crosses. No files in v1.

> **Status / honesty note.** All logic is covered by 97 automated tests, including a full
> sender↔receiver run over an encrypted loopback link, and the real Windows input hooks were
> smoke-tested. The Bluetooth RFCOMM transport itself, and the Linux/macOS input backends, have
> **not** been verified against two real computers by the author - use the
> [manual test checklist](#manual-test-checklist) on your hardware and report/fix what you find.

---

## Contents
1. [Install](#install) · 2. [Try it on one machine](#try-it-on-one-machine-no-bluetooth) · 3. [Pairing](#pairing-two-computers)
4. [Run: Windows](#run-windows) · 5. [Run: Linux](#run-linux) · 6. [Configuration](#configuration)
7. [Troubleshooting](#troubleshooting) · 8. [Security model](#security-model)
9. [How it works](#how-it-works) · 10. [Manual test checklist](#manual-test-checklist) · 11. [Roadmap](#roadmap) · 12. [Development](#development)

## Install

Needs Python 3.11+ on both computers (or use the [single-file exe](build.md)).

```bash
git clone <this repo> && cd btborders
uv sync                 # or: pip install .
uv run btb --help       # or just: btb --help   (after pip install)
```
Optional QR code for pairing: `pip install ".[qr]"`.

Linux also needs Xlib bits that pynput pulls in, plus a clipboard tool for pyperclip: `sudo apt install xclip` (or `xsel`).

## Try it on one machine (no Bluetooth)

```bash
btb test-loopback          # safe: scripted input, nothing real is moved or typed
btb test-loopback --live   # also moves your REAL cursor on the receiving side
```
You should see every line `PASS`, ending in `ALL CHECKS PASSED`, plus a loopback latency figure.

## Pairing two computers

Pairing has **two independent parts**:

1. **OS Bluetooth pairing** - the operating systems must trust each other (done once, in OS settings).
2. **BTBorders pairing** - a shared secret so BTBorders accepts only *your* other computer (done once with `btb pair`).

Call the computer with the physical mouse/keyboard the **sender** (`btb serve`) and the other the **receiver** (`btb connect`).

### 1. Pair in the OS Bluetooth settings

**Windows 10/11 ↔ anything:** Settings → Bluetooth & devices → *Add device* → *Bluetooth*. Pick the other computer, confirm the matching PIN on both. Make sure the other computer is "discoverable" (Linux: `bluetoothctl discoverable on`).

**Linux:**
```bash
bluetoothctl
  power on
  agent on
  default-agent
  scan on                       # wait for the other computer to show up
  pair AA:BB:CC:DD:EE:FF
  trust AA:BB:CC:DD:EE:FF       # lets it reconnect without prompts
  quit
```

### 2. Find the other computer's MAC address

* **Easiest:** `btb devices` lists every paired device with its MAC (works on Windows, Linux, macOS), and `btb pair` offers it as a numbered menu.
* Windows manually: Settings → Bluetooth & devices → Devices → *More Bluetooth settings* → Hardware tab, or Device Manager → Bluetooth → device → Properties → Details → *Bluetooth device address*. (Also: on the *other* computer, Device Manager → your Bluetooth adapter → Properties → Advanced → Address.)
* Linux manually: `bluetoothctl devices` (or `hciconfig` on the computer itself to see its own address).

### 3. Pair BTBorders

On the **sender**:
```bash
btb pair --edge right
```
It creates the secret, shows a **pairing code** (and a QR if installed), and lets you choose the receiver from your paired devices. `--edge` says which side of the *sender's* screen the receiver sits on (`left`, `right`, `top`, `bottom`).

On the **receiver**, type the code in:
```bash
btb pair --code ABCD-EFGH-...   # then choose the sender in the device menu
```
A mistyped code is rejected (it has a built-in checksum). The secret is stored in your config directory as `pairing.key`.

## Run: Windows

1. Both computers: Bluetooth on, OS-paired, `btb pair` done.
2. **Sender**: open PowerShell → `btb serve`
   (listens on RFCOMM channel 4; a TUI shows link state, latency and which computer has the input).
3. **Receiver**: `btb connect`
   (dials the sender; if it isn't there yet it retries with exponential backoff: 0.5 s, 1 s, 2 s … 15 s).
4. Wait for **● connected** on both, then slide the cursor off the configured edge of the sender's screen.
5. Slide back over the opposite edge on the receiver to return, or press **Ctrl+Alt+Q** on the sender at any time.

Windows specifics:
* **DPI scaling** - the program opts in to per-monitor DPI awareness so coordinates are physical pixels on both ends; no per-machine tweaking needed.
* **Hooks & suppression** - uses `WH_MOUSE_LL` / `WH_KEYBOARD_LL`. While controlling the other computer, local input is blocked and the local cursor is parked at screen centre; deltas are measured from there, so you never run out of screen.
* **Elevated (administrator) windows ignore injected input** (Windows UIPI). If the *receiver* shows a UAC prompt, Task Manager, or an app running "as administrator", clicks/keys won't reach it. Fix: run `btb connect` from an *elevated* PowerShell on the receiver. (Likewise, to capture input while an elevated window is focused on the *sender*, run `btb serve` elevated.) The lock screen / Ctrl+Alt+Del / UAC secure desktop cannot be controlled at all.
* Only the **primary monitor** is used for edge detection in v1.
* Windows Defender / antivirus may ask about the program because it installs global hooks. That is expected.

## Run: Linux

1. Log in with an **X11 / Xorg** session (see Wayland below). Bluetooth on, OS-paired, `btb pair` done.
2. Sender: `btb serve` · Receiver: `btb connect` (same flow as Windows).
3. If Bluetooth RFCOMM sockets need privileges on your distro, check `bluetoothctl show` (Powered: yes) and that your user is allowed to use Bluetooth; as a last resort `sudo setcap` is *not* needed on mainstream distros.

Linux specifics:
* **X11 via pynput** (XRecord to capture, XTest to inject). To suppress local input while remote, a pair of grabbing listeners is started on demand (pointer + keyboard grab) and released on return.
* **Wayland is not supported** for global capture/injection: the protocol deliberately forbids ordinary apps from doing either. Options: (a) choose "Xorg"/"X11" at the login screen; (b) the planned evdev/uinput fallback - read `/dev/input/event*` with `evdev` (grab the device with `EVIOCGRAB` to suppress) and inject with `/dev/uinput`. It needs the user in the `input` group (or root) and is on the [roadmap](#roadmap). On the *receiver* side only, running under XWayland sometimes works for X11 apps but not for native Wayland windows.
* On a Linux **receiver**, install `xclip` for clipboard sync.

macOS is best-effort: grant the terminal *Accessibility* and *Input Monitoring* permission; CPython from python.org often lacks `AF_BLUETOOTH` - use the serial transport (`--transport serial --serial-port /dev/cu.<device>`).

### Serial fallback (Windows Bluetooth COM ports, `/dev/rfcomm0`)

If raw RFCOMM sockets misbehave on your setup, use a serial port instead:
```bash
btb serve   --transport serial --serial-port COM5
btb connect --transport serial --serial-port COM6
```
Windows: Settings → Bluetooth → *More Bluetooth settings* → **COM Ports** tab → *Add…* shows/creates the ports. Linux: `sudo rfcomm bind 0 AA:BB:CC:DD:EE:FF 4` creates `/dev/rfcomm0`. The encryption layer is identical on every transport.

## Configuration

`btb pair` writes `config.toml` in:
Windows `%APPDATA%\btborders` · Linux `~/.config/btborders` · macOS `~/Library/Application Support/btborders` (override with `BTB_HOME`).

```toml
[peer]
mac = "AA:BB:CC:DD:EE:FF"
[transport]
kind = "bluetooth"          # bluetooth | serial | tcp (tcp = testing only)
rfcomm_channel = 4
serial_port = ""
[layout]
receiver_edge = "right"     # side of the SENDER's screen where the receiver sits
[hotkeys]
return_local = "ctrl+alt+q"
[security]
key_file = ""               # default: <config dir>/pairing.key
```
Every setting can be overridden per run (`btb serve --edge left --channel 5`). `btb status` shows the config and the last-known state of a running instance.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `cannot listen on RFCOMM channel 4` | Bluetooth is off / no adapter / channel in use. Turn Bluetooth on; try `--channel 5`. |
| Receiver loops on "retry in …s" | Sender isn't running (`btb serve` first), OS pairing missing, wrong `peer.mac`, or the two sides use different `--channel`. Check `btb devices`. |
| Connects, then immediately "rejected peer" / "handshake failed" | Different pairing secrets. Re-run `btb pair --new` on the sender and `btb pair --code …` on the receiver. |
| Cursor reaches the edge but nothing happens | Receiver not **● connected** yet; wrong `--edge`; on multi-monitor, the edge is the *primary* monitor's edge. |
| Clicks/keys ignored in some receiver windows | Those windows are elevated: run `btb connect` as administrator (Windows UIPI). |
| Cursor lands in the wrong spot on a scaled display | Make sure you run the real `btb` (it sets DPI awareness); don't embed it in a non-DPI-aware host. |
| Laggy cursor | Check the Latency line (target < 30 ms). Move closer, avoid 2.4 GHz Wi-Fi congestion, close other Bluetooth audio streaming (it shares the radio). |
| Stuck modifier key on either machine | Press Ctrl+Alt+Q, then tap the key. Report it - releasing held keys on every mode change is a design goal. |
| Linux: nothing is captured | You're on Wayland. See [Run: Linux](#run-linux). |
| Linux: `Xlib.error` / no DISPLAY | Run inside the graphical session, not over plain SSH. |
| Need to see what's happening | `btb serve --no-ui -v` prints log lines and a JSON status stream. |

## Security model

**Why it matters:** this program turns bytes from a radio link into keystrokes. Anyone who could inject packets could type commands on your computer. So security is not optional here.

* **Trust anchor = a 256-bit pre-shared key (PSK)** generated by `btb pair` with the OS CSPRNG and entered on the second machine via a short code (base32, with checksum). It never travels over the air; Bluetooth OS pairing is treated as *discovery/transport only*, not as trust.
* **Per-connection keys.** Each side sends a random 16-byte nonce; keys are derived with **HKDF-SHA256**(PSK, server_nonce‖client_nonce), one key per direction. Old recordings can't be replayed into new sessions, and a packet can't be reflected back at its sender.
* **AES-256-GCM** authenticated encryption on every frame. The counter is the GCM nonce *and* authenticated data; the receiver requires a **strictly increasing** counter, rejecting replays, reordering and tampering. A forged frame can't advance the counter (authentication happens first).
* **Unauthenticated peers are dropped.** A new connection must produce a valid encrypted `Hello` within 5 s or it is closed; before that, only ≤64-byte frames are accepted, so strangers can't make us allocate memory. Any later authentication failure ends the connection and returns control to the local machine.
* **Defence in depth on the receiver.** It ignores input packets unless a `switch(enter)` is active, clamps clipboard size (64 KB), and releases every held key/button when control ends for any reason.
* **Fail-safe.** Ctrl+Alt+Q, a missed heartbeat (2 s), a dropped connection, or program exit all unconditionally restore local control and unsuppress input.
* **At rest.** `pairing.key` is created `0600` (owner-only) on Linux/macOS; on Windows it inherits your user-profile ACL. Treat it like an SSH private key; don't commit or sync it. Regenerate with `btb pair --new` if exposed.

Known limits (be aware, not afraid): there is no forward secrecy across sessions beyond the per-session keys (a stolen PSK lets an attacker authenticate *and* decrypt recordings of future sessions they capture); a nearby attacker can still **deny service** (jam the radio or hold the single connection slot - the 5 s handshake timeout bounds this); clipboard contents (including passwords you copy) cross the link when you switch - encrypted, but they do land on the other computer; anything you type goes to whichever computer has the cursor, so a compromised receiver can read your keystrokes. The program has not had an independent security audit.

## How it works

```
btborders/
  core/protocol.py   packet types + struct codecs (move, button, scroll, key, clipboard, switch, heartbeat, hello)
  core/crypto.py     PSK <-> pairing code, HKDF key schedule, AES-GCM Session (counter, replay checks)
  core/transport.py  length-prefixed frames over TCP-loopback / Bluetooth RFCOMM / serial; backoff helpers
  core/channel.py    transport + handshake + crypto + codec = SecureChannel
  core/edge.py       edge detection, proportional position mapping, receiver-side VirtualCursor
  core/coalesce.py   125 Hz mouse-move coalescer
  input_backend.py   InputSource/InputSink interfaces + mocks (tests)
  pynput_backend.py  real capture/injection (Windows hooks, X11, macOS)
  server.py          SENDER engine (modes LOCAL/REMOTE, hotkey, watchdog)
  client.py          RECEIVER engine (virtual cursor, release-all, watchdog)
  discovery.py       list paired Bluetooth devices per OS
  config.py          config.toml + key file      ui.py  rich TUI      cli.py  `btb` commands
```
Key design decisions:
* **Relative deltas + cursor parking.** In REMOTE mode the sender measures `dx = x - centre`, then warps the cursor back to the centre, so deltas never hit a screen edge.
* **Hooks never touch the network.** Capture callbacks only enqueue packets; a writer thread sends. A stalled Bluetooth link therefore can't freeze your local input or the emergency hotkey.
* **Key mapping.** Keys travel as layout-independent names (`char:a`, `key:ctrl_l`); a raw virtual-key code rides along and is used only when both computers run the same OS, because Unicode-injected characters don't trigger shortcuts like Ctrl+C.
* **Held-key hygiene.** If you hold Ctrl while crossing, its release happens while input is suppressed; on return the backend releases such keys locally so nothing sticks.

## Manual test checklist

Run these on your two real computers (sender = A, receiver = B). ☐ = tick when verified.

**Setup**
- ☐ `btb test-loopback` passes on both computers.
- ☐ A and B paired in OS Bluetooth settings; `btb devices` lists the other one.
- ☐ `btb pair` on A shows a code; `btb pair --code …` on B accepts it; a deliberately mistyped code is rejected.
- ☐ `btb status` shows the config and key on both.

**Connection**
- ☐ `btb serve` on A, `btb connect` on B → both show ● connected; latency shown (target < 30 ms).
- ☐ Start `btb connect` *before* `btb serve`: it retries, then connects by itself.
- ☐ Switch off Bluetooth on B briefly → A shows disconnected; turn it back on → reconnects automatically.
- ☐ Run `btb connect` with a different pairing key → A logs "rejected peer", never enters remote mode.

**Input**
- ☐ Cursor leaves A's configured edge → appears on B at the proportional height.
- ☐ Mouse movement on B is smooth; left/right/middle click and scroll wheel work.
- ☐ Typing letters, digits, Shift/Caps, Enter, Backspace, arrows, F-keys on B work; Ctrl+C / Ctrl+V on B work.
- ☐ Different keyboard layouts on A and B: typed characters are correct.
- ☐ While on B, A's own screen does not react to the mouse or keyboard.
- ☐ Push the cursor back over B's entry edge → control returns to A at the matching spot; A's mouse works again.
- ☐ Hold Ctrl on A while crossing, release after → no stuck Ctrl on either machine.
- ☐ Drag a selection / hold a mouse button, then press **Ctrl+Alt+Q** → control returns instantly and B's button is released.

**Failure handling**
- ☐ Walk out of Bluetooth range / disable Bluetooth on A or B while controlling B → within ~2 s A's input returns; no stuck keys on B.
- ☐ Kill `btb connect` on B while controlling it → A recovers within ~2 s.
- ☐ Ctrl+C on A while remote → A's mouse/keyboard work immediately.

**Clipboard**
- ☐ Copy text on A, cross to B, paste → same text. Copy on B, return to A, paste → same text.
- ☐ Copying > 64 KB text is skipped without breaking anything.

**Platform**
- ☐ Windows receiver: an app run as administrator ignores input until `btb connect` is elevated.
- ☐ Windows with display scaling ≠ 100%: cursor entry height is correct.
- ☐ Linux: confirm an X11 session; on Wayland you get the warning.

## Roadmap

1. **BLE transport** - GATT characteristic streaming for lower power, and connection-less reconnection (Bluetooth LE HID-style latency).
2. **Android remote-trackpad client** - turn a phone into a receiver/trackpad using the same encrypted protocol.
3. **File transfer over a Wi-Fi fallback** - negotiate over Bluetooth, move bulk data over a local Wi-Fi/TCP link.
4. **Rust rewrite** - single static binary, tighter latency, and native evdev/uinput + Windows raw-input paths.

Also planned: Linux evdev/uinput backend for Wayland, multi-monitor layouts, SDP service registration so the RFCOMM service is discoverable, per-session key confirmation (SAS) for stronger pairing.

## Development

```bash
uv sync
uv run pytest            # 97 tests: protocol, crypto, edge, coalescing, transports, end-to-end
uv run btb test-loopback
```
Tests never install real hooks or inject real input; the integration tests run `Server` and `Client` over `TCPLoopbackTransport` with mock input devices. See [build.md](build.md) for packaging.
