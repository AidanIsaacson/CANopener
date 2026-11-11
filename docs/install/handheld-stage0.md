# Handheld, first steps: Pi 5, screen, CANable (stages 0 and 0b)

The first two steps of the handheld build ([ELECTRONICS.md](../ELECTRONICS.md), "Stages"). Nothing
to solder:

* **Stage 0:** a Raspberry Pi 5, the Touch Display 2 and the same USB CANable 2.0 as on the
  laptop. The result is a working touch-screen scan tool.
* **Stage 0b:** add a few buttons and the rotary encoder on a breadboard.

Everything after that (the CAN chips on adapter boards, the Picos, the probes) builds on this Pi.

> **Status:** written from the docs and the installer, **not yet done on a real Pi 5 and panel**.
> Steps marked [verify] are the likely spots to differ; note what you actually saw.

## What you need

| Item | Notes |
|---|---|
| Raspberry Pi 5 | Any RAM size |
| Power supply: official 27 W USB-C (5 V / 5 A) | With a 3 A supply the Pi limits USB to 600 mA in total. That's still enough for the CANable, but you'll get a warning |
| Cooler (Active Cooler or a case fan) | The Pi 5 throttles without one |
| microSD card, 32 GB or more | Endurance-rated (SanDisk / Samsung Pro Endurance): logs write a lot |
| Touch Display 2 (10.1") and the cables in its box | The display cable and a power lead |
| CANable 2.0 and your CAN cable | The same ones as on the laptop |
| For stage 0b: breadboard, female-to-male jumper wires, 2-5 push buttons (the B3F-4000s), a rotary encoder | See [stage 0b](#stage-0b-buttons-and-the-encoder) |

## Stage 0

### 1. Connect the display (power off)

1. **Display cable:** use the one that fits the Pi 5. Its Pi end is the narrow 22-pin plug. It goes
   into either of the Pi 5's two small connectors marked CAM/DISP. Lift the connector's latch, slide
   the cable in fully with the contacts facing the way the display's leaflet shows, and press the
   latch down.
2. **Power lead:** from the display to the Pi's 40-pin header, **5 V to pin 2 (or 4) and GND to
   pin 6**. [verify against the leaflet: the colours and the plug on the display end.] These are
   shared power pins; nothing in the handheld's pin plan needs them to itself.

Pin numbers in this guide are the **physical pin** (1-40, counted on the header; pin 1 is the one
with the square pad, nearest the SD card end), with the GPIO number beside it. They aren't the
same thing: GPIO5 is pin 29.

### 2. Flash and set up the Pi

Follow [Install on a Raspberry Pi](raspberry-pi.md) steps 1 and 2 (Raspberry Pi OS 64-bit, desktop
version). The screen should work on first boot with nothing to configure. If it stays black, look
in the display's documentation for a `dtoverlay=` line [verify].

Then run the installer with the handheld options:

```bash
cd ~/CANopener
./deploy/raspberry-pi/install.sh --sudoers --autostart --device
```

`--device` lets the app read the encoder (stage 0b) and set the brightness. Log out and back in
(or reboot) for it to apply.

### 3. Screen orientation and size

The 10.1" panel is portrait (1200 x 1920) as it comes, and the handheld is laid out landscape.

1. On the Pi desktop: **Preferences → Screen Configuration**, right-click the DSI screen, set the
   orientation, then **Apply**. Check that touch still lands where you tap [verify: touch should
   turn with the screen].
2. In CANopener: **System → Display → Screen size → 10.1"**. That sizes buttons to about 10 mm for gloves.
   Leave **Rotate picture** at 0°: it's only for running with no desktop.

### 4. The CANable

Plug it in. Either firmware works (see the laptop guide's
[Your USB adapter](linux-laptop.md#your-usb-adapter-canable-20--20-pro)), but **candleLight is
better here**: slcan firmware can't do listen-only, and **Auto**, **Find signal** and placing
probes on a live machine need listen-only. If yours has slcan, it's easiest to flash candleLight
from the laptop (in Chrome or Edge) before moving it to the Pi.

```bash
~/canopener-venv/bin/canopener-cli interfaces       # should list can0 (or say why not)
```

At login the launcher brings `can0` up at 250k; change it on the handheld under **CAN → Connect**.

### 5. Tell the app what's fitted

```bash
~/canopener-venv/bin/canopener-cli hardware --stage 0 -v
```

`usb_can` should read **ok** with the CANable plugged in. This list grows with each stage (System →
Installed options shows the same).

### 6. Check it: stage 0 is done when

- [ ] The Pi boots straight into the handheld UI, full screen.
- [ ] Touch works everywhere, including the soft keys along the edge.
- [ ] **System → Simulators → Show simulators**, then **CAN → Connect → Simulator**: codes and live data appear.
- [ ] On a bench bus or a real machine: **CAN → Connect** at the right bitrate (or **Auto** with
      candleLight) shows traffic, and a scan reads codes.
- [ ] A data log records, saves, and copies to a USB stick (**System → Storage → Copy all logs**).

**On a machine, run the Pi from a USB-C power bank, not a mains supply** (ELECTRONICS.md section
10: battery only when connected to a machine). Most power banks give 3 A, which is fine here. Note
anything that misbehaves: it becomes a requirement for the real build.

## Stage 0b: buttons and the encoder

Each button goes **between a GPIO pin and a GND pin**. No resistors: the Pi's own pull-ups are
switched on by the config lines. Do the wiring with the Pi **powered off**.

### Which pins

Start with whatever you have. Any subset works, and touch covers the rest. Suggested first set:

| Control | GPIO | Physical pin | Does |
|---|---|---|---|
| Button 1 | 5 | 29 | Soft key 1 |
| Button 5 | 16 | 36 | Back |
| Encoder A | 24 | 18 | Move (turn) |
| Encoder B | 4 | 7 | Move (turn) |
| Encoder push | 26 | 37 | Select (hold: Back) |
| GND | | 30, 34, 39 (also 6, 9, 14, 20, 25) | One side of every switch |

The full set (buttons 2-4 on GPIO6/12/13, the d-pad on GPIO17/27/22/23) is in
[DEVICE.md](../DEVICE.md#controls-wiring). Those pins are chosen to stay clear of what the later
stages need, so use these and not others.

**Encoder:** the RKJXT1F42001 in the order is the final part, but its pinout isn't confirmed yet
(ELECTRONICS.md 5.8), so start with any plain mechanical encoder (EC11 type): its two outer pins
are A and B, the middle one is common (to GND), and the push switch is a separate pair. A
**KY-040-style module** works too, but connect its **+ pin to 3.3 V (pin 1 or 17), never 5 V**: it
has pull-ups to +, and 5 V on a GPIO pin damages the Pi.

Rules that protect the Pi:

* GPIO pins are 3.3 V only. Nothing at 5 V or above ever goes to one.
* Double-check physical pin against GPIO number before powering up.
* Wiring a pin straight to GND through a switch is safe (that's what a press does). Wiring two
  GPIO pins together, or a GPIO to 3.3 V or 5 V, is not.

### Turn it on

```bash
cat ~/CANopener/deploy/raspberry-pi/config-controls.txt | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

That adds all twelve controls. The unwired ones are harmless: they sit pulled up and never press.

### Check it: stage 0b is done when

- [ ] `~/canopener-venv/bin/canopener-cli hardware --stage 0b -v`: `controls` reads **ok** and
      counts the buttons and the encoder [verify: the input device names it looks for].
- [ ] Each wired button does its job in the UI (**System → Controls → Key map** lists what each key does).
- [ ] One encoder click moves the focus one row. If it moves two, change `steps-per-period=1` to
      `2` (or `4`) on the `rotary-encoder` line in `/boot/firmware/config.txt` and reboot.
- [ ] Holding the encoder push goes Back.

If a button does nothing: `sudo apt install evtest && sudo evtest` shows every key event. No
event at all means wiring or the config line; an event the app ignores means the key map.

## Next

Stages 1-3 wire up the modules: the MCP2518FD CAN module, the two Picos and the ADS1115 with its
probe inputs, all on breadboard power rails: [Handheld prototype wiring](handheld-prototype.md).
The CANable stays plugged in as the CAN module's bench partner on the same Pi.
