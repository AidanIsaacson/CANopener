# CANopener: notes for Claude Code

J1939 / CANopen / OBD-II / CAN diagnostics. Python 3.10+. Every doc is listed in docs/README.md;
start with docs/ARCHITECTURE.md (software structure, open work) and docs/DEVELOPING.md (dev setup,
profile format, plugins).

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

## Docs: where things go

User docs are for mechanics: plain words, menu paths in **bold**, no internals. Say each fact in
one place and link to it. `tests/test_docs_links.py` checks every relative link and `#anchor`, so
renaming a heading means fixing its links.

* `README.md`: the front page. Keep it short (what, why, install, connect, links). New features
  don't go here: what and why in `docs/FEATURES.md`, how in `docs/USAGE.md` (screens, Log tool,
  what it sends, CLI, file locations).
* `docs/INSTALL.md`: picking a setup, updating, plugins. Step by step per platform in
  `docs/install/`; folder reference in `deploy/*/README.md`.
* The handheld: `docs/DEVICE.md` (UI), `docs/DESIGN.md` and `docs/ELECTRONICS.md` (the owner's
  hardware notes; edit only what a change makes stale), `hardware/`, `firmware/`.
* Code: `docs/DEVELOPING.md`, `docs/ARCHITECTURE.md` (move an item out of "Open work" when it lands).
* Screenshots: `docs/images/`, retaken by `scripts/readme_screenshots.py` (which runs
  `scripts/device_screenshots.py` for the handheld's), in the Element Zero theme.

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
* **Installed options** (`core/hardware.py`): a feature that needs on-deck hardware checks
  `Hardware.available(id)` first, and a part that's "not installed" is never opened or polled.
  Detection only looks (sysfs, `/dev`), never opens a device. Hardware I/O stays on its own thread.
  Electronics decisions and pinouts are `docs/ELECTRONICS.md` (keep its pin tables, section 19,
  `deploy/raspberry-pi/config-hardware.txt` and `firmware/meter-pico/config.py` in step).
  `firmware/` is never imported by `canopener/`; `hardware/` will hold the KiCad board.
* **Measurements** (meters, probes, anything read besides CAN) go through `core/measure.py`
  (`MeasurementHub`, wall-clock timestamps like `CanFrame`); sources are in `core/meters.py`.
  A capture records them to `measurements.csv` and the report merges them with the CAN data.
* **Colours come from the theme** (`core/themes.py`): the handheld names them by role
  (`ui.color("dim")`, `ui.panel` / `ui.card`), the desktop uses the palette or `gui/themes.tint()`
  / `fill_style()`. Don't hard-code a colour a theme can't reach. Element Zero is the handheld's
  default; the desktop's is Standard (the system look).
* GUI code must work with both PySide6 and PyQt6: import from `canopener.gui.qt` and use fully
  scoped enums (`Qt.AlignmentFlag.AlignLeft`).
* **Codes and names: write them by hand, from public sources; never invent or copy them.** Short
  descriptions in our own plain words are the preferred way to cover a code, rather than leaving it
  out:
  * **SAE standard** names go in the built-in tables (`j1939/spn.py`, `addresses.py`, ...). Those
    tables are SAE-standard only. Word the names yourself: the J1939 Digital Annex can't be
    redistributed.
  * **Manufacturer-specific** codes go in a profile for that maker or machine, never in the
    built-in tables. That means J1939 SPNs 516096-524287, proprietary PGNs, and OBD-II
    P1xxx / B, C, U1xxx and the like: the same number means different things per maker. Use the
    profile's `"spns"`, `"pgns"` or `"emcy"`. Base them on a **public** source (the maker's
    published manual, fault-code list or bulletin) or on our own bus captures. Name the source in
    the profile's `"description"`.
  * **Never** from dealer-tool data, paid or licensed manuals, or anything under NDA, however it's
    reworded: those stay in the private plugin. Never copy text word for word, and if a code's
    meaning isn't confirmed, mark it unconfirmed (`?`) instead of guessing.
  * Generic OBD-II code text is `obd/dtc_codes.py`, generated from python-OBD: regenerate it,
    don't hand-edit it. The sim profile is explicitly marked as simulated.
* **License: GPL-2.0-or-later.** Anything copied in must be GPL-2.0-compatible; keep attribution
  (README "License" and the copied file's header).
* The simulators' fault codes (J1939 `sim/machine.py`, CANopen `sim/canopen.py`) are asserted by
  tests. Change both together.
* Protocols are per profile (`j1939`, `canopen`, `j1939+canopen`, `obd2`; generic profile for each in
  `profiles.GENERIC`). CANopen and J1939 share tabs but each shows only when the profile says so:
  check `profiles.speaks_j1939()` / `speaks_canopen()`, never "is it on the bus". Row sources are
  `core.canopen.source_name` ints (J1939 address, `node_src`, `id_src`). EMCY text is the CiA 301 class; OEM text only via a
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
* Plugins installed from GitHub (`core/plugin_repos.py`, sign-in `core/github.py`): standard library
  only, no root, git pull --ff-only, and the token goes to git only through `github.git_env` (never argv
  or `.git/config`). Never name a private repository in this public repo.
* Capture logic (tagging rules, saving, summaries) lives in `core/capture*.py` so the future
  on-device UI shares it; `gui/log_panel.py` is only the screen. Captures must stay recoverable:
  write `capture.json` / `markers.csv` on every change while recording.
* Uploads (`core/upload.py`): standard library only; the server's token lives in
  `upload-token.json` (mode 600) or `CANOPENER_UPLOAD_TOKEN`, never in `settings.json`, argv or a
  URL. The GitHub uploader (`core/upload_github.py`) uses the `core.github` sign-in in API headers
  only and refuses public repositories. `deploy/upload-server/server.py` stays one standard-library file; keep its README's API
  table, `HttpUploader` and `tests/test_upload.py` in step. A capture's `README.md`
  (`core/capture_readme.py`) is rendered from `capture.json` alone, so it's rewritten on every change.
