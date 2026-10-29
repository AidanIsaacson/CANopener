# Install on a Linux laptop (Mint / Ubuntu / Debian)

About 10 minutes, almost all of it downloading. At the end CANopener is in your application menu
and on your desktop. Click it to start, like any other program, and a USB CAN adapter works the
first time you plug it in. You run three commands in a terminal once. After that you never need
the terminal again.

**You need:** Linux Mint 21 or newer, Ubuntu 22.04 or newer, or Debian 12 or newer, an internet
connection, and your login password. You don't need the CAN adapter yet.

> **Windows / macOS:** not supported for talking to a real bus yet (CANopener uses Linux's
> built-in CAN support, "SocketCAN"). The [Raspberry Pi guide](raspberry-pi.md) is the other
> supported setup.

## 1. Open a terminal

Menu → **Terminal**, or press **Ctrl+Alt+T**.

## 2. Install

Paste these three lines (right-click → Paste, or Ctrl+Shift+V) and press Enter:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/AidanIsaacson/CANopener.git ~/CANopener
~/CANopener/deploy/linux/install.sh
```

When it asks for your password, type it and press Enter. Nothing shows while you type, which is
normal. The installer:

1. installs the system packages: Python, `can-utils`, and the libraries the window needs (without
   them the app installs but its window never opens);
2. installs the **USB CAN adapter drivers** (`gs_usb` and `slcan`; on Ubuntu/Mint it adds the
   `linux-modules-extra` package if your kernel doesn't include them) and a plug-in rule so a
   **CANable** becomes `can0` by itself, whichever firmware it has (see
   [the adapter section](#your-usb-adapter-canable-20--20-pro));
3. installs CANopener and its GUI library (~100 MB) into `~/CANopener/.venv`, kept separate from
   the rest of your system;
4. lets CANopener bring the CAN interface up **without asking for a password**. This only allows
   `ip link set canN …` as root. No password is stored, and the app never runs as root.
   Use `--no-sudoers` if you'd rather type your password at each connect;
5. adds **CANopener** to the application menu and puts an icon on the desktop.

It ends with `Done.` and a list of the CAN adapters it can see. It's safe to run again at any
time; it just repairs anything missing.

The first time you double-click the desktop icon, some desktops ask whether to trust the launcher.
Choose **Trust and Launch** (or **Allow Launching**). It only asks once.

## 3. Try it with the simulator (no adapter needed)

Start CANopener from the menu or the desktop icon. Tick **Connection → Show simulators**, pick
**Simulator** in the top-left box and press **Connect**. Try **Full Scan** on the Fault Codes tab, tick a few signals on **Live Data**, and open
the **📝 Log** panel.

## 4. Connect to a real machine

1. Plug the USB adapter into the laptop. On the first plug-in, wait about 5 seconds for Linux to
   set it up.
2. Connect the adapter to the machine's diagnostic connector and turn the key on.
3. In CANopener open **🔌 Connection** (top-left button, or F3) and pick **SocketCAN adapter**.
   The interface (`can0`) appears below it. Its list shows only adapters that are connected right now, and it looks again
   every time you open it (or press **⟳**), so plugging in after the app opened is fine.
4. Pick the bitrate: **250k** for most J1939 machines, **500k** for cars (OBD-II: also pick the
   **OBD-II vehicle** profile, top right) and some newer
   machines, or **Auto** to detect it. Auto listens in listen-only mode, so it never disturbs the
   bus while it searches.
5. Press **Connect**, then **Read Active** / **Full Scan**.

Connect sets the bitrate and brings the interface up for you, every time, without a password.

**One-click from then on:** tick **Settings → Connect on startup**. After that, clicking the icon
with the adapter plugged in opens CANopener already connected. If the adapter isn't plugged in, the
app just says so in the status bar. With **Auto** bitrate selected as well, it works safely on any
machine.

Tick **Listen-only** if you want to be certain the laptop sends nothing at all onto the bus.

The **Adapter** and **Bus** lamps at the bottom of the window show whether the adapter is plugged
in and whether it hears the machine. Probing for the CAN wires? Press **Find signal** in the
Connection sidebar and go move the probes: it chimes and connects as soon as it hears steady
traffic. Turn the laptop's volume up.

## Your USB adapter (CANable 2.0 / 2.0 Pro)

CANable boards are sold with one of two firmwares, and which one you get depends on the seller.
**Both work after the installer above, with nothing more to do.**

| Firmware | What Linux sees | What happens |
|---|---|---|
| **candleLight** (gs_usb) | a native CAN device, USB ID `1d50:606f` | Linux's `gs_usb` driver makes it `can0` immediately. |
| **slcan** | a serial port `/dev/ttyACM0`, USB ID `16d0:117e` | The installer's plug-in rule attaches it with `slcand` so it becomes `can0` too, and stops ModemManager from probing it as a modem. Needs Linux 6.0 or newer (`uname -r`): Mint 22, Ubuntu 24.04 and Debian 12 have it. **No listen-only** (see below). |

To see which one you have, plug it in and run `lsusb`: `1d50:606f` is candleLight and `16d0:117e`
is slcan. `~/CANopener/.venv/bin/canopener-cli interfaces` lists what CANopener sees, including an
adapter that's plugged in but not set up, with the reason.

**slcan firmware can't do listen-only.** Linux opens it in listen-only with a command the CANable's
slcan firmware doesn't have, so the adapter stays closed and hears nothing at any bitrate, while
`ip` still shows the interface up and healthy. CANopener refuses listen-only on it and says so:
**Auto**, **Find signal** and the **Listen-only** box all need it. Pick the bitrate by hand
(250k for most J1939 machines) with Listen-only unticked, or switch to candleLight.

**candleLight is the better firmware** (faster, no serial layer, works on any kernel, real
listen-only). If yours
has slcan and you want to switch, open the CANable firmware updater (linked from canable.io) in
Chrome or Edge. Set the adapter's **BOOT** jumper (or hold its boot button) while plugging it in,
pick the candleLight build for your board (**CANable 2.0** for 2.0 / 2.0 Pro boards, unless your
seller supplies their own), and flash. Then remove the jumper and replug. The installer already
lets your user flash it without root.

Wiring: connect CAN-H, CAN-L and **GND** (on the isolated 2.0 Pro, GND is the isolated side's
ground and is needed for a reliable link). If you're connecting at the end of a
bus that isn't already terminated (e.g. bench-testing one ECU), turn on the board's 120 Ω
termination switch or jumper. A machine's diagnostic connector is normally already terminated,
so leave it off there.

Other adapters that work with no extra setup: Innomaker USB2CAN / USB-CAN-FD, CANine, PEAK
PCAN-USB, Kvaser, 8devices USB2CAN (all native SocketCAN). `gs_usb` adapters don't support bus-off
auto-restart (`restart-ms`). CANopener handles that, so the interface still comes up; you just don't
get automatic recovery from bus-off.

## Updating

In the app: **Help → Check for updates…** shows what's new, **Install update** downloads it, and
**Restart now** opens the new version. It checks only when you ask, unless you tick **Help → Check
for updates at startup** (off by default; it stays quiet unless there is an update). If an update also changes
the system setup (drivers, rules), the window says so and shows the installer command to run once.

From a terminal, the full update (code, drivers, rules and dependencies in one go):

```bash
~/CANopener/deploy/linux/install.sh --update
```

This downloads the latest version and re-runs the installer, so new drivers, rules or
dependencies are picked up too. The menu entry and desktop icon keep working. Your settings,
captures and profiles are untouched.

## Uninstalling

```bash
~/CANopener/.venv/bin/canopener --uninstall-desktop
sudo rm -f /etc/sudoers.d/canopener /etc/udev/rules.d/70-canopener.rules \
           /etc/systemd/system/canopener-slcan@.service
