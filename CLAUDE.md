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

Headless Linux needs `libegl1 libxkbcommon0 libfontconfig1 libdbus-1-3` for Qt.

## Rules

* **Diagnostics are read-first; transmitting is a separate, confirm-first path.** The receive/scan
  code only ever transmits standard J1939 Requests (PGN 0xEA00) and TP flow control. Active sending
  lives apart in `core/transmit.py` + `gui/transmit_dialog.py`: off by default, never in listen-only,
  and every frame is confirmed (exact CAN ID + bytes + destination) and logged. Only **generic,
  publicly documented** frames belong here (a J1939 Request, a raw frame the user composes). Do
  **not** add OEM/proprietary commands, or fault-clearing/reset commands (DM3/DM11, OBD Mode 04),
  to this repo — a separately installed plugin registers those as `TransmitAction`s via
  `register_action` / `PluginApi.register_transmit_action`, and they confirm with the user before
  changing a machine.
* **This repo is public: no OEM dealer-tool data** (extracted fault tables, PID dictionaries,
  node maps, OEM text). It lives in the private plugin repo or `~/.config/canopener/profiles/`.
  `tests/test_repo_hygiene.py` enforces file names and markers; keep its lists current.
* **Layering.** `j1939/` and `can/` must not import `core/`, `gui/` or `sim/`. `core/` must not import
  Qt. The GUI is one of several front-ends (CLI now, a Pygame instrument UI later).
* **No new runtime dependencies below `gui/`.** Standard library only, so it stays light on a Pi.
* GUI code must work with both PySide6 and PyQt6: import from `canopener.gui.qt` and use fully
  scoped enums (`Qt.AlignmentFlag.AlignLeft`).
* Don't invent OEM data. SPN/SA/PGN names in the built-in tables are SAE-standard only. OEM labels
  belong in profiles, and the sim profile is explicitly marked as simulated.
* The simulator's fault codes are asserted by tests. Change both together.
* `legacy/` is frozen reference code. Don't import it, and don't copy J1939 tables from
  `legacy/can_opener_v0.1`: they're wrong (see legacy/README.md).
* Anything that runs as root goes through `core.interfaces.run_privileged` as explicit argv
  lists (never `sh -c` with user input). Keep `deploy/raspberry-pi/canopener.sudoers` matching
  the commands `setup_steps()` produces.
* Capture logic (tagging rules, saving, summaries) lives in `core/capture*.py` so the future
  on-device UI shares it; `gui/log_panel.py` is only the screen. Captures must stay recoverable:
  write `capture.json` / `markers.csv` on every change while recording.
