# Architecture

This document explains how the J1939 diagnostics tool is built, and how it is meant to become one
tool inside the larger CANopener handheld (a CM4-based instrument with a 7" touchscreen, a
navigation encoder, a fullscreen Pygame UI, and Pico / ESP32 subsystems), described in
[DESIGN.md](DESIGN.md).

## Layers

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Front-ends                                                               │
│   gui/   desktop Qt app      cli.py   terminal / SSH      device/ Pygame │
│                                                           handheld UI    │
└───────────────▲──────────────────────────▲───────────────────────────────┘
                │ reads state, calls scans │
┌───────────────┴──────────────────────────┴───────────────────────────────┐
│ core/   Session (rx thread, state, recording) · scan (DM requests,       │
│         retries) · report · profiles · units · interfaces · connection   │
└───────────────▲──────────────────────────────────────────────────────────┘
                │ CanFrame in / out
┌───────────────┴──────────────────┐   ┌───────────────────────────────────┐
│ can/  backends: SocketCAN,       │   │ j1939/  pure protocol: IDs, DTCs, │
│       python-can (opt.), virtual,│   │         DM1-DM23, TP (BAM/RTS),   │
│       replay; candump read/write │   │         signals, NAME, SA names   │
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

`core/transmit.py` is the place to send arbitrary frames on purpose. A `Transmitter` wraps a
`Session` with a gate (off by default, never sends in listen-only) and a log of every attempt; the
UI (`gui/transmit_dialog.py`) shows the exact CAN ID, bytes and destination and asks before each
send. Sendable actions are `TransmitAction`s in a registry: the repo ships generic, publicly
documented ones (a J1939 Request, a raw frame, a UDS request). A plugin adds its own — including
OEM commands that change a machine — with `register_action` (or
`PluginApi.register_transmit_action` from `register_gui`); they then appear in the same
confirm-first window.

The standard commands have their own paths: OBD-II Mode 04 (clear codes) and Mode 08 (on-board
tests) are `ObdClient.clear_dtcs` / `run_control_test` in `core/obd.py`; UDS
ClearDiagnosticInformation / ECUReset are `UdsClient.clear_dtcs` / `ecu_reset` in `core/uds.py`.
J1939 code clears (DM11 active, DM3 stored: a Request for that PGN, to every module or one, which
answers with an ACK / NACK) are `core/j1939_clear.py`. Front-ends confirm before calling any of them
(the warning texts are `core.obd.CLEAR_WARNING` and `j1939_clear.warning()`, shared by every
front-end), and the simulator answers DM11 / DM3 like a real ECU.

### OBD-II

