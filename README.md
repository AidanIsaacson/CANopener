# CANopener

A free, Open Source diagnostic tool for **heavy equipment, trucks, and cars**. Plug a USB CAN adapter into a Linux
laptop, connect to the machine; read fault codes, watch live data, and
record what happens while you work the machine.

**For now, just this program.** With active development for a Raspberry Pi-based handheld, packed with features to make it an all-in-one electrical and electronic diagnostic platform.

* **Features Techs actually want:** Error codes take you to the indicated sensor, a signal finder that chimes when you pick up or lose CAN
  signal, a Log tool that records *everything* with timestamps you tag from the keyboard, with playback, and one-click
  export to a USB stick.
  Easy setup, clear indicators, configurable layout and controls.
* **Not ones we don't:**  NEVER ANY $1000 Dongles, proprietary nonsense, or gatekeeping.

**Supports:**

* **Equipment and trucks (J1939):** forklifts, telehandlers, loaders, generators, on-highway
  trucks. Active and stored codes with warning lamps, ~35 live engine and chassis values, every
  module on the bus.
* **Cars and light trucks (OBD-II):** codes from every module, live sensor data, freeze frame,
  readiness for inspection, and clearing codes, like a good generic scan tool.


## Get it

**Linux laptop** (Mint, Ubuntu, Debian), about 10 minutes. Open a terminal and paste:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/AidanIsaacson/CANopener.git ~/CANopener
~/CANopener/deploy/linux/install.sh
```

You get CANopener in the app menu and on the desktop, plus the drivers for USB CAN adapters.
Step by step, with your adapter: [Linux laptop guide](docs/install/linux-laptop.md).
For a **Raspberry Pi** that starts CANopener full screen at power-on: [Raspberry Pi guide](docs/install/raspberry-pi.md).

**Try it without a machine:** open **🔌 Connection**, pick **Simulator** (a telehandler) or
**OBD-II simulator (car)**, and press **Connect**.

**Updating:** **Help → Check for updates…**, then **Install update** and **Restart now**.
(Tick **Help → Check for updates at startup** to be told when there's one.)

## Hooking up ( ͡° ͜ʖ ͡°)

**Adapters:** any that Linux sees as a CAN interface: CANable / candleLight, PEAK, Kvaser,
8devices, or a CAN HAT on a Pi. The installer sets up the CANable (both firmwares) for you.

1. Plug the adapter into the laptop and connect it to the machine's diagnostic port. Key on.
2. Open **🔌 Connection** (top left, or **F3**). Pick **SocketCAN adapter**, then the bitrate:
   **250k** for most equipment and trucks, **500k** for cars and some newer machines, or **Auto**.
   Press **Connect**.
3. Two lamps at the bottom of the window tell you where you stand:
   * **Adapter:** is the adapter plugged in and on?
   * **Bus:** is it hearing the machine? Green = traffic, amber = connected but silent (key on?
     probes on CAN-H / CAN-L?), red = errors only (wrong bitrate, or the wires swapped).

## Features I'm really proud of

### It tells you when your probes land

Back-probing a harness for CAN-H and CAN-L usually means moving a probe, walking back to the
laptop, squinting at the screen, and walking back again. Here you press **Find signal** in the
Connection panel (or **F4**) and go do the probing. It listens the whole time, without sending a
thing, and **chimes** as soon as it hears steady traffic, usually within 5 seconds of a good probe
position. Then it locks the bitrate in and connects. Once connected, a falling tone tells you the
traffic dropped out (a probe slipped) and a rising one that it's back, so you can keep your head
in the machine. Both chimes can be switched off in the Connection panel. Turn the laptop volume
up, or wire a buzzer to a Pi (**Settings → Sound cues**).

### From a code straight to its sensor

My favourite. When you pull codes and a code points at a specific sensor, **there's a button that
pulls that sensor up in Live Data**, already ticked and charting. No hunting through a list of
fifty values for the right one. On J1939 it's **Show this parameter in Live Data**; on a car
it's **Show in Live Data**, and if the car stored a freeze frame for that code, **Show freeze
frame** takes you to the snapshot from the moment it was set.

### Data logs built for the fault that only happens sometimes

You know the call: *"It runs about ten minutes, shuts down, then you have to wait 30 seconds and
cycle the key twice before it'll start again."* A data log lets you reproduce that and pick it
apart afterwards, and the Log tool is built around exactly that job. Before you start, list the
moments you expect:

1. Key on, start: **slap the spacebar** (timestamp 1)
2. Drive it around
3. It shuts down: **spacebar** (timestamp 2)
4. Wait 30 seconds, key on: **spacebar** (timestamp 3), and so on

Each press tags the next moment on your list, and you can name them, so when more than one thing
happens you can tell them apart. Unplanned moments get a press too. The log records **everything
on the bus for the whole run**. Afterwards, tick just the readings you care about and they're
charted with **your timestamps drawn as lines on the graph**, so you can see exactly what oil
pressure, voltage or engine speed were doing at the moment it quit. The saved capture also has a
plain-text timeline of codes coming and going against those same timestamps, ready to send to
someone. (All the details: [Recording a job](#recording-a-job-the-log-tool).)

### It opens in about a second

Unlike *some other* diagnostic programs. On my laptop it's open in about a second, so it's
quicker to check something than to talk yourself out of checking it.

## Equipment and trucks (J1939)

| Tab | What you get |
|---|---|
| **Fault Codes** | **Read Active** / **Read Stored** / **Pending / Emissions** / **Full Scan**. Each code with its description, occurrence count and the warning lamps. Tick *Include every code seen this session* to catch intermittent ones. **Show this parameter in Live Data** jumps to the sensor behind a code. |
| **Live Data** | Engine speed, temperatures, pressures, voltages, DPF / DEF and more, with min / max. Tick values to chart them. **Hide unticked** and **Hide plots** tidy a small screen. |
| **Bus Monitor** | Every raw frame, or one row per ID with changing bytes highlighted, for figuring out proprietary data. |
| **Network** | Every module on the bus and what it sends. |
| **Machine Info** | Component and software IDs, VIN, engine hours, fuel used. |
| **Report** | A text report of the last scan, to save or copy. |

**Machine profiles** (the **Profile** box, top right) name a specific machine's modules and add
its own parameters. The app recognises a machine and offers its profile. **Profiles → Manage
profiles…** hides ones you don't use and sets the order. Making your own: [docs/DEVELOPING.md](docs/DEVELOPING.md#machine-profiles).

**Modules beyond the engine** (ABS, transmission, body): **Diagnostics → Read a module (UDS)**
reads their codes, and can clear them or reset the module.

## Cars and light trucks (OBD-II)

Pick the **OBD-II vehicle** profile (top right; connecting the OBD-II simulator does it for you).
The screen becomes one row of pages:

| Page | What you get |
|---|---|
| **Trouble Codes** | **Check codes** reads every module (engine, transmission ...): stored, pending and permanent codes, with descriptions, and the check-engine light. **Clear codes** asks first and tells you what else it resets. Pick a sensor code and press **Show in Live Data** to watch that sensor, or **Show freeze frame**. |
| **Live Data** | Every value the car offers, streaming, with min / max and charts. Same controls as the J1939 Live Data. |
| **Freeze Frame** | The values saved when the code was set. |
| **Readiness** | Inspection monitors: complete, incomplete or not supported, and a READY / NOT READY verdict. |
| **On-Board Tests** | Ask the car to run a test, e.g. the EVAP leak test. |
| **Test Results** | Each monitor's last test value against its limits. Spots a part that passes, but only just. |
| **Vehicle Info** | VIN, calibration IDs, module names. |
| **Bus Monitor** | Raw frames, as on the J1939 side. |

Newer cars often have a security gateway: the diagnostic port is silent unless the tool asks
something. The Bus lamp then shows an indigo **"Bus: quiet (gateway?)"**, which is normal.

## Recording a job (the Log tool)

Open it with **📝 Log** (top right, or **F2**). Plan the moments you care about, record, and tag
them with one key while you work the machine. Its keys only work while it's open, and never while
you're typing in a box (**Esc** leaves a box):

| Key | Not recording | Recording |
|---|---|---|
| **Enter** | start | stop |
| **Space** | add a timestamp and type its name | tag the next timestamp |
| **Shift+Space** | add a timestamp and type its name (also while typing one: keeps it and starts the next) | add a timestamp at the end |
| **1** … **9**, **0** | | tag timestamp 1 … 10 directly |
| **Backspace** | | undo the last tag |

Remap them under **Log → Keyboard shortcuts…**.

1. Name the job, write the customer concern, and list the timestamps ("Key on", "Start",
   "Shuts down" ...), or load a template like *Intermittent shutdown*.
2. **Start**, then tag as you go. The Live Data chart shows each tag as a line.
3. **Stop**, fix up names and notes if needed, then **Save**.

On a car the Log tool keeps it talking while it records: it reads the codes once, then streams
the ticked live values.

Each capture is a folder in `~/canopener/logs/`:

| File | What's in it |
|---|---|
| `summary.txt` | Your notes and timestamps, and one timeline of codes appearing and clearing, warning lamps, and modules dropping off the bus, each with the phase it happened in. **The file to send someone.** |
| `live_data.csv` | Every live value twice a second, with your timestamps in a column. Opens in a spreadsheet. |
| `capture.log` | Every raw frame (reopen it with **Open…** in the Log panel, then **View data**). |
| `markers.csv`, `capture.json` | The timestamps, and everything in machine-readable form. |

**How long can a recording be?** As long as the disk allows: frames go straight to the file. A
busy 250k J1939 bus is roughly 500 to 900 frames a second, about 100 to 170 MB an hour. Saving
reads the whole log back to write the summary (about 350 MB of memory per hour at 500 frames a
second), so multi-hour captures are fine on a laptop; split all-day ones. The Bus Monitor and
**File → Export frames as CSV** only keep the latest 250,000 frames (a few minutes on a busy
bus); the capture's log has everything.

Captures survive a crash: the next start offers to recover anything unsaved.

**Export Logs** (in the Log panel, or **Ctrl+E**): plug in a USB stick, press **Export**. It
copies this session's captures and reports onto the stick and ejects it.

## What it sends

* **Reading** sends only standard requests (J1939, OBD-II and UDS reads). Tick **Listen-only** in
  the Connection panel to send nothing at all: J1939 codes (DM1) and live data still work,
  because the machine broadcasts them.
* **Commands that change a vehicle** (clear codes, start an on-board test, reset a module) say
  what they change and wait for a Yes.
* **Diagnostics → Transmit a frame** sends a frame you compose: off by default, it shows the
  exact bytes and asks before every send.

## Command line

Everything works over SSH too (handy on a Pi):

```bash
canopener-cli find --iface can0 --sound             # signal finder: wait for traffic, chime
canopener-cli scan --iface can0 --full --units imperial
canopener-cli obd --iface can0 --all                # car: codes, readiness, freeze frame, live data
canopener-cli obd --iface can0 --clear              # car: clear codes (asks first)
canopener-cli decode capture.log --csv frames.csv   # decode a saved log
canopener-cli update                                # check for and install updates
canopener-cli --help                                # everything else
```

## More

* [docs/DEVELOPING.md](docs/DEVELOPING.md): working on the code, making machine profiles, plugins.
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how the software is built.
* [docs/DESIGN.md](docs/DESIGN.md): the CAN Opener field-diagnostic handheld this is heading for.

## License

GPL-2.0-or-later (see [LICENSE](LICENSE)): you may use, modify and redistribute CANopener, and
anything you distribute that includes it must be offered under the same license, with source.

The generic OBD-II trouble-code descriptions in `canopener/obd/dtc_codes.py` and the Mode 06
unit/scaling table come from [python-OBD](https://github.com/brendan-w/python-OBD) (GPL-2.0-or-later,
© Donour Sizemore, Secons Ltd., Peter J. Creath, Brendan Whitfield).
