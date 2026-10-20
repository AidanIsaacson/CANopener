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
| **Reports and logs** | Text reports, `candump`-format recording, offline decode of a log, and real-time replay of a log. |
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
3. Click **Connect**, then **Read Active** / **Read Stored** / **Full Scan**.

From a terminal:

```bash
sudo ip link set can0 type can bitrate 250000 restart-ms 100 && sudo ip link set can0 up
canopener-cli scan --iface can0 --full --units imperial
canopener-cli decode ~/canopener/logs/capture_20260101_120000.log
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
* **Raspberry Pi OS (64-bit):** either `pip install -e ".[gui]"` in a venv, or use the distro's
  Qt with `sudo apt install python3-pyqt6` and `pip install -e .` (the GUI works with PySide6 or
  PyQt6). The core and CLI have **no dependencies** beyond Python 3.10+.
* Small touch screens: `canopener --fullscreen --scale 1.5`.

Files the app writes:

| Path | What |
|---|---|
| `~/canopener/logs/` | Recordings (`candump -l` format, readable by can-utils, SavvyCAN, etc.) |
| `~/canopener/reports/` | Saved reports (plus the raw log alongside) |
| `~/.config/canopener/profiles/*.json` | Your machine profiles |
| `~/.config/canopener/spn_names.csv` | Optional extra SPN names, `spn,name` per line |

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
legacy/      the original single-file j1939_codes.py field script (frozen, for reference)
tests/       pytest suite, including headless GUI tests
docs/        DESIGN.md (hardware + concept), ARCHITECTURE.md (software)
```

## License

To be decided by the project owner before the first release.
