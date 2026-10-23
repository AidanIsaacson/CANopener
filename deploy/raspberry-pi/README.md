# Raspberry Pi deployment

Reference for the files in this folder. **For step-by-step setup from a blank SD card, follow
[docs/install/raspberry-pi.md](../../docs/install/raspberry-pi.md).**

Target setup: Raspberry Pi OS (64-bit, Bookworm or later), a SocketCAN adapter or an
MCP2515/MCP2518FD HAT showing up as `can0`, and a touchscreen. (Not yet verified on Pi hardware.)

```bash
git clone https://github.com/AidanIsaacson/CANopener.git ~/CANopener
cd ~/CANopener
./deploy/raspberry-pi/install.sh --sudoers --autostart
```

| File | Purpose |
|---|---|
| `install.sh` | Runs [`../linux/adapter-setup.sh`](../linux) (USB CAN drivers, CANable plug-in rule), creates `~/canopener-venv`, installs the app, adds the launcher and a menu entry. `--sudoers` and `--autostart` are optional. |
| `canopener-launch.sh` | Runs `canopener-cli setup` for the CAN interface, then the GUI fullscreen. Configured with `CANOPENER_*` environment variables (see the top of the file). |
| `canopener.sudoers` | Lets your user run **only** `ip link set canN up/down/type can ...` without a password. Without it, interface setup asks for a password (pkexec), which a kiosk can't answer. |
| `canopener.service` | systemd **user** service: start at login, restart on crash. |
| `canopener.desktop` | Application-menu entry. |

## SPI-HAT kernel overlay

For an MCP2515 HAT add to `/boot/firmware/config.txt` (oscillator/interrupt depend on the board):

```
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
```

For the planned MCP2518FD board: `dtoverlay=mcp251xfd,spi0-0,interrupt=25`.

## Running without a desktop

The PySide6 wheels include Qt's `eglfs` and `linuxfb` back-ends, so the app can run
straight on the display with no desktop session. Set in the service or environment:

* DSI/HDMI screen: `QT_QPA_PLATFORM=eglfs`
* SPI TFT on a framebuffer: `QT_QPA_PLATFORM=linuxfb:fb=/dev/fb1`

(This is the stepping stone toward the boot-to-app instrument in [docs/DESIGN.md](../../docs/DESIGN.md).)

## Small screens

`CANOPENER_ARGS="--fullscreen --scale 1.3"` enlarges everything for touch. The current GUI
targets 1024x600 and up; 800x480 works but is cramped. The planned on-device UI is designed
for small screens from the start (see docs/ARCHITECTURE.md).

## Hardware buttons for the Log tool

The Log tool's keys are remappable (Log → Keyboard shortcuts), and a physical button can send
any key using the kernel's `gpio-key` overlay. For example, Start/Stop on GPIO17 and Timestamp
on GPIO27, wired between the pin and ground, sending Enter and Space:

```
# /boot/firmware/config.txt
dtoverlay=gpio-key,gpio=17,active_low=1,gpio_pull=up,keycode=28    # KEY_ENTER
dtoverlay=gpio-key,gpio=27,active_low=1,gpio_pull=up,keycode=57    # KEY_SPACE
```

(Key codes are from `linux/input-event-codes.h`: F5 = 63, F6 = 64, 1 = 2 ... 9 = 10, 0 = 11.)

## Buzzer for sound cues

The signal finder chimes when it locks on, and a falling / rising tone marks traffic dropping
out / coming back. Besides HDMI or headphone audio, a piezo buzzer works; pick it under
**Settings → Sound cues** (or `canopener-cli find --buzzer SPEC`). Two ways to drive it:

* **PWM (passive piezo, plays the actual notes).** Enable a hardware PWM channel, wire the piezo
  (through a transistor for anything loud) to that pin, and use `pwm:0:0` as the buzzer:

  ```
  # /boot/firmware/config.txt
  dtoverlay=pwm,pin=18,func=2        # GPIO18 = PWM0 channel 0 -> /sys/class/pwm/pwmchip0/pwm0
  ```

  On Raspberry Pi OS the `gpio` group can write the PWM files (the default user is in it). The
  chip number differs between models (a Pi 5's RP1 PWM is not `pwmchip0`): check
  `ls /sys/class/pwm` and use the **Test** button in Sound cues.
* **Kernel beeper device.** A `pwm-beeper` (or `gpio-beeper`) node in a device-tree overlay
  appears as `/dev/input/eventN` and is found automatically (**Auto-detect**). The user needs to
  be in the `input` group (`sudo usermod -aG input $USER`, then log in again).

Each cue also has its own rhythm, so they stay distinct on a buzzer that can only beep.
