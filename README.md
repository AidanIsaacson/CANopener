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

Short version (Mint / Ubuntu / Debian): installs the app, the USB CAN adapter drivers, and a
menu entry and desktop icon:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/AidanIsaacson/CANopener.git ~/CANopener
~/CANopener/deploy/linux/install.sh            # update later with: install.sh --update
```

## What it does

| | |
|---|---|
| **Fault codes** | Active (DM1), stored (DM2), pending (DM6), emissions (DM12/DM23), freeze frames (DM4), readiness (DM5). Warning-lamp status per ECU. Optional "every code seen this session" view that catches intermittent faults. |
| **Live data** | ~35 standard engine/chassis parameters (rpm, temps, pressures, voltages, DPF/DEF ...), min/max, a rolling multi-lane chart, metric or imperial. Jump straight from a fault code to its live parameter. **Hide unticked** lists just the signals you picked, and **Hide plots** gives the list the whole tab on a small screen (both remembered). |
| **Bus monitor** | Raw frames, scrolling or one row per ID (like `cansniffer`) with changed bytes highlighted, for reverse-engineering proprietary frames. |
| **Network** | Every ECU on the bus, its address-claim NAME, and each PGN it sends with rate and latest data. |
| **Machine info** | Component / software / ECU IDs, VIN, engine hours, fuel used. |
| **OBD-II (cars)** | A generic scan tool (SAE J1979 over CAN, 11- or 29-bit, picked automatically). Codes from every module (stored, pending, permanent) with ~2,000 generic SAE descriptions; **Clear codes** (Mode 04); I/M readiness monitors and MIL; every public Mode 01 PID streaming live with min/max (multi-value O2, wide-band, EGT ...); freeze frame; Mode 06 on-board test results with pass/fail; vehicle info (VIN, calibration IDs, CVNs, module names, in-use counters); Mode 08 on-board tests (EVAP leak test). Pick the **OBD-II vehicle** profile. A built-in two-module simulator to try it with no car. |
| **UDS modules** | ABS, transmission, body and cluster modules over UDS (ISO 14229): DTCs with their standard status flags, data identifiers, **clear codes** and **module reset** — which generic OBD-II can't reach (Diagnostics → Read a module). |
| **Log tool** | Plan timestamps for a job ("key on, start, drive, shutdown ..."), record, and tag them with single keys while you work the machine. After stopping, relabel and save. Each capture gets a raw log, a live-data spreadsheet with a marker column, and a summary of codes and ECU dropouts against your timestamps. |
| **Reports and logs** | Text reports, `candump`-format recording, CSV export, copy everything to a USB stick, offline decode of a log, and real-time replay of a log. |
| **Machine profiles** | Per make/model JSON files that name OEM source addresses, SPNs and proprietary PGNs, and add custom signals: bit fields, signed values, enum text, groups and normal ranges. The app recognises a machine from the modules on its bus and offers the matching profile. A profile can also be run as a simulator. |
| **Units** | Metric, imperial, or your own mix per quantity (Settings → Units…, e.g. psi with °C). |
| **Plugins** | Separately installed packages can add tabs, menus, CLI commands and profiles (see below). |

**Reads are free; commands ask first.** Reading sends standard requests (J1939 Requests, OBD-II
and UDS reads) and transport-protocol flow control. The standard commands that change a vehicle,
OBD-II **clear codes** / **on-board tests** and UDS **clear codes** / **module reset**, each say what
they change and wait for a Yes. Raw sending is in **Diagnostics → Transmit a frame**: off by
default, shows the exact CAN ID, bytes and destination before every send, and logs what it sent.
OEM/proprietary commands come from plugins behind the same confirmation. Tick **Listen-only** to
transmit nothing at all (DM1 and live data still work, they're broadcast).

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
PEAK, 8devices USB2CAN, Kvaser, MCP2515/MCP2518FD SPI HATs (`can0`). A CANable with the `slcan`
firmware is attached as `can0` automatically once the installer's plug-in rule is in place
([deploy/linux](deploy/linux)); other `slcan` adapters need `sudo slcand /dev/ttyACM0 can0`.

1. Plug in the adapter and connect to the diagnostic port (J1939 is usually 250 kbit/s; some newer
   machines use 500 kbit/s).
2. Open **🔌 Connection** (top-left, or F3): a sidebar like the Log tool. Pick **SocketCAN
   adapter**, choose the interface and bitrate, and click **Connect**. It brings the interface up first
   (`ip link set can0 type can bitrate 250000 ... && ip link set can0 up`), without a password
   if the installer's sudoers rule is in place, otherwise after one password prompt.
   Pick **Auto** to detect the bitrate. It listens at each rate in listen-only mode, so nothing
   is transmitted onto the machine's bus while it searches.
3. Two lamps at the bottom of the window show the link at a glance: **Adapter** (is the CAN
   adapter plugged into this computer?) and **Bus** (is it hearing the machine: green = traffic,
   amber = connected but silent, red = only error frames / link error). Click either to open
   Connection setup.
4. Then **Read Active** / **Read Stored** / **Full Scan**.

### Finding the wires: signal finder

When you're back-probing a harness and don't know yet which wires are CAN-H / CAN-L, press
**Find signal** in the Connection sidebar (or F4), then go and move the probes. It keeps
listening in listen-only mode (cycling bitrates if **Auto**), never stops on an error or an
unplugged adapter, and as soon as it hears a second of steady traffic it **chimes**, locks that
bitrate in and connects (untick *Connect when found* to just lock). A good probe position is
usually confirmed within 5 seconds. While connected, a falling tone means traffic dropped out
(a probe slipped) and a rising one that it's back.

Cues play on the computer's speaker and/or a piezo buzzer: **Settings → Sound cues**. On a Pi, a
PWM-driven piezo plays the same notes (see [deploy/raspberry-pi](deploy/raspberry-pi/README.md#buzzer-for-sound-cues)).
Searching every bitrate changes the adapter's bitrate about once a second, so it needs the
passwordless CAN access rule (the Linux installer adds it; on a Pi run its installer with
`--sudoers`; otherwise the app offers to). With a fixed
bitrate picked it asks for a password at most once. **Diagnostics →
   Request a PGN…** asks one ECU (or all) for any parameter group and shows the reply.

From a terminal:

```bash
canopener-cli setup --iface can0 --bitrate auto     # or --bitrate 250000
canopener-cli find --iface can0 --sound             # signal finder: wait for traffic, chime, leave it up
canopener-cli scan --iface can0 --full --units imperial
canopener-cli decode ~/canopener/logs/capture_20260101_120000.log --csv frames.csv
canopener-cli obd --iface can0 --all                # OBD-II scan: codes, readiness, freeze frame, Mode 06, live
canopener-cli obd --iface can0 --watch 30           # stream live data for 30 s
canopener-cli obd --iface can0 --clear              # clear codes on every module (asks first)
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
  core/      session engine, scans, live-data model, OBD/UDS clients, transmit, reports, profiles, units,
             link status + signal finder, sound cues (speaker / buzzer)
  sim/       simulated machine (J1939) and OBD-II/UDS simulator
  gui/       desktop GUI (Qt) - a thin front-end over core/
  cli.py     command-line front-end
deploy/      Linux installer + USB adapter plug-in rules; Raspberry Pi launcher, service, sudoers
legacy/      the original j1939_codes.py script and the v0.1 Pi app (frozen, for reference)
tests/       pytest suite, including headless GUI tests
docs/        DESIGN.md (hardware + concept), ARCHITECTURE.md (software), install/ (guides)
```

## License

GPL-2.0-or-later (see [LICENSE](LICENSE)): you may use, modify and redistribute CANopener, and
anything you distribute that includes it must be offered under the same license, with source.

The generic OBD-II trouble-code descriptions in `canopener/obd/dtc_codes.py` and the Mode 06
unit/scaling table come from [python-OBD](https://github.com/brendan-w/python-OBD) (GPL-2.0-or-later,
© Donour Sizemore, Secons Ltd., Peter J. Creath, Brendan Whitfield).