`obd/` is the protocol: ISO-TP (11- and 29-bit `Addressing`, segmenting and reassembly), every
public Mode 01/02 PID (`pids.py`, with multi-value and text PIDs, and Live Data groups), readiness
monitor decode (`readiness.py`), Mode 06 test results with the unit/scaling table (`monitors.py`),
Mode 09 info, DTC text (`dtc.py` + the generated `dtc_codes.py`) and code -> sensor PID links
(`dtc_pids.py`, the OBD-II counterpart of a J1939 code's SPN).

`obd/sniffer.py` decodes OBD-II *passively* from frames, the way `Session.feed` does J1939: it
follows any tester's requests and the ECUs' answers and keeps the latest values, codes, readiness,
freeze frame, info, test results and an event timeline. That one decode path serves the live view
(`core.live.ObdLiveData`, rows shaped like J1939 signals so `gui/tab_live.py` shows either), a
decoded log file, and the capture report. Polling only puts requests on the bus; whatever answers
(to this app or another scan tool) is decoded the same way. `core/obd.py`'s `ObdClient` sends
functional requests and keeps every ECU's answer (keyed by response ID), detects the addressing,
and polls live data with physical requests. `sim/obd.py` is a two-module car (ECM + TCM) that
answers all of it, on 11- or 29-bit IDs.

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
| Paged screens: Live Data, Fault Codes, Capture Log ... | Desktop GUI tabs, and the handheld UI's pages in `device/screens/` over the same core calls. Live Data's model (values, min/max, history, picked signals) is `core/live.py` |
| DMM probes, Pico analog channels, wireless satellites on the capture timeline | `core/measure.py` (hub, channels, clock mapping) + `core/meters.py` (sources); recorded to `measurements.csv` by `core/capture.py` |
| Header chrome, soft keys, encoder / d-pad navigation | `device/app.py` + `device/input.py`; wiring in `deploy/raspberry-pi/config-controls.txt` |
| Fault code → live PID (use-case step 2) | `Signal.spn` links; GUI "Show in Live Data" button |
| CAN status pill / "is the adapter hearing the machine" | `core/link.py`: `adapter_status`, `bus_status` (GUI: Adapter / Bus lamps) |
| Placing probes: wait for good traffic, chime, lock bitrate | `core/link.SignalFinder` (listen-only, never stops on errors) + `core/sound.py` (speaker WAV, or piezo via sysfs PWM / kernel beeper). GUI: `gui/connection_panel.py`; CLI: `canopener-cli find` |
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

### The Pygame instrument UI (`canopener/device/`)

Built along these lines; usage, wiring and status are in [DEVICE.md](DEVICE.md).

* `device/context.py` (no Pygame) holds the state every screen shares: settings, connection,
  live models, the meter hub, the capture, and worker-thread jobs drained once per frame. Tests
  drive it directly, like `gui/context.py` but without Qt.
* `device/input.py` (no Pygame) turns keys (the `gpio-key` buttons), the rotary encoder (evdev
  `REL_X`) and an optional resistor ladder into actions: up / down / left / right, select (hold
  = back), back, home, soft keys 1-5, rotate, log, tag.
* `device/app.py` draws the chrome (soft keys on the buttons' edge, status strip opposite) and
  stacks screens; `device/ui.py` is the drawing kit (sizes in millimetres from the panel's pixel
  density, lists with drag scrolling, procedural icons); `device/screens/` are the pages.
* Modes (home-screen tiles) register in `device/modes.py`: CAN, Meter, Data log, System today;
  a logic analyzer or scope later, and plugins through `register_device(api)`.
* `frontends.py` switches between the desktop GUI and the handheld UI (shared `ui` setting).

### Measurements on the CAN timeline

`core/measure.py` (channels, samples, `MeasurementHub`, `ClockMapper`, `MeasurementLiveData`) and
`core/meters.py` (simulated, serial JSON-lines and Linux IIO sources) are the groundwork for the
DMM, the Pico's analog channels and wireless satellites. Samples carry wall-clock time like
`CanFrame`; a capture records them to `measurements.csv`, and `capture_report` merges them into
`live_data.csv` and the summary timeline.

## Device readiness review (October 2026)

How well this app drops into the handheld as one system, checked against [DESIGN.md](DESIGN.md).

**Ready as is**

| Design item | Status |
|---|---|
| MCP2518FD → `can0` | `SocketCanBackend` needs no change. Interface bring-up (`core/interfaces.py`) already treats `restart-ms` as best-effort, and the `mcp251xfd` driver supports it. |
| Boot to app, no desktop | Pi launcher + systemd user service; Qt runs on `eglfs` / `linuxfb`. The Pygame UI replaces the window, not the plumbing. |
| CAN Node & PID Label Profiles | `core/profiles.py`, per make/model, with auto-detection from the modules on the bus. Plugins can ship private ones. |
| Fault code → live PID (use-case step 2) | `Signal.spn` links; `LiveData.find_spn()` gives the device page the same jump. |
| Start/stop log, timestamps, hardware buttons | `core/capture*.py` (plan, tag, save, crash recovery, summary) is UI-free. Keys are remappable and `gpio-key` buttons send them. |
| Frame replay / command injection | `core/transmit.py`: off by default, refused in listen-only, every frame confirmed and logged. OEM actions come from plugins. |
| Subsystem fault management (CAN interface) | `Session.health` (link / tx errors, bus-off, no-ACK) plus `interfaces.usb_adapters()` / `no_adapter_hint()`, which say *why* there's no interface (adapter missing, slcan not attached, DFU mode, no driver). This is the "human-readable dialogue" pattern DESIGN.md asks for; Pico and ESP32 checks can follow it. |
| Memory on a 2 GB CM4 | The session's raw-frame buffer (250,000 frames) is about 46 MB; Live Data history is 600 s per signal. Comfortable. |
| Live Data page | **Done in this review:** the model moved from the Qt tab into `core/live.py` (`LiveData`: `sample()`, `load_history()`, `select()`, `matches()`, `find_spn()`, min/max, history), tested without Qt in `tests/test_live.py`. The desktop tab is now only the view; the Pygame page uses the same model, and its "PID picker → big-value grid" is `LiveData.selected`. |

**Recommended before the device UI** (in priority order; not changed yet)

1. **Settings that both front-ends share.** *Partly done:* the JSON store exists
   (`core/settings.py`, `~/.config/canopener/settings.json`) and the handheld UI uses it. The
   desktop GUI still keeps interface, bitrate, units, profile and so on in `QSettings`; moving
   them over (reading the old values once to migrate) is the remaining step.
2. **Timestamps for the shared timebase.** `SocketCanBackend.recv()` stamps frames with
   `time.time()` in Python after `recv()` returns: wall-clock (it jumps when a Pi with no RTC gets
   NTP time after boot) and subject to scheduler jitter. For lining CAN up with Pico captures, enable
   `SO_TIMESTAMPNS` and read the kernel's receive time with `recvmsg()` (falling back to
   `time.time()`), and record the capture's T+0 on `time.monotonic()` alongside wall time. Test it on
   `vcan0` and the MCP2518FD; it can't be exercised without SocketCAN.
3. **Plugins on the device.** `register_gui(api)` is Qt-only. Keep plugin logic in Qt-free modules
   with a thin Qt view (as the private Hyster/Cummins plugin does: `hyster.py` / `cummins.py` +
   `*_tab.py`), and add a front-end-neutral hook (e.g. `register_pages`) when the Pygame UI exists, so
   a plugin can offer a device page over the same logic.
4. **Two CAN channels.** DESIGN.md's dual MCP2518FD / expanded CAN module means two buses at once.
   A `Session` is one bus and the GUI assumes one connection. Plan for one session per channel and a
   channel tag on frames when captures are merged onto one timeline.
5. **CAN FD.** The backend reads classic 16-byte frames only. Add `CAN_RAW_FD_FRAMES` and 72-byte
   frames (J1939-22 uses FD); the MCP2518FD and CANable 2.0 both support it.
6. **Screen size.** The desktop GUI is tested to fit a 1280x720 laptop (about 1280x650 usable) with
   the Log panel open. Its minimum width (~970 px) is too wide for an 800x480 panel, so the
   on-device UI is a separate front-end, as planned. Size it for the chosen panel (800x480,
   1024x600 or 720x1280 portrait are the common 7" DSI options).
7. **Encoder.** *Done:* `device/input.EvdevReader` reads the rotary-encoder overlay's `REL_X`
   events from `/dev/input` (overlay lines in `deploy/raspberry-pi/config-controls.txt`).
8. **Missing-module alerts.** Profiles know which modules a machine has (`detect.addresses`) and the
   capture summary reports ECUs going silent; a live "expected module missing" alert in `core/`
   would serve both front-ends (the Hyster plugin's Modules list does this for one OEM).

## Adding things

* **A new live parameter:** add a `Signal(...)` to `j1939/signals.py` (or a profile `signals` entry
  for machine-specific ones). It appears in Live Data, reports and the CLI automatically.
* **A new DM / request:** add the PGN to `j1939/constants.py`, a decoder to `j1939/dtc.py` or
  `info.py`, include it in a preset in `core/scan.py`, and render it in `core/report.py`.
* **A new bus type** (CAN FD, a second channel, a serial gateway on the ESP32): implement
  `can.backends.CanBackend` (`open`, `close`, `send`, `recv`) and add it to `core/connection.py`.
  First check whether python-can already covers it: `can/pythoncan.py` opens any adapter python-can
  supports (optional `adapters` extra), named `interface:channel` in the same Interface box as can0.
* **Labels made in the app:** `core/labeling.py` edits user profile JSON (atomic writes, unknown
  keys kept, `extends` the profile in use); front-ends then reload profiles, so labels apply live
  with no reconnect. `core/analyze.py` ("Find what changed") ranks the bytes / bits / 16-bit
  values that differ between two time windows of any `CanFrame` list (live buffer, log, capture
  tags), skipping rolling counters. Both are UI-free so the handheld can share them.
* **Simulator behaviour:** `sim/machine.py`. Tests rely on its codes, so update `tests/` with it.

## Roadmap ideas

* CAN FD frames in the SocketCAN backend (MCP2518FD supports them; J1939-22 uses them).
* Dropout / missing-ECU alerts in the Network view (expected SAs per profile).
* DM1 history timeline (when each code went active / inactive).
* Labeling 11-bit IDs (CANopen PDOs) and big-endian values; a passive CANopen decoder (heartbeat,
  NMT state, EMCY as fault codes).
* More transmit actions on the confirm-first path (saved frame replay); machine-changing OEM
  commands stay in private plugins.
* Export to CSV for the live-data history.
