# Developing CANopener

For people changing the code, writing machine profiles or plugins. Using the app is in
[USAGE.md](USAGE.md); how the code is laid out is in [ARCHITECTURE.md](ARCHITECTURE.md). The
project rules (layering, what may be sent, no OEM data) are in [CLAUDE.md](../CLAUDE.md): read
them before a first change.

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
IDs. *Bluetooth OBD simulator (car)* is that car (or an older J1850 / K-line one, or the J1939
machine) behind a simulated ELM327 adapter that speaks the adapter's real text protocol
([BLUETOOTH-OBD.md](BLUETOOTH-OBD.md#testing-it-yourself)). *Simulate the selected profile*
broadcasts a profile's signals (below).

### Testing against a virtual bus

```bash
sudo modprobe vcan && sudo ip link add dev vcan0 type vcan && sudo ip link set vcan0 up
canopener-cli sim --iface vcan0      # terminal 1: the simulated machine
canopener --iface vcan0              # terminal 2: the GUI, over real SocketCAN
candump vcan0                        # terminal 3: watch the traffic (can-utils)
```

### Screenshots

`python scripts/readme_screenshots.py` retakes `docs/images/*.png` (used by the README,
FEATURES.md, USAGE.md and DEVICE.md) from the app running offscreen against the simulators (about
a minute), in the Element Zero colour theme (`... OUT standard` for the system look). It ends by
running `scripts/device_screenshots.py`, which retakes the handheld's `device-*.png` (pick another
theme or panel size with `--theme` / `--size`). Or replace any of them with your own.

### Keeping a Pi branch up to date with main

The handheld UI is part of `main` (since 0.2.0). Work that only makes sense on the Pi, such as
hardware experiments, can live on a branch: the handheld code is mostly its own files
(`canopener/device/`, `core/settings.py`, `measure.py`, `meters.py`, `obd_stream.py`,
`system.py`), so merges from `main` are normally clean.

```bash
scripts/sync-from-main.sh          # merge origin/main in, then lint + test
scripts/sync-from-main.sh --push   # and push it if everything passes
```

It merges rather than rebases, so the branch's history never changes under anyone. Keep handheld
changes to shared files small so those merges stay clean.

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
* on an 11-bit CAN ID instead of a PGN (`"id": "0x181"`: a CANopen PDO, or any plain CAN message)
* high byte first for 2- / 4-byte values (`"order": "big"`; the default is low byte first, as in
  J1939 and CANopen)
* text (`"type": "text"`, `"length"` 1-8): ASCII from the frame, e.g. a display's messages
  ("LOW BTRY", "M 185") or a fault string. Live Data shows the latest message (blank frames
  between blinks are skipped) with the last few others in its tooltip / under the tile; there's no
  graph or min / max. A capture's summary notes each different message the first time it shows,
  and `live_data.csv` gets the text. Not offered in the Label… dialog yet: add it in the file

**CANopen machines:** `"nodes": {"0x01": "Traction controller"}` names node IDs (like `"sources"`
for J1939 addresses), `"emcy": {"0xFF01": "..."}` describes a maker's own EMCY error codes, and
`"detect": {"nodes": ["0x01", "0x02"]}` recognises the machine from the nodes heard (alongside or
instead of `"addresses"`). [`sim-canopen.json`](../canopener/profiles/sim-canopen.json) is a
worked example for the CANopen simulator, and
[`zapi-combiacx.json`](../canopener/profiles/zapi-combiacx.json) one for a real machine (a Zapi
COMBIACX pallet truck, decoded from bus captures; marked Unconfirmed until it's been checked on
more trucks). Fields still being worked out end in `?`, and raw bytes sit in a "Raw (for
decoding)" group.

