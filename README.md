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

## What it does

| | |
|---|---|
| **Fault codes** | Active (DM1), stored (DM2), pending (DM6), emissions (DM12/DM23), freeze frames (DM4), readiness (DM5). Warning-lamp status per ECU. Optional "every code seen this session" view that catches intermittent faults. |
| **Live data** | ~35 standard engine/chassis parameters (rpm, temps, pressures, voltages, DPF/DEF ...), min/max, a rolling multi-lane chart, metric or imperial. Jump straight from a fault code to its live parameter. |
| **Bus monitor** | Raw frames, scrolling or one row per ID (like `cansniffer`) with changed bytes highlighted, for reverse-engineering proprietary frames. |
| **Network** | Every ECU on the bus, its address-claim NAME, and each PGN it sends with rate and latest data. |
| **Machine info** | Component / software / ECU IDs, VIN, engine hours, fuel used. |
| **Log tool** | Plan timestamps for a job ("key on, start, drive, shutdown ..."), record, and tag them with single keys while you work the machine. After stopping, relabel and save. Each capture gets a raw log, a live-data spreadsheet with a marker column, and a summary of codes and ECU dropouts against your timestamps. |
| **Reports and logs** | Text reports, `candump`-format recording, CSV export, copy everything to a USB stick, offline decode of a log, and real-time replay of a log. |
| **Machine profiles** | Per make/model JSON files that name OEM source addresses, SPNs, proprietary PGNs, and add custom signals. |

**Read-only by design.** The tool only transmits J1939 Requests (PGN 59904) and transport-protocol
flow control (CTS / end-of-message ACK). It never sends DM3/DM11 clears or writes anything to an
ECU. Tick **Listen-only** to transmit nothing at all (DM1 and live data still work, they're broadcast).

## Quick start (no hardware needed)

```bash
git clone https://github.com/AidanIsaacson/CANopener.git
cd CANopener
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[gui,dev]"

canopener --sim            # desktop GUI connected to the simulated telehandler
canopener-cli scan --sim --full
pytest                     # run the tests

canopener --install-desktop --desktop-shortcut   # add CANopener (with icon) to the menu + desktop
```

After `--install-desktop`, CANopener shows up in the application menu like any other program.
It launches with this same Python environment, so there's no venv to activate. Run it again if
you move the folder; `--uninstall-desktop` removes it.

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
3. Click **Connect**, then **Read Active** / **Read Stored** / **Full Scan**.

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

* **Ubuntu / Mint / Debian desktop:** `sudo apt install python3-venv can-utils`, then the quick
  start above.
* **Raspberry Pi OS (64-bit):** `./deploy/raspberry-pi/install.sh --sudoers --autostart` sets
  up a venv, a launcher, passwordless CAN setup and start-at-login. See
  [`deploy/raspberry-pi/README.md`](deploy/raspberry-pi/README.md). The GUI works with PySide6
  or the distro's PyQt6 (`sudo apt install python3-pyqt6`). The core and CLI have **no
  dependencies** beyond Python 3.10+.
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
| **Space** | add a timestamp to the plan | tag the next untagged timestamp (adds a generic one once the plan runs out) |
| **1** … **9**, **0** | | tag timestamp 1 … 10 directly (pressed twice: adds a "(2)" entry, never overwrites) |
| **Backspace** | | undo the last tag |

Remap any of them under **Log → Keyboard shortcuts…** (or the panel's **Keys…** button). Held
keys don't repeat, and Start/Stop ignores a second press within 0.8 s.

1. Name the job, write the customer concern, and list the timestamps, or load a template like
   *Intermittent shutdown*. Save your own lists with **Save as…**.
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
(516096-524287) and proprietary PGNs. Pick one from the **Profile** box in the toolbar.
Built-in examples are in [`canopener/profiles/`](canopener/profiles). Copy one into
`~/.config/canopener/profiles/` and edit it. The format is documented at the top of
[`canopener/core/profiles.py`](canopener/core/profiles.py).

## Project layout

```
canopener/
  j1939/     protocol library: IDs, DTCs/DMx, transport protocol, signals, address names
  can/       bus backends (SocketCAN, virtual, log replay) and candump log files
  core/      session engine, scans, reports, profiles, units, interface setup
  sim/       simulated machine
  gui/       desktop GUI (Qt) - a thin front-end over core/
  cli.py     command-line front-end
deploy/      Raspberry Pi launcher, systemd service, sudoers rule, installer
legacy/      the original j1939_codes.py script and the v0.1 Pi app (frozen, for reference)
tests/       pytest suite, including headless GUI tests
docs/        DESIGN.md (hardware + concept), ARCHITECTURE.md (software)
```

## License

To be decided by the project owner before the first release.
