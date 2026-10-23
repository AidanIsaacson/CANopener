# Linux install files

Reference for the files in this folder. **For step-by-step setup, follow
[docs/install/linux-laptop.md](../../docs/install/linux-laptop.md).**

```bash
~/CANopener/deploy/linux/install.sh            # install (safe to re-run)
~/CANopener/deploy/linux/install.sh --update   # git pull, then the same
```

| File | Purpose |
|---|---|
| `install.sh` | Run as your user. System packages (Python, can-utils, Qt's libraries), then `adapter-setup.sh` with sudo, the app in `~/CANopener/.venv`, the passwordless rule (`sudoers_content()` in `canopener/core/interfaces.py`, validated with `visudo`), and the menu entry + desktop icon. `--no-desktop-icon`, `--no-sudoers`. |
| `adapter-setup.sh` | Run as root (both installers call it). Makes sure the kernel has `gs_usb`/`slcan`/`can_raw` (installs `linux-modules-extra-$(uname -r)` on Ubuntu/Mint if not), installs the two files below, reloads udev and systemd. |
| `70-canopener.rules` | udev: a CANable with **slcan** firmware (USB `16d0:117e`, or a product name containing "CANable") is hidden from ModemManager and starts `canopener-slcan@ttyACMn`. The STM32 bootloader (`0483:df11`) gets `uaccess` so firmware can be flashed without root. |
| `canopener-slcan@.service` | Runs `slcand -F /dev/ttyACMn`, attaching the port as the next free `canN`. It doesn't set a bitrate: on Linux 6.0+ the slcan driver accepts `ip link set canN type can bitrate …` like any adapter, so CANopener's normal bring-up (and the existing sudoers rule) covers it. Stops on unplug. |

candleLight-firmware adapters (`1d50:606f`) need none of this: the in-kernel `gs_usb` driver
makes them `canN` directly.