rm -rf ~/CANopener
```

Your saved captures and reports stay in `~/canopener/` (lower-case), and settings in
`~/.config/canopener/` and `~/.config/CANopener/`. Delete those too if you want them gone.

## Troubleshooting

First run `~/CANopener/.venv/bin/canopener-cli interfaces`. It shows the CAN interfaces and any
recognised USB adapter, and explains what's wrong if the adapter is plugged in but no interface
appeared.

| Problem | Fix |
|---|---|
| Anything odd after an update or a system upgrade | Re-run `~/CANopener/deploy/linux/install.sh`. It repairs packages, drivers, rules and the app's environment (it rebuilds the environment if a system upgrade broke it). |
| The menu icon does nothing | Run `~/CANopener/.venv/bin/canopener` in a terminal to see the error. Usually re-running the installer fixes it. If you moved the `CANopener` folder, re-run the installer from its new place. |
| `libEGL.so.1: cannot open shared object file` / `Could not load the Qt platform plugin "xcb"` | A system library is missing: re-run the installer. |
| "No CAN adapter found" with it plugged in | Check `lsusb` shows it. Try another USB port or cable (some cables only carry power). On a brand-new install, unplug and replug once. |
| "slcan firmware … isn't attached" | Linux older than 6.0 (Mint 21 ships 5.15): install a newer kernel (Mint: Update Manager → View → Linux kernels → 6.x, then reboot), or flash candleLight firmware (above). Otherwise re-run the installer, then replug. |
| "is in firmware-update (DFU) mode" | Remove the BOOT jumper / don't hold the button, and replug. |
| `this kernel has no driver for: gs_usb …` during install | Your kernel is a cut-down one. `sudo apt install linux-generic` (Ubuntu/Mint) or `linux-image-amd64` (Debian), reboot, re-run the installer. |
| Connected but no traffic, or lots of errors in the status bar | Wrong bitrate. Use **Auto**, or try 500k. Check the key is on and CAN-H/CAN-L aren't swapped. |
| "slcan firmware, which can't do listen-only" | The adapter has slcan firmware (above): untick **Listen-only** and pick the bitrate instead of **Auto**, or flash candleLight. To check by hand, `candump` hears nothing while `ip -details link show can0` says `LISTEN-ONLY`. |
| "stopped answering" (or `RTNETLINK answers: Connection timed out` / `Broken pipe`) | Some USB adapters (seen on an InnoMaker USB2CAN) lock up when they're started at the wrong bitrate on a busy bus, which **Auto** and **Find signal** do while searching. Turn the key off or unplug the adapter from the machine for a moment (or replug it into the laptop), then pick the bitrate by hand. |
| Connect asks for a password | The passwordless rule isn't installed. Re-run the installer (or use **Settings → Set up passwordless CAN access…**). |

Still stuck? Open an issue on GitHub with the output of `canopener-cli interfaces` and the error
text, or attach a capture's `summary.txt`.
