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
│         · uploads                                                        │
└───────────────▲──────────────────────────────────────────────────────────┘
                │ CanFrame in / out
┌───────────────┴──────────────────┐   ┌───────────────────────────────────┐
│ can/  backends: SocketCAN,       │   │ pure protocol libraries:          │
│       python-can (opt.), virtual,│   │ j1939/  obd/  uds/  canopen/      │
│       replay, ELM327 (Bluetooth  │   │ (IDs, codes, transport, signals)  │
│       OBD); candump read/write   │   │                                   │
└──────────────────────────────────┘   └───────────────────────────────────┘
  serialline/  serial ports (pyserial, opt.), reader threads, serial.log: a console port's bytes
                ▲
          sim/  simulated machine, car and battery truck (another user of the same bus)
```

Rules that keep it reusable:

* **`j1939/`, `obd/`, `uds/`, `canopen/`, `can/` and `serialline/` never import from `core/`, `gui/` or `sim/`.** They are
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
* **Few dependencies, and optional ones loaded late.** Below `gui/` and `device/` it's the standard
  library, plus two optional extras imported only when used: python-can (`can/pythoncan.py`, an
  adapter Linux can't show as can0) and pyserial (`serialline/port.py`, a serial line). A new one
  needs a compelling reason and a library that rarely needs patching. That keeps the Pi image
  small and boot fast.

## Plugins

`canopener/plugins.py` loads packages registered under the `canopener.plugins` entry point.
They can add GUI tabs / menus (`register_gui(api)`, see `PluginApi` in `gui/main_window.py`),
CLI commands (`register_cli`) and profile folders (`profile_dirs`). Two `Session` hooks exist for
them: `add_listener(fn)` (every received frame, on the receive thread) and `send_frame(frame)`
(any 11/29-bit frame; refused in listen-only). This is how OEM-specific and bidirectional tools
live in separate, private packages while this repository stays generic. On the handheld,
`register_device(api)` adds a mode (a home-screen tile and its screens; `device/modes.py`). Keep a
plugin's logic in Qt- and Pygame-free modules with a thin view per front-end, so both can share it.
A plugin can also add somewhere for saved captures to go: `core.upload.register_uploader`
(or `PluginApi.register_uploader`), next to the built-in HTTPS uploader.

**Bundled plugins** (`canopener/addons/`, `plugins.BUNDLED`) ship in this repository but load only
when switched on (`plugins.bundled`). The Zapi console mode (`addons/zapi_console/`) is the first:
`console.Console` (listening, message joining, analysis; no UI toolkit) under a handheld mode and
a desktop tab. While it listens it sets the context's `serial` to its `Sniffer`, which both Log
tools pass to `Capture.start(serial=...)`.

### Captures leaving the unit

`core/capture.py` keeps the machine details (`machine`, `question`) in `capture.json`, and
`core/capture_readme.py` rewrites `README.md` with it from the capture's state alone, so it's cheap
on every change. At save, `capture_report` adds what it can only learn from the log:
`traffic.csv` and `meta["bus"]` (identity heard, senders, undecoded streams, from `core/identity.py`,
which also suggests machine details from a live `Session`).

`core/upload.py` zips a saved folder, keeps per-capture state in its `upload.json` and the queue in
`~/.config/canopener/upload-queue.json`, and sends one capture at a time on its own thread with a
growing back-off. `core/upload_github.py` commits a capture to a private GitHub repository with
the device's GitHub sign-in (Git Data API: blobs, tree, commit, ref). The built-in `HttpUploader` speaks the small chunked API of
`deploy/upload-server/server.py` (standard library, one file; API and setup in its README). Parts
are resumable: creating the same upload again returns the parts the server already has. Both UIs
poll `UploadQueue.status()`; the desktop starts the thread in `MainWindow`, the handheld in
`device/app.py` (`DeviceContext.start_uploads`).

### Install identity, team access, the startup check

`core/install.py` owns the install's ID (`device-id`, random, made once) and its readable serial
(`CO-XXXX-XXXX`, Crockford base 32 of the first 40 bits; the upload server's `serial()` works it
out the same way: keep the two alike). Beside the ID is a salted fingerprint of `/etc/machine-id`;
when it no longer matches (the config folder was copied to another computer) a new ID is made.
`upload.device_id()` is the same ID.

`core/team.py` is team access, the app's side of the server's `/api/team` (its `Team` class):
joining with an invite link or a request, checking in, and installing the team plugin from the
server's zip (unpacked to `~/.local/share/canopener/team-plugin`, `pip install -e`, like
`plugin_repos`). The install's key is made here and only its hash reaches the server; requests
carry `X-Install` / `X-Machine` beside it. State is `team.json` (mode 600), valid only for the
install ID it names, so a copied folder carries nothing. A check that finds the install revoked,
unknown or on another computer removes the plugin without asking (and retries a failed `pip
uninstall` at the next one); a newer build is only reported, except the first install. `Admin`
and `make_bundle` are the owner's side (`canopener-cli team-admin`). The server also serves the owner's
side as a web page, `GET /admin` (`ADMIN_PAGE` / `ADMIN_JS` / `ADMIN_CSS` in `server.py`, strict CSP,
the admin token in `sessionStorage` sent as a Bearer header to the same `/api/team` calls).

`core/startup_check.py` is what a front-end runs in the background at start: team access always,
then (when the user's startup update check is on, the default) `updates.check()` and
`plugin_repos.check()` per GitHub source. `Found` says what's newer; `apply()` installs it only
after the user says yes (`gui/update_dialog.FoundDialog`). The desktop also re-runs the team part
hourly when `team.due()`; the handheld runs only the team part at start (`device/app._team_check`),
and **System > Software > Team access** (`device/screens/settings.py`) asks, checks, installs and leaves.

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
* **What changed, and observed profiles** (`core/changes.py`, `core/observed.py`): `changes.analyze`
  cuts a capture into phases (its timestamps) and each stream into fields (a 16-bit value where two
  bytes carry into each other, bytes, and single bits of bytes that hold few values), classes each
  (constant / counter / noise / switch / value), ranks what differs between two phases
  (`Report.changes`) and finds unnamed values that move with a named one (`Report.relations`, a
  straight-line fit over half-second slots, which also gives the scale). `observed` turns picked
  fields into profile `signals` entries with an `"observed"` note each (evidence, how sure) and
  saves an ordinary profile; the loader ignores the extra keys. Screens: `device/screens/discover.py`;
  CLI `changes`. Housekeeping traffic (`changes.is_housekeeping`) is never listed.
* **Live fields** (`core/fieldwatch.py`): `FieldWatch`, fed every frame by a session listener the
  handheld adds on connect (`ctx.fieldwatch`). It keeps two minutes of payloads per stream (every
  change, else 20 a second), the change times of every byte and bit (cheap `last_change` /
  `recent_changes` for "moving now"), and `refresh` re-judges every field over the whole window
  with `changes._stream` (on its own thread via `refresh_soon`). `plausible` says a field could be
  a control's own signal: not a bit of a byte that moves like a number, half of a 16-bit number,
  a counter or noise.
* **Guided Profile Builder** (`core/guide.py`, `device/screens/guide.py`): a state machine over a
  script of `Step`s (waiting / quiet / rest / do / look / result), fed frames by a session
  listener and time by the screen. Each step's rest and do windows go through `changes.analyze`
  (a value that leaves its resting band counts, not only one whose mean moves). Fields are left
  out when a decoder names them or the value they're part of (`observed.known_name`, bit-exact),
  when they changed in the opening hands-off stretch (`restless`), when `FieldWatch.plausible`
  says no, or when they changed in any hands-off stretch so far (the quiet and every rest:
  `wanders`). The result state is the live pick: each `Offer` counts its changes since "let go"
  from the watcher, the list re-sorts by that only when no key was pressed for `RESORT_IDLE`, and
  a clear winner is ticked (`auto`) unless the technician ticked something. Nothing is ticked on
  one sighting: only "seen again" or a value that follows a known one. Kept values are "checked"
  when they moved with the control while picking. The screen records an ordinary log and turns
  the steps into its timestamps ("get ready", the step, "let go"), and writes `Guide.summary()`
  (each attempt: offered, kept, warnings) into `capture.json` as `meta["guide"]`, which the
  capture README renders. Steps with the same `axis` are one control's two directions: a value
  kept for one is offered again (`Offer.mirror`) for the other, and keeping it merges them
  (`_merge_axis`: signed when the held raw values sit either side of half way from a rest at 0).
  `feed` also tracks the machine's warnings (CANopen EMCY per node, single-frame J1939 DM1 per
  address): those raised while a step is held are reported with it, CANopen ones become
  `emcy_names()` for the profile, and `begin` waits in `SETTLE` until they clear (at most
  `SETTLE_MAX`). Housekeeping streams (NMT, SYNC / EMCY, SDO, heartbeat; J1939 requests, TP, DM1)
  are never offered. `STAGE_NOTES` say what each stage expects (interlocks included).
* **Values other profiles know** (`core/suggest.py`): for each stream nothing decodes (not the
  profile in use or what it extends, not the SAE tables), every other profile's signals on that
  ID / PGN are judged against up to 400 payloads (`judge`: fits the frame, enum covers the raw
  values, mostly within `low` / `high`; moving and sane counts more than constant; a bad value
  costs twice a good one). A stream is offered from the profile that fits the most of this bus
  (then nodes matched, then score). `from_watch` (handheld: the Profile builder page's **Add
  all**, and `Guide.hints` for the offers) and `from_frames` (`canopener-cli suggest`).
* **Saving observed values** (`core/observed.py`): `save_entries` adds to the profile in use
  (`in_use_target`: a user profile directly; a built-in or plugin one through
  `observed-<id>-additions.json`, which `extends` it) or saves a new profile that `extends` the
  profile in use. Profiles keep the streams heard (`"observed": {"bus": [...]}`), for matching.
* **Which module is missing** (`core/roster.py`): `check(session, profile)` puts the modules a
  profile expects (`expect`, else `detect`) beside every module heard: on the bus, went quiet
  (silent for `quiet_after` its usual gap, at least 2 s), not heard (after `SETTLE` s of traffic;
  *waiting* before), or the whole bus quiet. A CANopen node counts only from frames it sends
  (heartbeat, TPDO, EMCY, SDO answer), never RPDOs / SDO requests to it, and an unlisted node only
  once the sniffer has confirmed it. `Alerts` turns successive checks into one-off messages (the
  handheld's toast and `module` sound, the desktop's status bar); `remember` saves the modules
  heard as the profile's `detect` list. Screens: the desktop's Network tab (banner, rows) and Codes
  tab (banner when something's wrong, `gui/module_check.py`), the handheld's Bus and Codes pages
  (`DeviceContext.roster`, checked once a second); CLI `modules`.
* **Which profile fits** (`core/profile_match.py`): `similar` ranks profiles by how many of their
  own proprietary messages (J1939 0xFF00-0xFFFF and Proprietary A, 11-bit IDs) and recorded
  streams are on the bus; the handheld checks once a few seconds into a connection
  (`ctx.check_profiles`), the builder offers the best before it starts, and its page lists them.
* **Bus explorer** (`device/screens/explore.py`): every `FieldWatch` field as a live row, moving
  first (held still for a moment after a key press); a field page with its last minute and Name
  it, which saves through `observed.save_entries` and switches the profile on.
* **SSH tunnel** (`core/tunnel.py`, `deploy/upload-server/tunnel/`): `ssh -N -R` from the unit to a
  locked-down sshd container beside the upload server; the unit's key is enrolled over the
  upload API and can only listen on its own loopback port there.
* **Remote help** (`core/remote.py`, `device/remote_actions.py`, `deploy/upload-server` `/api/remote`):
  the unit opens a session on the upload server and long-polls it for commands (`RemoteLink`, a
  thread); the helper's end is `Operator` / `canopener-cli remote`. An action is a registered
  function with a flag (or a function of its arguments) saying whether it only reads; one that
  doesn't calls `confirm`, which on the handheld posts a default-No dialog to the UI thread
  (`remote_actions.Asker`, `DeviceContext.post`) and waits up to a minute. The server relays and
  stores nothing. Profile uploads (`upload.upload_profile`) use the same server.
* **Installed options set the Pi up** (`core/bootconfig.py`, `deploy/raspberry-pi/canopener-boot-config`):
  `plan(hw)` turns the options for parts on the Pi's pins (`PARTS`) into helper tokens and compares
  them with the marked block in `config.txt` (readable without root); `apply` runs the helper's
  `set` (rewrites only that block, backing the file up first) and `live` (dtparam / dtoverlay now,
  for `LIVE` parts) through `run_privileged`, and records when a restart is still needed. The
  helper is root-owned, standard library only, imports nothing of ours, and checks every token
  against its own table; `tests/test_bootconfig.py` keeps that table, `LINES` here,
  `config-hardware.txt` and `config-controls.txt` equal. Screens: the handheld's Installed options
  page; CLI `hardware --apply`.
* **Honest sends** (`ElmBackend._raise_if_not_sent`): when the adapter says a frame never reached
  the bus (`TX_FAILED`) and nothing answered, `send` raises, so `Session.send_frame` reports it
  (`health.tx_error`) and doesn't log the frame; `scan.run_scan` gives up after a first request
  that couldn't be sent. `capture_report.own_request` tells a tool's own requests from what the
  vehicle said, for the "nothing answered" lines in the summary and README.
* **Thin modules** (`ObdClient.thin_ecus`, `core.obd.THIN_NOTE`): a module that lists no live
  values at all gets a warning beside its empty code list. `sim/thin_kline.py` plays one.
* **K-line deep probe** (`can/kline_probe.py`): read-only discovery for a K-line computer whose
  generic answers are thin: generic OBD-II asked anyway, each wake-up, then the standard KWP2000
  reads addressed to the module. It talks to the adapter directly (`command(..., keep_progress=True)`
  keeps the 'BUS INIT' lines) while holding the backend's lock, then runs `_init()` again. The
  handheld (`DeviceContext.kline_probe`) records a log around it and writes `kline_probe.txt` into
  that capture, so it uploads. docs/BLUETOOTH-OBD.md, "Thin K-line computers".
* **KWP2000 session** (`can/kwp.py`): once the probe has shown a module keeps more behind a
  session addressed to it alone, `KwpSession` opens it (header, wake-up, tester-present
  keep-alive), sends one request at a time and gives the positive answer or raises
  (`KwpNegative` / `KwpError`), and `close()` sets the adapter up again for OBD-II. Transport
  only: a plugin supplies the requests and their meaning (and confirms any that change the vehicle).
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
* Uploads (`core/upload.UploadQueue`) run on their own thread and write only the capture's
  `upload.json` and the queue file; the UIs read the state when they refresh.
* Front-ends **poll**: the Qt app refreshes the visible tab every 200 ms. This is deliberate. It
  maps directly onto a Pygame frame loop, and nothing on the rx thread ever waits on the UI.

## Mapping onto the CANopener device

| Device concept (design notes) | Where it lives now |
|---|---|
| MCP2518FD on SPI → `can0` | `can.backends.SocketCanBackend` (no code changes needed) |
| `can_logger.py` foundation | `Session.start_recording()` → `can.logfile.LogWriter` (candump format) |
| Source-address filtering, dropout detection | `Session.stats` / `ecus` (first/last seen, rate per PGN); `core/roster.py` names modules that are missing or went quiet |
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
  `REL_X`) and the button ADC's resistor stacks (`ButtonAdc`: a second ADS1115 at 0x49, read on its
  own thread, which also reads the battery for `core/battery.py`) into actions: up / down / left /
  right, select (hold = back), back, home, soft keys 1-5, rotate, log, tag, hold. While Doom runs,
  `doom_controls.ButtonKeys` passes the stacks on as a uinput keyboard. `ButtonAdc` ignores a stack that sends presses on its
  own (a floating input: `flooding`), and takes the Control mapping page's changes
  (`stack_actions`, `stack_levels`, muted stacks); `screens/controls.py` is that page, which takes
  the hardware's input for itself while it tests (`DeviceApp._testing`).
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

### Serial lines

`serialline/` is the transport for controllers whose service console talks over a serial line
(RS-232, logic level, or a current loop through an optocoupler tap: [SERIAL-SNIFFING.md](SERIAL-SNIFFING.md)).
`framing.Framing` is the baud rate and framing; `port.Tap` reads one port on a thread and stamps
each chunk with `time.time()`, like `CanFrame`; `port.Sniffer` is one tap per direction of a link.
A capture records a sniffer to `serial.log` (`serialline/log.py`, `Capture.start(serial=...)`),
and `core/serial_analysis.py` rebuilds messages from the chunks and reports message kinds, byte
roles (`changes._byte_kind`), checksums that fit, exchanges, text and what moved at each
timestamp (`serial_analysis.txt` at save, `canopener-cli serial analyze`). Writing to a line is
off unless a tap is opened with `allow_write=True`; the front-end confirms each send first. The
Zapi console mode ([ZAPI-CONSOLE.md](ZAPI-CONSOLE.md)) is the first user.

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

`core/calibration.py` holds a zero offset and gain per channel (`meters.cal` in `settings.json`).
The hub applies them as samples arrive, so live views and captures see corrected values; raw
listeners (`hub.add_listener(fn, raw=True)`) get the uncorrected ones. `CalSession` is the
Full / Quick flow as a single-threaded state machine; the handheld's screen
(`device/screens/calibrate.py`) feeds it readings and keys, and a desktop screen could do the same.

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
* **Two CAN channels.** *Partly done.* The unit's own CAN connectors are **ports**, named by where
  they are (**CAN 1**, **CAN 2**: `Hardware.can_ports()`, from the SPI device, so the names hold
  whatever Linux called the interfaces). Each can be switched off (`can.ports_off`). One port is
  the **main bus**: every screen (codes, live data, bus, transmit) works on its session, as before.
  The other can be **watched** (`can.second` = `watch`, `can.second.bitrate`): a second, always
  listen-only `Session` opened with the main one (`DeviceContext.second`), with its own lamp
  (`bus2`) and its own file in a capture (`capture-can2.log`, `meta["second"]`). A watched bus
  that won't open never costs the main connection. `swap_ports()` makes the other port main and
  swaps the bitrates. Handheld only so far. What's left:
  * **Show too:** live values and modules from the watched bus beside the main one, each row
    tagged with its port, the way meter channels sit beside CAN values now. Needs a port on
    live-data row keys and on `core.canopen.source_name` sources.
  * **A profile per port**, for a machine with J1939 on one bus and CANopen on the other. Today
    the profile applies to the main bus; the watched bus only says which protocols it has heard.
  * **The capture's outputs** (`traffic.csv`, the summary, the report) read the main bus only; the
    second log is saved raw.
  * **Open question:** whether codes should ever be read on the watched bus (that means
    transmitting there). Swapping ports does it today.
* **CAN FD.** The backend reads classic 16-byte frames only. Add `CAN_RAW_FD_FRAMES` and 72-byte
  frames (J1939-22 uses FD); the MCP2518FD and CANable 2.0 both support it.
* **Team access, the rest.** The handheld joins by request only (no keyboard for an invite link:
  a QR code or a short code typed with the encoder would do). Possible next steps: per-install
  upload credentials issued at join (replacing the shared `UPLOAD_TOKENS`). *Done:* the admin
  page on the server (`/admin`) for approving from a phone.
* **Handheld screens still to come:** CANopen (nodes, EMCY, error history), clear codes and
  transmit (confirm-first, default No, never in listen-only), the logic analyzer (`core/logic.py`
  has the command and channel names, no UI yet) and Component Test (ELECTRONICS.md 6.7).
* **CANopen:** read PDO mappings (0x1A00..) to suggest signal layouts.
* **Transmit:** more actions on the confirm-first path (saved frame replay). Machine-changing OEM
  commands we worked out ourselves can live here (with their evidence, CLAUDE.md "Provenance");
  ones from dealer tools stay in private plugins.
* **Bluetooth OBD adapters:** check everything against a real adapter and vehicles, then
  multi-PID requests and the non-CAN Mode 06 decoder: [BLUETOOTH-OBD.md](BLUETOOTH-OBD.md#gaps-to-fill).
* **Ideas from the first thin K-line machine (2026-11-13), not done yet.** That day's work is in
  [BLUETOOTH-OBD.md](BLUETOOTH-OBD.md#thin-k-line-computers-the-deep-probe); the health watch,
  honest captures, the thin-answer warning and the simulated thin computer came out of it. Still open:
  * **One risk ladder for everything that transmits.** "It's a read" turned out not to mean safe.
    Three tiers, used by every front-end and scan: standard reads (free), reads of undescribed
    ranges (engine off, warned, default No), writes (confirmed). The UDS identifier scans and the
    module finder on CAN vehicles should sit on it too.
  * **The health watch beyond the K-line probe:** the UDS module finder, CANopen scans and any
    plugin reader, with engine speed as well as voltage where one is known. It needs a home in
    `core/` (the probe's lives in `can/kline_probe.py` because `can/` can't import `core/`).
  * **Generalise the probe ladder** (ask anyway, try request shapes, sample, sweep only described
    ranges) to UDS over CAN and to CANopen object dictionaries.
  * *Done:* the "what changed between these markers" report (`core/changes.py`) and structured
    probe results (`kline_probe.json`). Next for them: the same screen on the desktop app, a live
    version that compares as you tag (no saved log needed), the K-line value recorder's CSV as an
    input, and scale suggestions from a meter channel (battery voltage, a pressure gauge).
  * *Done:* remote help through the upload server, and the SSH tunnel beside it. Still open: the
    deep probe and the value recorder as remote actions, and the same page on the desktop app.
  * *Done:* the guided Profile Builder (handheld), with questions that build the script
    (`guide.QUESTIONS`, `build_script`); after its first real run (an H80UT, 2026-11-17: five of
    seven kept values were noise) the live pick, the hands-off filtering, saving on top of the
    profile in use, profile matching and the bus explorer. After the second (a Yale NR045 reach
    truck, 2026-11-25): electric stand-ups (reach and counterbalance), a control's two directions
    as one signal, interlock warnings expected, named and waited out, three timestamps per step
    and the record in the capture, and values borrowed from other profiles (`core/suggest.py`).
    Next: more kinds of machine (generator, scissor lift, order picker), the explorer, the live
    pick and borrowing on the desktop app, borrowing across node IDs (the same device at another
    CANopen node, matched by its identity strings), the team's shared profiles from the upload
    server as a source, reading a node's PDO map over SDO (asked first) to get field positions
    without guessing, steps that expect three states (forward / neutral / reverse), merging bits that
    always change together into one multi-bit value, a "press when you move it" key matched
    against the changes, and the standard ETC5 switches (transmission forward / neutral /
    reverse, PGN 65219) in `j1939/signals.py` once their bit positions are checked against a
    public source (the starter mode in EEC1 is in, as a raw number).
  * **Upload one result without a whole log**, and ask for the machine on the handheld before an
    upload (every capture that day went up with no machine and no concern).
  * **Finish the "not checked" rows** of BLUETOOTH-OBD.md's gaps on real vehicles (CAN OBD timing,
    J1939 through the adapter, response pending, the vLinker's own commands).
  * **Smaller defects seen that day:** `capture.log`'s first answer can be written before its
    request and a request can be missing (non-CAN vehicles); the adapter trace header says
    "protocol not found yet" and gives the ELM version rather than the STN chip; the probe's
    "(n bytes)" counts are off by one for answers that don't echo their option byte.
* **Serial lines on the screens.** Recording and analysis are CLI-only so far, plus the Zapi
  console mode: the Log tool on both front-ends should offer serial lines next to the meters, and
  `Hardware` could list a USB-serial adapter as an installed option. A live version of the
  analysis (kinds and checksums as you record) would help at the machine.
* **Handheld text entry.** The handheld picks machine details from the bus or the recent list but
  can't type them (or the concern / question): an on-screen keyboard would cover those, the upload
  server's address and token, and naming timestamps.

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
