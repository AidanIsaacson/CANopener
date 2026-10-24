# CANopener: notes for Claude Code

J1939 / CAN diagnostics. Python 3.10+. See README.md (usage), docs/DESIGN.md (the hardware device this is
for) and docs/ARCHITECTURE.md (software structure).

## Commands

```bash
pip install -e ".[gui,dev]"      # PySide6 + pytest + ruff
pytest                           # full suite, incl. headless GUI tests (QT_QPA_PLATFORM=offscreen)
ruff check .                     # lint (config in pyproject.toml)
canopener --sim                  # GUI against the simulator
canopener-cli scan --sim --full  # CLI against the simulator
```

Headless or minimal Linux needs `libegl1 libxkbcommon0 libfontconfig1 libdbus-1-3 libxcb-cursor0` for Qt.

## Rules

* **Reads are free; anything that changes a vehicle asks first.** The J1939 receive/scan code only
  transmits standard J1939 Requests (PGN 0xEA00) and TP flow control. Standard, **publicly
  documented** commands are welcome in this repo: OBD-II Mode 04 (clear codes) and Mode 08
  (on-board tests) in `core/obd.py`, UDS ClearDiagnosticInformation / ECUReset in `core/uds.py`,
  and the composable frames in `core/transmit.py`. Every front-end confirms with the user before
  sending one (state what it changes, default No), and nothing transmits in listen-only. The
  Transmit window (`gui/transmit_dialog.py`) stays off by default and confirms each frame.
  **OEM/proprietary** commands (routines, I/O controls, DIDs from dealer tools) still belong in the
  separately installed plugin, which registers them via `register_action` /
  `PluginApi.register_transmit_action`.
* **This repo is public: no OEM dealer-tool data** (extracted fault tables, PID dictionaries,
  node maps, OEM text). It lives in the private plugin repo or `~/.config/canopener/profiles/`.
  `tests/test_repo_hygiene.py` enforces file names and markers; keep its lists current.
* **Layering.** `j1939/` and `can/` must not import `core/`, `gui/` or `sim/`. `core/` must not import
  Qt. The GUI is one of several front-ends (CLI now, a Pygame instrument UI later).
* **No new runtime dependencies below `gui/`.** Standard library only, so it stays light on a Pi.
* GUI code must work with both PySide6 and PyQt6: import from `canopener.gui.qt` and use fully
  scoped enums (`Qt.AlignmentFlag.AlignLeft`).
* Don't invent OEM data. SPN/SA/PGN names in the built-in tables are SAE-standard only. OEM labels
  belong in profiles, and the sim profile is explicitly marked as simulated. Generic OBD-II code
  text is `obd/dtc_codes.py`, generated from python-OBD: regenerate it, don't hand-edit it, and
  never describe manufacturer-specific codes (P1xxx, B/C/U1xxx ...).
* **License: GPL-2.0-or-later.** Anything copied in must be GPL-2.0-compatible; keep attribution
  (README "License" and the copied file's header).
* The simulator's fault codes are asserted by tests. Change both together.
* `legacy/` is frozen reference code. Don't import it, and don't copy J1939 tables from
  `legacy/can_opener_v0.1`: they're wrong (see legacy/README.md).
* Anything that runs as root goes through `core.interfaces.run_privileged` as explicit argv
  lists (never `sh -c` with user input). Keep `deploy/raspberry-pi/canopener.sudoers` matching
  the commands `setup_steps()` produces.
* First-plug-in adapter support lives in `deploy/linux/` (`install.sh` installs and updates;
  `adapter-setup.sh` adds drivers + the CANable udev rule / slcand unit). Keep the USB IDs in
  `70-canopener.rules` matching `interfaces.USB_SLCAN` (a test checks), and keep
  `docs/install/` in step with the installers.
* Capture logic (tagging rules, saving, summaries) lives in `core/capture*.py` so the future
  on-device UI shares it; `gui/log_panel.py` is only the screen. Captures must stay recoverable:
  write `capture.json` / `markers.csv` on every change while recording.
