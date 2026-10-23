# Architecture

This document explains how the J1939 diagnostics tool is built, and how it is meant to become one
tool inside the larger CANopener handheld (a CM4-based instrument with a 7" touchscreen, a
navigation encoder, a fullscreen Pygame UI, and Pico / ESP32 subsystems), described in
[DESIGN.md](DESIGN.md).

## Layers

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Front-ends                                                               │
│   gui/   desktop Qt app      cli.py   terminal / SSH      (future) Pygame │
│                                                           instrument UI  │
└───────────────▲──────────────────────────▲───────────────────────────────┘
                │ reads state, calls scans │
┌───────────────┴──────────────────────────┴───────────────────────────────┐
│ core/   Session (rx thread, state, recording) · scan (DM requests,       │
│         retries) · report · profiles · units · interfaces · connection   │
└───────────────▲──────────────────────────────────────────────────────────┘
                │ CanFrame in / out
┌───────────────┴──────────────────┐   ┌───────────────────────────────────┐
│ can/  backends: SocketCAN,       │   │ j1939/  pure protocol: IDs, DTCs, │
│       virtual bus, log replay;   │   │         DM1-DM23, TP (BAM/RTS),   │
│       candump read/write         │   │         signals, NAME, SA names   │
└──────────────────────────────────┘   └───────────────────────────────────┘
                ▲
          sim/  simulated machine (another backend user on the same bus)
