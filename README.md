# CANopener

Open-source tool for diagnosing equipment and vehicles. Developed on Linux, for deployment on a
Raspberry Pi with matching hardware.

* **[docs/DESIGN.md](docs/DESIGN.md)**: the CAN Opener field-diagnostic cyberdeck: concept,
  hardware, enclosure, and the on-device software plan.
* **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**: how this software is structured and how it
  becomes one tool on that device.

This repository currently holds the **J1939 diagnostics tool**: a reusable protocol and diagnostics
engine, a desktop GUI, a command-line tool, and a built-in machine simulator for working without
hardware.

## Install

| | |
|---|---|
| 💻 **[Linux laptop (Mint / Ubuntu / Debian)](docs/install/linux-laptop.md)** | ~10 min. Ends with CANopener in your app menu and on the desktop. Try it with the built-in simulator, no CAN adapter needed. |
| 🍓 **[Raspberry Pi](docs/install/raspberry-pi.md)** | From a blank SD card to CANopener starting full-screen at power-on, with a USB adapter or a CAN HAT. |

Short version for a Linux machine that already has `git` and `python3-venv`:

```bash
git clone https://github.com/AidanIsaacson/CANopener.git ~/CANopener && cd ~/CANopener
python3 -m venv .venv && .venv/bin/pip install -e ".[gui]"
.venv/bin/canopener --install-desktop --desktop-shortcut     # then start it from the menu
```

## What it does

| | |
|---|---|
| **Fault codes** | Active (DM1), stored (DM2), pending (DM6), emissions (DM12/DM23), freeze frames (DM4), readiness (DM5). Warning-lamp status per ECU. Optional "every code seen this session" view that catches intermittent faults. |
| **Live data** | ~35 standard engine/chassis parameters (rpm, temps, pressures, voltages, DPF/DEF ...), min/max, a rolling multi-lane chart, metric or imperial. Jump straight from a fault code to its live parameter. |
| **Bus monitor** | Raw frames, scrolling or one row per ID (like `cansniffer`) with changed bytes highlighted, for reverse-engineering proprietary frames. |
| **Network** | Every ECU on the bus, its address-claim NAME, and each PGN it sends with rate and latest data. |
| **Machine info** | Component / software / ECU IDs, VIN, engine hours, fuel used. |
| **OBD-II (cars)** | Automotive J1979 over CAN: ~40 standard live PIDs, stored/pending/permanent trouble codes with generic (SAE) descriptions, and Mode 09 vehicle info (VIN, calibration ID, CVN, ECU name). A built-in OBD-II simulator to try it with no car. |
| **UDS modules** | Read ABS, transmission, body and cluster modules over UDS (ISO 14229) — DTCs with their standard status flags, and data identifiers — which generic OBD-II can't reach (Diagnostics → Read a module). |
| **Log tool** | Plan timestamps for a job ("key on, start, drive, shutdown ..."), record, and tag them with single keys while you work the machine. After stopping, relabel and save. Each capture gets a raw log, a live-data spreadsheet with a marker column, and a summary of codes and ECU dropouts against your timestamps. |
| **Reports and logs** | Text reports, `candump`-format recording, CSV export, copy everything to a USB stick, offline decode of a log, and real-time replay of a log. |
| **Machine profiles** | Per make/model JSON files that name OEM source addresses, SPNs and proprietary PGNs, and add custom signals: bit fields, signed values, enum text, groups and normal ranges. The app recognises a machine from the modules on its bus and offers the matching profile. A profile can also be run as a simulator. |
| **Units** | Metric, imperial, or your own mix per quantity (Settings → Units…, e.g. psi with °C). |
| **Plugins** | Separately installed packages can add tabs, menus, CLI commands and profiles (see below). |

