# Architecture

How CANopener's software is built: the layers and the rules that keep them apart, how sending is
gated, the threading model, and how each part maps onto the handheld ([DESIGN.md](DESIGN.md)).
Dev setup and the profile format are in [DEVELOPING.md](DEVELOPING.md).

## Layers

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Front-ends                                                               │
│   gui/  desktop (Qt)     cli.py  terminal / SSH     device/  handheld    │
│                                                              (Pygame)    │
└───────────────▲──────────────────────────▲───────────────────────────────┘
                │ reads state, calls scans │
┌───────────────┴──────────────────────────┴───────────────────────────────┐
│ core/   Session (rx thread, state, recording) · scans · OBD/UDS/CANopen  │
│         clients · live data · capture · profiles · settings · transmit   │
│         · interfaces · link status · measurements · installed options    │
└───────────────▲──────────────────────────────────────────────────────────┘
                │ CanFrame in / out
┌───────────────┴──────────────────┐   ┌───────────────────────────────────┐
│ can/  backends: SocketCAN,       │   │ pure protocol libraries:          │
│       python-can (opt.), virtual,│   │ j1939/  obd/  uds/  canopen/      │
│       replay, ELM327 (Bluetooth  │   │ (IDs, codes, transport, signals)  │
│       OBD); candump read/write   │   │                                   │
└──────────────────────────────────┘   └───────────────────────────────────┘
                ▲
          sim/  simulated machine, car and battery truck (another user of the same bus)
