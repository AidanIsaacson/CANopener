# CANopener: notes for Claude Code

J1939 / CANopen / OBD-II / CAN diagnostics. Python 3.10+. See README.md (usage, written for mechanics: keep it
plain), docs/DEVELOPING.md (dev setup, profile format, plugins), docs/DESIGN.md (the hardware device
this is for) and docs/ARCHITECTURE.md (software structure).

## Commands

```bash
pip install -e ".[gui,dev]"      # PySide6 + pytest + ruff
pip install -e ".[device]"       # Pygame, for the handheld UI
pytest                           # full suite, incl. headless GUI tests (QT_QPA_PLATFORM=offscreen)
ruff check .                     # lint (config in pyproject.toml)
canopener --sim                  # GUI against the simulator
canopener-cli scan --sim --full  # CLI against the simulator
canopener-cli canopen --sim --info --errors   # CANopen CLI against the CANopen simulator
canopener-device --sim           # the handheld UI (Pygame) against the simulator + a simulated meter
```

Headless or minimal Linux needs `libegl1 libxkbcommon0 libfontconfig1 libdbus-1-3 libxcb-cursor0` for Qt.

## Rules

* **Reads are free; anything that changes a vehicle asks first.** The J1939 receive/scan code only
  transmits standard J1939 Requests (PGN 0xEA00) and TP flow control. Standard, **publicly
  documented** commands are welcome in this repo: OBD-II Mode 04 (clear codes) and Mode 08
  (on-board tests) in `core/obd.py`, J1939 DM11 / DM3 code clears in `core/j1939_clear.py` (shared
  warning text: `j1939_clear.warning()`), UDS ClearDiagnosticInformation / ECUReset in `core/uds.py`,
  CANopen NMT commands and the error-history clear (0x1003 sub 0 = 0) in `core/canopen.py`
  (warning text: `warning_nmt` / `warning_clear`), and the composable frames in `core/transmit.py`. Every front-end confirms with the user before
  sending one (state what it changes, default No), and nothing transmits in listen-only. The
  Transmit window (`gui/transmit_dialog.py`) stays off by default and confirms each frame.
  **OEM/proprietary** commands (routines, I/O controls, DIDs from dealer tools) still belong in the
  separately installed plugin, which registers them via `register_action` /
  `PluginApi.register_transmit_action`.
* **This repo is public: no OEM dealer-tool data** (extracted fault tables, PID dictionaries,
  node maps, OEM text). It lives in the private plugin repo or `~/.config/canopener/profiles/`.
  `tests/test_repo_hygiene.py` enforces file names and markers; keep its lists current.
* **Layering.** `j1939/`, `canopen/` and `can/` must not import `core/`, `gui/` or `sim/` (a test checks
  `canopen/`). `core/` must not import
  Qt or Pygame. Front-ends: the desktop GUI (`gui/`, Qt), the CLI, and the handheld UI (`device/`,
  Pygame; docs/DEVICE.md). In `device/`, only `app.py`, `ui.py` and `screens/` import Pygame:
  `context.py` and `input.py` stay Pygame-free (a test checks). Logic both UIs need goes in `core/`.
  `frontends.py` switches between the two graphical UIs (the shared `ui` setting).
* **No new runtime dependencies below `gui/` / `device/`.** Standard library only, so it stays light
  on a Pi. Pygame is the `device` extra, like PySide6 is the `gui` extra. python-can is the optional
  `adapters` extra: `can/pythoncan.py` imports it only when an `interface:channel` adapter is opened,
  and Linux interfaces (can0) always use the built-in `SocketCanBackend`. Keep it that way.
* **Settings both UIs share** (connection, units, profile, sound, meters, the handheld's controls)
  live in `core/settings.py` (`settings.json`); the desktop GUI still keeps its own in `QSettings`.
* **Measurements** (meters, probes, anything read besides CAN) go through `core/measure.py`
  (`MeasurementHub`, wall-clock timestamps like `CanFrame`); sources are in `core/meters.py`.
  A capture records them to `measurements.csv` and the report merges them with the CAN data.
* GUI code must work with both PySide6 and PyQt6: import from `canopener.gui.qt` and use fully
  scoped enums (`Qt.AlignmentFlag.AlignLeft`).
* Don't invent OEM data. SPN/SA/PGN names in the built-in tables are SAE-standard only. OEM labels
  belong in profiles, and the sim profile is explicitly marked as simulated. Generic OBD-II code
  text is `obd/dtc_codes.py`, generated from python-OBD: regenerate it, don't hand-edit it, and
  never describe manufacturer-specific codes (P1xxx, B/C/U1xxx ...).
* **License: GPL-2.0-or-later.** Anything copied in must be GPL-2.0-compatible; keep attribution
  (README "License" and the copied file's header).
* The simulators' fault codes (J1939 `sim/machine.py`, CANopen `sim/canopen.py`) are asserted by
  tests. Change both together.
* CANopen shares the J1939 screens (no separate mode): row sources are `core.canopen.source_name`
  ints (J1939 address, `node_src`, `id_src`). EMCY text is the CiA 301 class; OEM text only via a
  profile's `"emcy"`.
* `legacy/` is frozen reference code. Don't import it, and don't copy J1939 tables from
  `legacy/can_opener_v0.1`: they're wrong (see legacy/README.md).
* Anything that runs as root goes through `core.interfaces.run_privileged` as explicit argv
  lists (never `sh -c` with user input). Keep `deploy/raspberry-pi/canopener.sudoers` matching
  the commands `setup_steps()` produces.
* The handheld's button wiring is `deploy/raspberry-pi/config-controls.txt`: keep its key codes
  matching `device/input.DEFAULT_KEYS` (a test checks) and its pins out of the reserved buses.
* Pi-only work can happen on a Pi branch; bring `main` into it with `scripts/sync-from-main.sh` (a
  merge, never a rebase). Keep handheld changes to shared files small so those merges stay clean.
* First-plug-in adapter support lives in `deploy/linux/` (`install.sh` installs and updates;
  `adapter-setup.sh` adds drivers + the CANable udev rule / slcand unit). Keep the USB IDs in
  `70-canopener.rules` matching `interfaces.USB_SLCAN` (a test checks), and keep
  `docs/install/` in step with the installers.
* Capture logic (tagging rules, saving, summaries) lives in `core/capture*.py` so the future
  on-device UI shares it; `gui/log_panel.py` is only the screen. Captures must stay recoverable:
  write `capture.json` / `markers.csv` on every change while recording.
