# Install on a Raspberry Pi

From a blank SD card to CANopener starting by itself at power-on, in about 30 minutes (most of
it waiting for downloads). Works on a Pi 3B+, 4, 5, or CM4.

> **Status:** these steps use the scripts in [`deploy/raspberry-pi/`](../../deploy/raspberry-pi),
> which are tested on desktop Linux but **not yet on real Pi hardware**. If a step doesn't match
> what you see, note where and open an issue.

## 1. Flash the SD card

1. Install **Raspberry Pi Imager** on another computer (raspberrypi.com/software).
2. Choose your Pi model, then **Raspberry Pi OS (64-bit)**, the normal desktop version. The
   64-bit OS is required (the GUI library has no 32-bit build), and a Pi 3B+ runs it fine.
3. When Imager offers **OS customisation**, set:
   * a username and password (remember them),
   * your Wi-Fi network (for the first setup; the Pi doesn't need it in the field),
   * **Enable SSH** on the Services tab, if you want to set it up from your laptop.
4. Write the card, put it in the Pi, connect the screen, and power on. The first boot takes a
   couple of minutes.

## 2. Update and download CANopener

On the Pi, open **Terminal** (or `ssh yourname@<pi-name>.local` from the laptop):

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git
git clone https://github.com/AidanIsaacson/CANopener.git ~/CANopener
```

## 3. Run the installer

```bash
cd ~/CANopener
./deploy/raspberry-pi/install.sh --sudoers --autostart
```

What the options do:

* *(always)*: creates `~/canopener-venv`, installs CANopener into it, and adds a **CANopener**
  entry to the Pi's menu.
* `--sudoers`: lets CANopener bring `can0` up **without a password**. This is limited to exactly
  `ip link set canN up/down/type can …` and nothing else (see
  [`canopener.sudoers`](../../deploy/raspberry-pi/canopener.sudoers)). Needed for a screen-only
  setup with no keyboard to type a password.
* `--autostart`: starts CANopener full-screen every time you log in to the desktop. It also
  brings up `can0` at 250k first.

The installer downloads the GUI library (about 100 MB); on a Pi 3B+ that can take a while.

## 4. Connect the CAN hardware

**USB adapter** (CANable, PCAN-USB, ...): just plug it in; it appears as `can0`.

**SPI CAN HAT** (MCP2515 now, the MCP2518FD board later): tell Linux about it once. Open the
boot config:

```bash
sudo nano /boot/firmware/config.txt
```

add at the bottom (check your HAT's documentation for the crystal frequency and interrupt pin):

```
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
```

(For the MCP2518FD board: `dtoverlay=mcp251xfd,spi0-0,interrupt=25`.) Save with Ctrl+O, Enter,
Ctrl+X, then `sudo reboot`.

Check it worked:

```bash
ip link show can0                                     # should exist
~/canopener-venv/bin/canopener-cli setup --iface can0 --bitrate 250000
candump can0                                          # on a live bus: scrolling frames (Ctrl+C to stop)
```

## 5. Start it

Reboot (`sudo reboot`). With `--autostart`, CANopener opens full-screen after the desktop
logs in. Without it, start it from the menu (**CANopener**) or run `canopener-launch`.

Make sure the Pi logs in to the desktop by itself: `sudo raspi-config` → **System Options** →
**Boot / Auto Login** → **Desktop Autologin** (this is usually the default).

To try it without a machine, pick **Simulator** in the top-left box and press **Connect**.

## Settings for the launcher

Edit `~/.config/systemd/user/canopener.service`, uncomment / add `Environment=` lines, then
`systemctl --user daemon-reload && systemctl --user restart canopener`:

| Variable | Default | Meaning |
|---|---|---|
| `CANOPENER_IFACE` | `can0` | CAN interface |
| `CANOPENER_BITRATE` | `250000` | bitrate, or `auto` to detect (listen-only, safe on a live bus) |
| `CANOPENER_ARGS` | `--fullscreen` | add `--scale 1.3` to make everything bigger on a small touchscreen |
| `QT_QPA_PLATFORM` | *(unset)* | `eglfs` to run with no desktop on a DSI/HDMI screen; `linuxfb:fb=/dev/fb1` for an SPI TFT |

Hardware buttons for the Log tool's Start/Stop and Timestamp keys: see
[deploy/raspberry-pi/README.md](../../deploy/raspberry-pi/README.md#hardware-buttons-for-the-log-tool).

## Updating

```bash
cd ~/CANopener && git pull
~/canopener-venv/bin/pip install -e ".[gui]"
```

then reboot (or `systemctl --user restart canopener`).

## Troubleshooting

| Problem | Fix |
|---|---|
| Doesn't start at login | `systemctl --user status canopener` and `journalctl --user -u canopener` show why. Fallback: `mkdir -p ~/.config/autostart && cp ~/.local/share/applications/canopener.desktop ~/.config/autostart/` |
| Startup log | `~/.local/state/canopener/launch.log` (interface setup + app output) |
| `pip` can't find PySide6 | You're on the 32-bit OS. Reflash with Raspberry Pi OS **(64-bit)**, or use `sudo apt install python3-pyqt6` and `~/canopener-venv/bin/pip install -e .` |
| `can0` doesn't exist with a HAT | Check the overlay line, the HAT's crystal (`8000000` / `16000000` / `20000000` / `40000000`) and interrupt pin, then reboot. `dmesg \| grep -i -E "mcp|can"` shows driver messages. |
| Interface setup asks for a password | Re-run the installer with `--sudoers`. |
| Everything is tiny on the touchscreen | Set `CANOPENER_ARGS=--fullscreen --scale 1.5` (see above). |