Each of those is also a field in the app's **Label…** dialog. A profile signal at the same place as a built-in one replaces it, so a profile can rename and
regroup standard data too. `"protocol"` picks the screens: `"j1939"` (the default), `"canopen"`
(the same tabs for CANopen nodes: EMCY errors and history, node states, NMT; nothing J1939),
`"j1939+canopen"` (both, for a bus that mixes them; `generic-j1939-canopen.json`), or `"obd2"`,
which makes a car profile (the OBD-II screens instead of the J1939 ones).

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

**Manufacturer-specific codes are welcome here when they come from a public source.** Use
something like the maker's published manual or fault-code list, or your own bus captures, and
write the descriptions in your own words. Put them in a profile for that maker or machine
(`"spns"` for 516096-524287, `"pgns"`, `"emcy"`), not the built-in tables, which are
SAE-standard only. Say in the profile's `"description"` where they came from, and end anything
not yet confirmed with `?`.

**Keep OEM data private.** Profiles generated from dealer-tool data, paid or licensed manuals or
anything under NDA belong in `~/.config/canopener/profiles/` or a private plugin, never in this
public repository, however they're reworded (a test fails if dealer-tool files appear here).

## Plugins

Separately installed Python packages can extend CANopener without changing this repository: a
package declares an entry point in the `canopener.plugins` group and can then add tabs and menu
actions, `canopener-cli` commands, folders of profiles, transmit actions and bus setups for
Bluetooth OBD adapters (`adapter_buses()`: adapter commands only). See
[`canopener/plugins.py`](../canopener/plugins.py) for the hooks. Anything a plugin sends that
changes a machine must confirm with the user first, and `Session.send_frame` refuses in
listen-only mode.

A plugin can also give saved captures somewhere else to go: subclass `core.upload.Uploader` (an
`id`, a `name`, `configured(settings)`, `upload(bundle, settings, progress)` returning
`{"key", "url"}`, raising `UploadError` on failure) and call `core.upload.register_uploader(...)`
at import. It then appears under **Log → Upload settings… → Send to** on the desktop (and is
used by the handheld when `upload.target` is its id). The bundle is the capture folder zipped,
README included; the queue, retries and status are handled for it.

A plugin can also hand every unit the built-in upload server's address and token, so nothing has
to be typed in on each one: call `core.upload.set_default_server(fn)` at import, where `fn()`
returns `(address, upload token)`. It's only the fallback (a unit with its own address and token
keeps using them), and it shows as "(from a plugin)". Only do this from a private plugin: the
token is a secret.

### Trying uploads locally

(Run `python -m canopener...` and `python -c` from the checkout, not from your home folder: the
data folder `~/canopener` has the package's name, and Python picks it up first there, failing with
"cannot import name ... from 'canopener' (unknown location)". The installed commands
`canopener`, `canopener-cli` and `canopener-device` work from anywhere.)

Run the receiving server against a temporary folder and point the app at it (plain http is
accepted for localhost only):

```bash
UPLOAD_TOKENS=dev READ_TOKENS=dev DATA_DIR=/tmp/uploads PORT=8765 python deploy/upload-server/server.py &
canopener-cli upload --setup        # http://127.0.0.1:8765, token: dev
canopener-cli upload ~/canopener/logs/<a capture>
curl -H "Authorization: Bearer dev" http://127.0.0.1:8765/api/captures
```

### Private plugins: installing from the device, no keyboard

**System → Software → Plugins** on the handheld (and `canopener-cli plugins` anywhere) signs in to GitHub
and installs plugins from repositories, private ones included. Nothing is typed on the device:
sign-in uses GitHub's device flow, so the screen shows a code and you approve it at
`github.com/login/device` on a phone. Then **Add a repository** lists the repositories that
sign-in can read; picking one clones it to `~/<name>`, runs `pip install -e` into the app's
environment and offers a restart. **Update all** pulls (fast-forward only) and restarts. Code:
`core/github.py` (sign-in, token store, API) and `core/plugin_repos.py` (git + pip), screens in
`device/screens/plugins.py`.

Sign-in needs a **GitHub App** (one-time setup, about two minutes, works from a phone):