```

Rules that keep it reusable:

* **`j1939/`, `obd/`, `uds/`, `canopen/` and `can/` never import from `core/`, `gui/` or `sim/`.** They are
  plain protocol libraries with no state beyond lookup tables (`obd/` = OBD-II J1979 + ISO-TP,
  `uds/` = UDS ISO 14229; both reuse the ISO-TP reassembler and carry no OEM identifiers). Their
  live clients (`core/obd.py`, `core/uds.py`, `core/canopen.py`) drive a `Session` the same way
  `scan.py` does.
* **`core/` never imports a UI toolkit.** Everything a screen needs (fault-code rows, lamp status,
  latest values, per-ECU stats, raw frames since sequence N) is a thread-safe method on `Session`
  or a dataclass from `scan`.
* **Front-ends hold no protocol logic.** The Qt app and the Pygame handheld call the same
  `scan.run_preset()`, `session.current_lamps()`, `session.latest_for(pgn)`, `core/live.py` and
  `core/capture.py`, so a fix in core reaches all three front-ends.
* **Standard library only** below `gui/` and `device/`. That keeps the Pi image small and boot fast.

## Plugins

`canopener/plugins.py` loads packages registered under the `canopener.plugins` entry point.
They can add GUI tabs / menus (`register_gui(api)`, see `PluginApi` in `gui/main_window.py`),
CLI commands (`register_cli`) and profile folders (`profile_dirs`). Two `Session` hooks exist for
them: `add_listener(fn)` (every received frame, on the receive thread) and `send_frame(frame)`
(any 11/29-bit frame; refused in listen-only). This is how OEM-specific and bidirectional tools
live in separate, private packages while this repository stays generic. On the handheld,
`register_device(api)` adds a mode (a home-screen tile and its screens; `device/modes.py`). Keep a
plugin's logic in Qt- and Pygame-free modules with a thin view per front-end, so both can share it.

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

### Bluetooth OBD adapters (ELM327)

An ELM327 is a scan tool spoken to in text, not a CAN interface. `can/elm327.py`'s `ElmBackend`
makes one look like a bus anyway, so `ObdClient`, `UdsClient`, live data, captures and the Log tool
run unchanged: `send()` takes the single-frame ISO-TP requests the clients build, sets the
adapter's header (`ATSH`, `ATCP`) and receive filter (`ATCRA` / `ATAR`), and sends the payload as
hex; every reply line (headers on) becomes a received `CanFrame`. Flow control frames from the
clients are dropped (the adapter does its own); multi-frame requests, non-ISO-TP frames and
anything on the wrong ID width raise `ElmError` (an `OSError`, so `Session.send_frame` reports it
in `health.tx_error`). `answers_only = True` tells `core.link` to word the lamps for it, and
`ObdClient.detect_addressing` to use the protocol the adapter found. Speed: a single-PID request
to one module ends with the response-count digit (`010C1`) so the adapter returns at the first
answer; clear / test / reset requests raise the adapter timeout (`ATSTFF`) for 'response pending'.
Timestamps are host-side. Streams: `AF_BLUETOOTH` RFCOMM (Classic SPP), TCP (Wi-Fi dongles) or a
serial device, all standard library. `sim/elm.py` speaks the adapter's text protocol over a
socket pair in front of `sim/obd.py` (or `sim/machine.py` for J1939), so tests and `--sim`
exercise the real backend.

* **Non-CAN cars** (J1850 PWM / VPW, ISO 9141-2, KWP2000, ATDPN 1-5): `obd/noncan.py` turns each
  request's messages (3-byte header, check byte, codes three per message with no count, Mode 09 in
  numbered 4-byte pieces) into the answer a CAN module would give, and the backend delivers it as
  ISO-TP frames on stand-in IDs (engine 0x10 on 0x7E8, transmission 0x18 on 0x7E9;
  `module_label()` names the real address for `ObdClient.ecu_label`). UDS and Mode 06 are refused
  there.
* **J1939** (protocol A, chosen explicitly, never by the automatic search): a thread runs `ATMA`
  and feeds every frame to the session like a wired bus (`answers_only` is off); any command
  pauses it (`_paused`), so J1939 Requests and TP flow control go out raw (`ATCAF0`) in between.
  Other frames are refused. Listen-only is honoured.
* **Probe and console** (`can/elm_probe.py`, `console_refusal`): what an adapter can do, and
  adapter-only commands typed by hand (nothing that reaches the vehicle, rewrites the adapter's
  stored settings or serial speed).
* **Battery voltage**: `core.meters.ObdVoltageSource` (ATRV, ~1 Hz) joins the measurement hub on
  both front-ends while an adapter is connected (the desktop now has a hub for it), so captures
  record it in `measurements.csv`.
* **Other buses** (`core/adapter_buses.py`): the Vehicle picker is the built-in car / J1939
  choices plus plugins' `adapter_buses()` (`BusSetup`: adapter commands, protocol, undo). Shared
  settings `elm.protocol` / `elm.bus` hold the choice.

What has and hasn't been checked on real hardware: [BLUETOOTH-OBD.md](BLUETOOTH-OBD.md#gaps-to-fill).

Pairing is `core/bluetooth.py`: BlueZ's `bluetoothctl` (scan, `info` to tell a dual-mode adapter's
Classic serial side from its BLE side, an interactive `pair` answering the PIN prompt, `remove`),
no root and no extra packages. Adapter names live in the shared settings (`bluetooth.names`,
`elm.adapter`, `elm.protocol`). The desktop's Pair / Manage dialog (`gui/bluetooth_dialog.py`)
and the handheld's Bluetooth screen (`device/screens/bluetooth.py`, PIN and names picked from
lists) are only screens over it.

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
| Tiered build: which parts are fitted | `core/hardware.py` (installed options: not installed / installed / use if found, by build stage); System > Installed options, `canopener-cli hardware` |
| Periodic SYNC pulse (meter Pico → Pi GPIO21 + logic Pico D2) | `core/sync.py` (`PulseClock`, `PpsReader`, `fit_edges`); firmware in `firmware/meter-pico/` |
| Logic analyzer (logic Pico, stock sigrok-pico) | `core/logic.py` (sigrok-cli command, channel names, SYNC edges from `.sr`) |
| Pico / ESP32 firmware | `firmware/` (meter Pico in MicroPython, ESP32 bridge in Arduino); all speak the meter line protocol |
| Header chrome, soft keys, encoder / d-pad navigation | `device/app.py` + `device/input.py`; wiring in `deploy/raspberry-pi/config-controls.txt` |
| Fault code → live PID (use-case step 2) | `Signal.spn` links; GUI "Show in Live Data" button |
| CAN status pill / "is the adapter hearing the machine" | `core/link.py`: `adapter_status`, `bus_status` (GUI: Adapter / Bus lamps) |
| Placing probes: wait for good traffic, chime, lock bitrate | `core/link.SignalFinder` (listen-only, never stops on errors) + `core/sound.py` (speaker WAV, or piezo via sysfs PWM / kernel beeper). GUI: `gui/connection_panel.py`; CLI: `canopener-cli find` |
| Subsystem fault management ("CAN interface" state) | `Session.health` (link error, tx error, error frames incl. bus-off / no-ACK), plus `interfaces.usb_adapters()` / `no_adapter_hint()`, which say *why* there's no interface (adapter missing, slcan not attached, DFU mode, no driver) |
| Memory on a 2 GB board | The session's raw-frame buffer (250,000 frames) is about 46 MB; Live Data history is 600 s per signal |
| Shared timebase with Pico captures | Every `CanFrame` carries a wall-clock `timestamp`. A capture start (T+0) can be recorded alongside and subtracted |
| Install, update, USB adapter drivers | `deploy/linux/` (`install.sh`, CANable udev rule + slcand unit); used by the Pi installer too |
| Boot to app on the Pi | `deploy/raspberry-pi/` (launcher, systemd user service, sudoers); Qt's `eglfs` / `linuxfb` back-ends need no desktop |
| Start/stop log and timestamp keys | `core/capture.py` (plan, tag, save, recover) + `core/capture_report.py` (summary, live-data CSV). The GUI's Log tool and remappable keys are in `gui/log_panel.py` / `gui/hotkeys.py`; hardware buttons send the same keys via `gpio-key` |
| Log export in the field | `core/storage.py`: copy logs + reports to a USB stick, CSV export |
| Frame replay / command injection | Transmit path in `core/transmit.py` + `gui/transmit_dialog.py`: explicit off-by-default transmit mode, per-frame confirmation, never in listen-only. Generic actions built in; OEM/active ones come from plugins |

### The Pygame instrument UI (`canopener/device/`)

Usage, wiring and status are in [DEVICE.md](DEVICE.md).

* `device/context.py` (no Pygame) holds the state every screen shares: settings, connection,
  live models, the meter hub, the capture, and worker-thread jobs drained once per frame. Tests
  drive it directly, like `gui/context.py` but without Qt.
* `device/input.py` (no Pygame) turns keys (the `gpio-key` buttons), the rotary encoder (evdev
  `REL_X`) and an optional resistor ladder into actions: up / down / left / right, select (hold
  = back), back, home, soft keys 1-5, rotate, log, tag.
* `device/app.py` draws the chrome (soft keys on the buttons' edge, status strip opposite) and
  stacks screens; `device/ui.py` is the drawing kit (sizes in millimetres from the panel's pixel
  density, lists with drag scrolling, procedural icons); `device/screens/` are the pages.
* `core/themes.py` is the look (the desktop app's colour themes, `gui/themes.py`, use its palettes): built-in themes and the user's
  `~/.config/canopener/themes/*.json`, resolved to a palette, style switches and font files
  (found by name with `fc-match`). Screens name colours by role (`ui.color("dim")`) and draw
  surfaces with `ui.panel` / `ui.card`, so a theme reaches every page; icons are drawn at 3x and
  scaled down smooth, once per size and colour.
* Modes (home-screen tiles) register in `device/modes.py`: CAN, Meter, Data log, System today;
  a logic analyzer or scope later, and plugins through `register_device(api)`.
* `frontends.py` switches between the desktop GUI and the handheld UI (shared `ui` setting).

### Measurements on the CAN timeline

`core/measure.py` (channels, samples, `MeasurementHub`, `ClockMapper`, `MeasurementLiveData`) and
`core/meters.py` (simulated, serial JSON-lines and Linux IIO sources) are the groundwork for the
DMM, the Pico's analog channels and wireless satellites. `core/hardware.py` decides which sources
start (the installed options, [ELECTRONICS.md](ELECTRONICS.md) section 19): a part that isn't fitted
gets no source, one that's fitted but missing gets a source that reports it and retries on its own
thread. Detection only looks at sysfs and `/dev`, so it can't block. `core/sync.py` pairs the meter
Pico's SYNC pulses with the kernel's pps stamps of the same edges and maps Pico time to wall time
between them; sources without SYNC keep `ClockMapper`. Samples carry wall-clock time like
`CanFrame`; a capture records them to `measurements.csv`, and `capture_report` merges them into
`live_data.csv` and the summary timeline.

## Open work

What's still to do for the handheld and beyond, roughly in priority order. Other docs refer to
these by name.

* **Shared settings.** *Partly done:* `core/settings.py` (`~/.config/canopener/settings.json`)
  holds what both UIs share and the handheld uses it. The desktop GUI still keeps interface,
  bitrate, units, profile and so on in `QSettings`; moving them over (reading the old values once
  to migrate) is the remaining step.
* **Kernel receive timestamps.** `SocketCanBackend.recv()` stamps frames with `time.time()` in
  Python after `recv()` returns: wall-clock (it jumps when a Pi with no RTC gets NTP time after
  boot) and subject to scheduler jitter. For lining CAN up with Pico captures, enable
  `SO_TIMESTAMPNS` and read the kernel's receive time with `recvmsg()` (falling back to
  `time.time()`), and record the capture's T+0 on `time.monotonic()` alongside wall time. Test it on
  `vcan0` and the MCP2518FD; it can't be exercised without SocketCAN.
* **Two CAN channels.** DESIGN.md's dual MCP2518FD means two buses at once. A `Session` is one bus
  and both GUIs assume one connection. Plan for one session per channel and a channel tag on
  frames when captures are merged onto one timeline.
* **CAN FD.** The backend reads classic 16-byte frames only. Add `CAN_RAW_FD_FRAMES` and 72-byte
  frames (J1939-22 uses FD); the MCP2518FD and CANable 2.0 both support it.
* **Missing-module alerts.** Profiles know which modules a machine has (`detect.addresses`) and the
  capture summary reports ECUs going silent; a live "expected module missing" alert in `core/`
  would serve both front-ends.
* **Handheld screens still to come:** CANopen (nodes, EMCY, error history), clear codes and
  transmit (confirm-first, default No, never in listen-only), the logic analyzer (`core/logic.py`
  has the command and channel names, no UI yet) and Component Test (ELECTRONICS.md 6.7).
* **CANopen:** read PDO mappings (0x1A00..) to suggest signal layouts.
* **Transmit:** more actions on the confirm-first path (saved frame replay); machine-changing OEM
  commands stay in private plugins.
* **Bluetooth OBD adapters:** check everything against a real adapter and vehicles, then
  multi-PID requests and the non-CAN Mode 06 decoder: [BLUETOOTH-OBD.md](BLUETOOTH-OBD.md#gaps-to-fill).

## Adding things

* **A new live parameter:** add a `Signal(...)` to `j1939/signals.py` (or a profile `signals` entry
  for machine-specific ones). It appears in Live Data, reports and the CLI automatically.
* **A new DM / request:** add the PGN to `j1939/constants.py`, a decoder to `j1939/dtc.py` or
  `info.py`, include it in a preset in `core/scan.py`, and render it in `core/report.py`.
* **A new bus type** (CAN FD, a second channel, a serial gateway on the ESP32): implement
  `can.backends.CanBackend` (`open`, `close`, `send`, `recv`) and add it to `core/connection.py`.
  An ELM327 reached through the ESP32 later only needs a new `can.elm327.Stream` (the backend
  above it stays the same).
  First check whether python-can already covers it: `can/pythoncan.py` opens any adapter python-can
  supports (optional `adapters` extra), named `interface:channel` in the same Interface box as can0.
* **CANopen** is a profile protocol like OBD-II: `"canopen"` turns the J1939 tabs into CANopen
  ones (Fault Codes = EMCY + error history, Network = nodes + NMT, no lamps, no J1939 sent),
  `"j1939+canopen"` shows both, and `"j1939"` shows none of it. Front-ends and the core
  (scans, reports, captures) ask `profiles.speaks_j1939()` / `speaks_canopen()`; the main
  window offers the matching `profiles.GENERIC` profile when the bus speaks something else. `canopen/` is the pure
  protocol (COB-IDs, NMT, EMCY, SDO, objects, `CanopenSniffer`); `Session.feed` keeps every 11-bit
  ID's latest payload (`latest_std`) and feeds the sniffer, so nodes, states, EMCY and anything read
  over SDO (by us or another tool) are followed passively. `core/canopen.py` has the client (SDO
  reads, node discovery, and the confirm-first NMT command and error-history clear), the
  fault-code rows (`codes`) and `source_name`, which front-ends use for any row source: a J1939
  address (0-255), a node (`node_src`) or an 11-bit ID (`id_src`). Scans read CANopen too and skip
  J1939 requests on a bus with CANopen nodes and no 29-bit traffic. Signals with an `id` (11-bit)
  decode from `latest_std`, so Live Data, reports, captures and labeling cover both.
* **Labels made in the app:** `core/labeling.py` edits user profile JSON (atomic writes, unknown
  keys kept, `extends` the profile in use); front-ends then reload profiles, so labels apply live
  with no reconnect. `core/analyze.py` ("Find what changed") ranks the bytes / bits / 16-bit
  values that differ between two time windows of any `CanFrame` list (live buffer, log, capture
  tags), skipping rolling counters. Both are UI-free so the handheld can share them.
* **Simulator behaviour:** `sim/machine.py`. Tests rely on its codes, so update `tests/` with it.