**Read-first, with a separate confirm-first transmit path.** Diagnostics only transmit standard
requests (J1939 Requests / OBD-II / UDS reads) and transport-protocol flow control. Actually
*sending* onto the bus is deliberately walled off in **Diagnostics → Transmit a frame**: it's off
by default, disabled in listen-only, shows the exact CAN ID, bytes and destination before every
send, and logs what it sent. Only generic, publicly documented frames ship in the app (a J1939
Request, a raw frame, a UDS request you compose); OEM/active commands come from plugins behind the
same confirmation. Tick **Listen-only** to transmit nothing at all (DM1 and live data still work,
they're broadcast).

## Developing (no hardware needed)

```bash
git clone https://github.com/AidanIsaacson/CANopener.git
cd CANopener
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[gui,dev]"

canopener --sim            # desktop GUI connected to the simulated telehandler
canopener-cli scan --sim --full
pytest                     # run the tests
ruff check .               # lint
```

The simulator is a Deutz-style engine ECU (SA 00) and a machine controller (SA 27). It warms up,
revs, throws an intermittent low-oil-pressure code at idle, and answers requests exactly like
a real ECU (single frame, BAM, and RTS/CTS transport).

## On a real machine

Any adapter that shows up as a SocketCAN interface works: candleLight / gs_usb (CANable etc.),
PEAK, 8devices USB2CAN, Kvaser, MCP2515/MCP2518FD SPI HATs (`can0`), or `slcan` adapters after
`slcand`.

1. Plug in the adapter and connect to the diagnostic port (J1939 is usually 250 kbit/s; some newer
   machines use 500 kbit/s).
2. In the GUI pick **SocketCAN adapter**, choose the interface and bitrate, and click
   **Set Up Interface**. It asks for your password and runs
   `ip link set can0 type can bitrate 250000 ... && ip link set can0 up`.
   Pick **Auto** to detect the bitrate. It listens at each rate in listen-only mode, so nothing
   is transmitted onto the machine's bus while it searches.
3. Click **Connect**, then **Read Active** / **Read Stored** / **Full Scan**. **Diagnostics →
   Request a PGN…** asks one ECU (or all) for any parameter group and shows the reply.

From a terminal:

```bash
canopener-cli setup --iface can0 --bitrate auto     # or --bitrate 250000
canopener-cli scan --iface can0 --full --units imperial
canopener-cli decode ~/canopener/logs/capture_20260101_120000.log --csv frames.csv
```

### Testing against a virtual bus

```bash
sudo modprobe vcan && sudo ip link add dev vcan0 type vcan && sudo ip link set vcan0 up
canopener-cli sim --iface vcan0      # terminal 1: the simulated machine
canopener --iface vcan0              # terminal 2: the GUI, over real SocketCAN
candump vcan0                        # terminal 3: watch the traffic (can-utils)
```

## Install notes

* Full guides: [Linux laptop](docs/install/linux-laptop.md), [Raspberry Pi](docs/install/raspberry-pi.md).
* The GUI works with PySide6 (installed by `pip install -e ".[gui]"`) or the distro's PyQt6
  (`sudo apt install python3-pyqt6`). The core and CLI have **no dependencies** beyond Python 3.10+.
* Small touch screens: `canopener --fullscreen --scale 1.5`.

Files the app writes:

| Path | What |
|---|---|
| `~/canopener/logs/` | Captures from the Log tool, one folder each (see above) |
| `~/canopener/reports/` | Saved reports (plus the raw log alongside) |
| `~/.config/canopener/profiles/*.json` | Your machine profiles |
| `~/.config/canopener/spn_names.csv` | Optional extra SPN names, `spn,name` per line |

## Log tool: recording a job

Open it with the **📝 Log** button (or F2). Its keys only work while it's open, and never while
you're typing in a box (press **Esc** to leave a box):

| Key (default) | Not recording | Recording |
|---|---|---|
| **Enter** | start | stop |
| **Space** | add a timestamp to the plan and type its name | tag the next untagged timestamp (adds a generic one once the plan runs out) |
| **Shift+Space** | add a timestamp and type its name. **Also works while you're typing a name**: it keeps the name and opens the next one | add a timestamp at the end of the list |
| **1** … **9**, **0** | | tag timestamp 1 … 10 directly (pressed twice: adds a "(2)" entry, never overwrites) |
| **Backspace** | | undo the last tag |

Remap any of them under **Log → Keyboard shortcuts…** (or the panel's **Keys…** button). Held
keys don't repeat, and Start/Stop ignores a second press within 0.8 s.

1. Name the job, write the customer concern, and list the timestamps, or load a template like
   *Intermittent shutdown*. To type a list from the keyboard: **Shift+Space**, type `Key On`,
   **Shift+Space**, type `Start`, … then **Enter** when done (**Esc** cancels the one you're
   naming). Save your own lists with **Save as…**.
2. **Start**, then tag as you go. The Live Data chart shows each tag as a labelled line.
3. **Stop**. Double-click to rename, add notes, delete or reorder, then **Save** / **Save As…**
   (or **Discard**). If you just press Start again, the last capture is saved for you first.

Each capture is a folder in `~/canopener/logs/` (change it with **Change…** in the panel):

| File | Contents |
|---|---|
| `summary.txt` | Notes, timestamps, and one timeline of markers, codes going active / clearing, lamp changes and ECUs going silent / coming back, each with the phase it happened in. **The file to send back for debugging.** |
| `live_data.csv` | Every live parameter twice a second, with `event` and `phase` columns. Opens in a spreadsheet. |
| `markers.csv` | The timestamps with elapsed times. |
| `capture.log` | Every raw frame (candump format; reopen with **Open…** → **View data**). |
| `capture.json` | Everything above in machine-readable form. |

Captures survive a crash: the next start offers to recover anything unsaved.
`canopener-cli capture <folder>` regenerates and prints a capture's summary.

**Export Logs** (in the panel, or Ctrl+E): tick what to export (this session's captures and
reports are pre-ticked), insert a USB stick (it's picked up automatically), press **Export**. It
copies everything into `CANopener_export_<date>/` on the stick, then ejects it. Or pick
**Folder** to export anywhere else.

## Machine profiles

Profiles label what the standard tables can't: OEM controllers, manufacturer-specific SPNs
(516096-524287) and proprietary PGNs, plus extra live signals. Pick one from the **Profile** box
in the toolbar. Built-in examples are in [`canopener/profiles/`](canopener/profiles); copy one
into `~/.config/canopener/profiles/` and edit it. The full format is documented at the top of
[`canopener/core/profiles.py`](canopener/core/profiles.py). Signals can be:

* whole bytes (`"length": 1/2/4`) or bit fields (`"bit": 4, "bits": 12`, spanning bytes in J1939 order)
* `"signed"`, scaled (`"scale"`, `"offset"`), with a `"unit"` (common spellings like `Volts`, `C`,
  `kph` are normalised so unit conversion works)
* enumerated (`"enum": {"0": "Released", "1": "Applied"}`), grouped (`"group"`, used by the
  Live Data group filter), with a normal range (`"low"`, `"high"`: out-of-range values show red)
* limited to one source address or a list (`"sa": ["0x00", "0x27"]`)

A profile signal at the same place as a built-in one replaces it, so a profile can rename and
regroup standard data too.

**Recognising a machine:** a `"detect": {"addresses": [...], "min_match": 2}` section lists the
module addresses that machine has. When the bus matches, a banner offers to switch profiles.

**Simulating a profile:** pick the profile, choose **Simulate the selected profile** as the
source, and Connect. Every signal is broadcast from its address with moving values, so you can
check a new profile's decoding and layout without the machine (`canopener-cli --profile ID sim
--iface vcan0` does the same on a SocketCAN bus).

**Keep OEM data private.** Profiles generated from dealer-tool data belong in
`~/.config/canopener/profiles/` or a private plugin, never in this public repository (a test
fails if such files appear here).

## Plugins

Separately installed Python packages can extend CANopener without changing this repository: a
package declares an entry point in the `canopener.plugins` group and can then add tabs and menu
actions, `canopener-cli` commands, and folders of profiles. See
[`canopener/plugins.py`](canopener/plugins.py) for the hooks. A plugin that transmits anything
beyond J1939 requests must confirm with the user first, and `Session.send_frame` refuses in
listen-only mode.

## Project layout

```
canopener/
  j1939/     protocol library: IDs, DTCs/DMx, transport protocol, signals, address names
  obd/       OBD-II (J1979) protocol: ISO-TP, Mode 01 PIDs, DTC + VIN/Mode 09 decode
  uds/       UDS (ISO 14229) protocol: services, NRCs, DTC status, request builders
  can/       bus backends (SocketCAN, virtual, log replay) and candump log files
  core/      session engine, scans, OBD/UDS clients, transmit, reports, profiles, units
  sim/       simulated machine (J1939) and OBD-II/UDS simulator
  gui/       desktop GUI (Qt) - a thin front-end over core/
  cli.py     command-line front-end
deploy/      Raspberry Pi launcher, systemd service, sudoers rule, installer
legacy/      the original j1939_codes.py script and the v0.1 Pi app (frozen, for reference)
tests/       pytest suite, including headless GUI tests
docs/        DESIGN.md (hardware + concept), ARCHITECTURE.md (software), install/ (guides)
```

## License

To be decided by the project owner before the first release.