1. github.com → your picture → **Settings → Developer settings → GitHub Apps → New GitHub App**.
2. Name: anything (e.g. "CANopener <your name>"). Homepage URL: this repository's URL. Untick
   **Webhook → Active**.
3. Tick **Enable Device Flow**. Untick **Expire user authorization tokens** (refreshing one needs
   the client secret, which has no place on a device).
4. **Repository permissions → Contents: Read-only** (Metadata: read-only is added automatically).
   Nothing else. GitHub also offers a **private key**: it isn't used (device flow needs none);
   delete it or never download it, and never commit one.
5. **Where can this GitHub App be installed? → Only on this account.** Create it.
6. On the App's page, **Install App** → your account → **Only select repositories** → pick the
   plugin repositories (only those).
7. Copy the **Client ID** (`Iv23...`; public, not a secret) into `DEFAULT_CLIENT_ID` in
   `core/github.py` (already set for the project's own App), or for one device only set `"github.client_id"` in
   `~/.config/canopener/settings.json` (or `CANOPENER_GITHUB_CLIENT_ID`).

The token (`~/.config/canopener/github-token.json`, mode 600) can only read the repositories the
App is installed on. It reaches git through the environment (`GIT_CONFIG_*`), never the command
line or the checkout's `.git/config`. **System → Software → Plugins → Sign out** forgets it on the device;
github.com → Settings → Applications → Authorized GitHub Apps revokes it everywhere. Don't name
private repositories in this public repo: the list lives in the `plugins.sources` setting.

## Project layout

```
canopener/
  j1939/     protocol library: IDs, DTCs/DMx, transport protocol, signals, address names
  obd/       OBD-II (J1979) protocol: ISO-TP, PIDs, DTCs, readiness, Mode 06, passive decoder
  uds/       UDS (ISO 14229) protocol: services, NRCs, DTC status, request builders
  canopen/   CANopen (CiA 301) protocol: COB-IDs, NMT, EMCY, SDO, standard objects, passive sniffer
  can/       bus backends (SocketCAN, python-can (optional), virtual, log replay, ELM327 Bluetooth OBD
             adapters + their probe) and candump log files
  core/      session engine, scans, live-data model, OBD/UDS clients, transmit, reports, profiles,
             units, link status + signal finder, sound cues, self-update; for the handheld:
             meters, installed options (hardware.py), SYNC clock (sync.py), logic Pico (logic.py)
  sim/       simulated machine (J1939), OBD-II/UDS car, CANopen battery truck, ELM327 adapter
  gui/       desktop GUI (Qt) - a thin front-end over core/
  device/    handheld UI (Pygame) - another front-end over core/ (docs/DEVICE.md)
  profiles/  built-in machine profiles (generic, simulator, example machines)
  cli.py     command-line front-end
  plugins.py plugin loading and hooks; frontends.py switches between the two graphical UIs
deploy/      Linux installer + USB adapter plug-in rules; Raspberry Pi launcher, service, sudoers,
             config.txt lines for the controls and for each hardware stage; the capture upload
             server (upload-server/: one standard-library file, Docker, nginx)
firmware/    the handheld's coprocessors: meter Pico (MicroPython), logic Pico (stock sigrok-pico),
             ESP32 bridge (Arduino). Not part of the Python package
hardware/    the custom board (KiCad, to come) and its groundwork notes
tests/       pytest suite, including headless GUI tests
docs/        user guides (FEATURES, INSTALL, USAGE, install/), the handheld (DEVICE, DESIGN,
             ELECTRONICS) and the software (DEVELOPING, ARCHITECTURE); index in docs/README.md
scripts/     readme_screenshots.py + device_screenshots.py (retake docs/images), db9_cables_diagram.py
             (the DB9 cable diagram), sync-from-main.sh (Pi branches)
```

## Files the app writes

Listed in [USAGE.md](USAGE.md#where-things-are-saved).
