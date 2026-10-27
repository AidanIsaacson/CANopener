# Developing CANopener

For people changing the code, writing machine profiles or plugins. Using the app is in the
[README](../README.md); how the code is laid out is in [ARCHITECTURE.md](ARCHITECTURE.md).

## Set up (no hardware needed)

```bash
git clone https://github.com/AidanIsaacson/CANopener.git
cd CANopener
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[gui,dev]"

canopener --sim            # desktop GUI connected to the simulated telehandler
canopener-cli scan --sim --full
pytest                     # the tests, including headless GUI tests
ruff check .               # lint
```

Headless or minimal Linux needs `libegl1 libxkbcommon0 libfontconfig1 libdbus-1-3 libxcb-cursor0`
for Qt. The GUI works with PySide6 (installed by `pip install -e ".[gui]"`) or the distro's PyQt6
(`sudo apt install python3-pyqt6`). The core and CLI have **no dependencies** beyond Python 3.10+.

**The simulators.** *Simulator* is a Deutz-style engine ECU (SA 00) and a machine controller
(SA 27): it warms up, revs, throws an intermittent low-oil-pressure code at idle, and answers
requests exactly like a real ECU (single frame, BAM and RTS/CTS transport). *OBD-II simulator
(car)* is a two-module car (engine + transmission) answering every OBD-II mode, on 11- or 29-bit
IDs. *Simulate the selected profile* broadcasts a profile's signals (below).

### Testing against a virtual bus

```bash
sudo modprobe vcan && sudo ip link add dev vcan0 type vcan && sudo ip link set vcan0 up
canopener-cli sim --iface vcan0      # terminal 1: the simulated machine
canopener --iface vcan0              # terminal 2: the GUI, over real SocketCAN
candump vcan0                        # terminal 3: watch the traffic (can-utils)
```

### README screenshots

`python scripts/readme_screenshots.py` retakes `docs/images/*.png` from the app running offscreen
against the simulators (about a minute). Or replace any of them with your own.

## Machine profiles

Profiles label what the standard tables can't: OEM controllers, manufacturer-specific SPNs
(516096-524287) and proprietary PGNs, plus extra live signals. Built-in examples are in
[`canopener/profiles/`](../canopener/profiles); copy one into `~/.config/canopener/profiles/` and
edit it. The full format is documented at the top of
[`canopener/core/profiles.py`](../canopener/core/profiles.py). Signals can be:

* whole bytes (`"length": 1/2/4`) or bit fields (`"bit": 4, "bits": 12`, spanning bytes in J1939 order)
* `"signed"`, scaled (`"scale"`, `"offset"`), with a `"unit"` (common spellings like `Volts`, `C`,
  `kph` are normalised so unit conversion works)
* enumerated (`"enum": {"0": "Released", "1": "Applied"}`), grouped (`"group"`, used by the
  Live Data group filter), with a normal range (`"low"`, `"high"`: out-of-range values show red)
* limited to one source address or a list (`"sa": ["0x00", "0x27"]`)

Each of those is also a field in the app's **Label…** dialog. A profile signal at the same place as a built-in one replaces it, so a profile can rename and
regroup standard data too. `"protocol": "obd2"` makes a car profile (the OBD-II screens instead of
the J1939 ones).

**Building on another profile:** `"extends": "generic"` (a profile id) starts from that profile:
its module names, SPN / PGN names, signals, detect section and protocol carry over, and this file
adds or replaces things (a signal here replaces the base's at the same place). Chains work; a
missing base is reported and the profile loads on its own. Profiles made in the app (**Label…**,
**Name this module…**, **Save profile as…**; `core/labeling.py`) are written this way, so they
hold only your labels and the built-in base keeps getting updates. Unknown keys survive those
saves, so hand edits are safe.

**Recognising a machine:** a `"detect": {"addresses": [...], "min_match": 2}` section lists the
module addresses that machine has. When the bus matches, a banner offers to switch profiles.

**Simulating a profile:** pick the profile, tick **Connection → Show simulators**, choose **Simulate
the selected profile** as the source, and Connect. Every signal is broadcast from its address with moving values, so you can
check a new profile's decoding and layout without the machine (`canopener-cli --profile ID sim
--iface vcan0` does the same on a SocketCAN bus).

**Keep OEM data private.** Profiles generated from dealer-tool data belong in
`~/.config/canopener/profiles/` or a private plugin, never in this public repository (a test
fails if such files appear here).

## Plugins

Separately installed Python packages can extend CANopener without changing this repository: a
package declares an entry point in the `canopener.plugins` group and can then add tabs and menu
actions, `canopener-cli` commands, folders of profiles and transmit actions. See
[`canopener/plugins.py`](../canopener/plugins.py) for the hooks. Anything a plugin sends that
changes a machine must confirm with the user first, and `Session.send_frame` refuses in
listen-only mode.

## Project layout

```
canopener/
  j1939/     protocol library: IDs, DTCs/DMx, transport protocol, signals, address names
  obd/       OBD-II (J1979) protocol: ISO-TP, PIDs, DTCs, readiness, Mode 06, passive decoder
  uds/       UDS (ISO 14229) protocol: services, NRCs, DTC status, request builders
  can/       bus backends (SocketCAN, python-can (optional), virtual, log replay) and candump log files
  core/      session engine, scans, live-data model, OBD/UDS clients, transmit, reports, profiles,
             units, link status + signal finder, sound cues, self-update
  sim/       simulated machine (J1939) and OBD-II/UDS simulator
  gui/       desktop GUI (Qt) - a thin front-end over core/
  cli.py     command-line front-end
deploy/      Linux installer + USB adapter plug-in rules; Raspberry Pi launcher, service, sudoers
legacy/      the original j1939_codes.py script and the v0.1 Pi app (frozen, for reference)
tests/       pytest suite, including headless GUI tests
docs/        DESIGN.md (hardware + concept), ARCHITECTURE.md (software), install/ (guides)
```

## Files the app writes

| Path | What |
|---|---|
| `~/canopener/logs/` | Captures from the Log tool, one folder each |
| `~/canopener/reports/` | Saved reports (plus the raw log alongside) |
| `~/.config/canopener/profiles/*.json` | Your machine profiles |
| `~/.config/canopener/profile_list.json` | Which profiles the picker shows, and their order |
| `~/.config/canopener/spn_names.csv` | Optional extra SPN names, `spn,name` per line |