```

Rules that keep it reusable:

* **`j1939/`, `obd/`, `uds/` and `can/` never import from `core/`, `gui/` or `sim/`.** They are
  plain protocol libraries with no state beyond lookup tables (`obd/` = OBD-II J1979 + ISO-TP,
  `uds/` = UDS ISO 14229; both reuse the ISO-TP reassembler and carry no OEM identifiers). Their
  live clients — `core/obd.py`, `core/uds.py` — drive a `Session` the same way `scan.py` does.
* **`core/` never imports a UI toolkit.** Everything a screen needs (fault-code rows, lamp status,
  latest values, per-ECU stats, raw frames since sequence N) is a thread-safe method on `Session`
  or a dataclass from `scan`.
* **Front-ends hold no protocol logic.** The Qt app is about 1,500 lines of widgets. A Pygame
  screen would call the same `scan.run_preset()`, `session.current_lamps()`,
  `session.latest_for(pgn)` and `signal.decode()`.
* **Standard library only** below `gui/`. That keeps the Pi image small and boot fast.

## Plugins

`canopener/plugins.py` loads packages registered under the `canopener.plugins` entry point.
They can add GUI tabs / menus (`register_gui(api)`, see `PluginApi` in `gui/main_window.py`),
CLI commands (`register_cli`) and profile folders (`profile_dirs`). Two `Session` hooks exist for
them: `add_listener(fn)` (every received frame, on the receive thread) and `send_frame(frame)`
(any 11/29-bit frame; refused in listen-only). This is how OEM-specific and bidirectional tools
live in separate, private packages while this repository stays generic.

### Transmitting (confirm-first)

`core/transmit.py` is the one place that *sends* on purpose. A `Transmitter` wraps a `Session`
with a gate (off by default, never sends in listen-only) and a log of every attempt; the UI
(`gui/transmit_dialog.py`) shows the exact CAN ID, bytes and destination and asks before each
send. Sendable actions are `TransmitAction`s in a registry: the repo ships only generic, publicly
documented ones (a J1939 Request, a raw frame). A plugin adds its own — including OEM or active
commands that change a machine — with `register_action` (or `PluginApi.register_transmit_action`
from `register_gui`); they then appear in the same confirm-first window. Fault-clearing/reset and
proprietary commands are intentionally **not** in this repository.

## Threading model

* `Session` owns one receive thread that calls `backend.recv()` and `Session.feed()`. Transport
  protocol CTS/ACK replies are sent from that thread so they're on time.
* Scans (`scan.run_scan`) block for a few seconds and run in a worker thread. The GUI gets the
  `ScanResult` through a Qt signal.
* Front-ends **poll**: the Qt app refreshes the visible tab every 200 ms. This is deliberate. It
  maps directly onto a Pygame frame loop, and nothing on the rx thread ever waits on the UI.

## Mapping onto the CANopener device

| Device concept (design notes) | Where it lives now |
|---|---|
| MCP2518FD on SPI → `can0` | `can.backends.SocketCanBackend` (no code changes needed) |
| `can_logger.py` foundation | `Session.start_recording()` → `can.logfile.LogWriter` (candump format) |
| Source-address filtering, dropout detection | `Session.stats` / `ecus` (first/last seen, rate per PGN). Dropout alerts are a small addition on top |
| CAN Node & PID Label Profiles | `core/profiles.py` + `canopener/profiles/*.json` |
| Paged screens: Live Data, Fault Codes, Capture Log ... | GUI tabs today; the same core calls back each Pygame page |
| Fault code → live PID (use-case step 2) | `Signal.spn` links; GUI "Show in Live Data" button |
| Subsystem fault management ("CAN interface" state) | `Session.health` (link error, tx error, error frames incl. bus-off / no-ACK) |
| Shared timebase with Pico captures | Every `CanFrame` carries a wall-clock `timestamp`. A capture start (T+0) can be recorded alongside and subtracted |
| Install, update, USB adapter drivers | `deploy/linux/` (`install.sh`, CANable udev rule + slcand unit); used by the Pi installer too |
| Boot to app on the Pi | `deploy/raspberry-pi/` (launcher, systemd user service, sudoers); Qt's `eglfs` / `linuxfb` back-ends need no desktop |
| Start/stop log and timestamp keys | `core/capture.py` (plan, tag, save, recover) + `core/capture_report.py` (summary, live-data CSV). The GUI's Log tool and remappable keys are in `gui/log_panel.py` / `gui/hotkeys.py`; hardware buttons send the same keys via `gpio-key` |
| Log export in the field | `core/storage.py`: copy logs + reports to a USB stick, CSV export |
| Frame replay / command injection | Transmit path in `core/transmit.py` + `gui/transmit_dialog.py`: explicit off-by-default transmit mode, per-frame confirmation, never in listen-only. Generic actions built in; OEM/active ones come from plugins |

### On-device UI reference: v0.1

`legacy/can_opener_v0.1/` is the first on-device attempt (Pi 3B+, TFT, Tkinter, 800x480).
Its protocol tables are wrong (see `legacy/README.md`), but its layout is a good starting
point for the instrument UI:

* **Status strip** across the top: clock, CAN status pill (`can0 250k` / disconnected / error),
  REC indicator, frames/s. Tap it for a **system overlay**: brightness
  (`/sys/class/backlight/*/brightness`), units, log folder, uptime / CPU temperature, reboot and
  shut down behind a confirm dialog.
* **Left tab bar** with big icon + label buttons (Config, Raw/Log, Codes, PIDs). These map onto
  the paged screens in DESIGN.md and onto the encoder: rotate = move between tabs or rows,
  push = select.
* **Per-ECU filter column** next to codes and raw frames. "Poll selected" = a DM1 request
  addressed to that ECU.
* **PID picker**: choose a handful of signals, then a large-value live grid.
* **Touch details**: drag-to-scroll lists, full-width confirm dialogs, no small checkboxes.

`core` already gives each of those screens its data: `Session.current_lamps()`, `scan.run_preset()`,
`Session.latest_for()`, `Session.ecu_list()`, `Session.frames_since()`, `Session.health`.

### Suggested path to the Pygame instrument UI

1. Add `canopener/device/` (or a separate package) with the Pygame app: header chrome, soft-key
   footer, paged screens, encoder/button input.
2. Each page takes a `Session` and reads from it each frame, exactly like `gui/tab_*.py` do.
3. Keep the desktop GUI as the bench / laptop tool. Both front-ends share every bug fix in `core/`.

## Adding things

* **A new live parameter:** add a `Signal(...)` to `j1939/signals.py` (or a profile `signals` entry
  for machine-specific ones). It appears in Live Data, reports and the CLI automatically.
* **A new DM / request:** add the PGN to `j1939/constants.py`, a decoder to `j1939/dtc.py` or
  `info.py`, include it in a preset in `core/scan.py`, and render it in `core/report.py`.
* **A new bus type** (CAN FD, a second channel, a serial gateway on the ESP32): implement
  `can.backends.CanBackend` (`open`, `close`, `send`, `recv`) and add it to `core/connection.py`.
* **Simulator behaviour:** `sim/machine.py`. Tests rely on its codes, so update `tests/` with it.

## Roadmap ideas

* CAN FD frames in the SocketCAN backend (MCP2518FD supports them; J1939-22 uses them).
* Dropout / missing-ECU alerts in the Network view (expected SAs per profile).
* DM1 history timeline (when each code went active / inactive).
* Bit-level signals and enumerated states in profiles.
* More transmit actions on the confirm-first path (saved frame replay); machine-changing OEM
  commands stay in private plugins.
* Export to CSV for the live-data history.
