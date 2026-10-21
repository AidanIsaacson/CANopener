# Install on a Linux laptop (Mint / Ubuntu / Debian)

About 10 minutes. You'll end up with CANopener in your application menu, and optionally an icon
on the desktop, that you can click to start like any other program. Nothing here needs a CAN
adapter: the built-in simulator lets you try everything first.

> **Windows / macOS:** not supported for talking to a real bus yet (CANopener uses Linux's
> built-in CAN support, "SocketCAN"). The Raspberry Pi guide is the other supported setup.

## 1. Install the system tools

Open a terminal (Menu → **Terminal**, or Ctrl+Alt+T) and paste:

```bash
sudo apt update
sudo apt install -y git python3-venv can-utils \
  libegl1 libxkbcommon0 libfontconfig1 libdbus-1-3 libxcb-cursor0
```

It asks for your password; nothing shows as you type, which is normal.
`can-utils` is optional but handy (`candump can0` shows raw traffic). The four `lib*`
packages are system libraries the Qt GUI needs; without them the app installs fine but the
window never opens (you'd see a `libEGL.so.1: cannot open shared object file` error if you ran
it from a terminal). Most desktops already have them, but installing them now saves a
head-scratch later.

## 2. Download CANopener

```bash
cd ~
git clone https://github.com/AidanIsaacson/CANopener.git
cd CANopener
```

This makes a `CANopener` folder in your home folder. Keep it there; the menu icon points at it.

## 3. Install it into its own Python environment

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[gui]"
```

This downloads the Qt GUI library (about 100 MB) into `CANopener/.venv`, so it can't interfere
with the rest of your system.

## 4. Try it with the simulator

```bash
.venv/bin/canopener --sim
```

The window opens already connected to a simulated telehandler. Try **Full Scan** on the Fault
Codes tab, tick a few signals on **Live Data**, and open the **📝 Log** panel. Close the window
when you're done.

## 5. Add it to the menu and desktop

```bash
.venv/bin/canopener --install-desktop --desktop-shortcut
```

CANopener is now in the application menu (under **Accessories** / **Utilities**, or just
search "CANopener"), with an icon on the desktop. From now on you never need the terminal to
start it. Leave out `--desktop-shortcut` if you only want the menu entry.

The installer marks the desktop icon as trusted. If your desktop still asks about an
"untrusted launcher" the first time, choose the option to trust / launch it; it only asks once.

## 6. Connect to a real machine

1. Plug the USB-CAN adapter into the laptop and the machine's diagnostic connector, key on.
2. In CANopener, pick **SocketCAN adapter** in the top-left box. The interface (usually `can0`)
   should appear next to it; press **⟳** if it doesn't.
3. Pick the bitrate (**250k** for most J1939 machines, or **Auto** to detect it safely in
   listen-only mode) and press **Set Up Interface**. It asks for your password the first time:
   bringing a network interface up needs admin rights.
4. Press **Connect**, then **Read Active** / **Full Scan**.

Tick **Listen-only** if you want to be certain the laptop sends nothing at all onto the bus.

### Stop it asking for a password every time

Bringing the interface up needs root, so by default you get a password prompt each session. To do
it once and never again, use **Settings → Set up passwordless CAN access…** (or
`.venv/bin/canopener-cli allow-setup`). It installs a small system rule — after one password
prompt — that lets your user run **only** `ip link set canN …` as root. No password is stored
anywhere, and the app itself never runs as root.

### Which USB adapter?

Get one that speaks **SocketCAN natively** (the `gs_usb` driver) so it just appears as `can0` with
no extra setup.

* **Recommended: CANable 2.0 with candleLight/gs_usb firmware** (~$30). Native SocketCAN, and it's
  **CAN FD** — a good stand-in for the MCP2518FD in the planned handheld, and future-proof for
  J1939-22 (it's fully backward-compatible with classic 250k CAN). Innomaker USB-CAN-FD or a CANine
  are equivalent alternatives.
* Make sure it's the **candleLight/gs_usb** firmware, not the older **slcan** one. slcan shows up as
  a serial port and needs a manual step each plug-in (below).
* `gs_usb` adapters don't support bus-off auto-restart (`restart-ms`); CANopener handles that
  automatically, so the interface still comes up — you just don't get auto-recovery (your final
  MCP2518FD hardware will, via the `mcp251xfd` driver).

| Adapter | Shows up as |
|---|---|
| CANable 2.0 / candleLight / other gs_usb, PEAK PCAN-USB, Kvaser, 8devices USB2CAN | `can0` straight away (drivers built into Linux) |
| CANable with the original "slcan" firmware | a serial port. Run once after plugging in: `sudo slcand -o -s5 -t hw -S 3000000 /dev/ttyACM0 can0 && sudo ip link set can0 up` (`-s5` = 250k, `-s6` = 500k) |

Check what Linux sees with `ip link show` (look for `can0`) and `.venv/bin/canopener-cli interfaces`.

## Updating

```bash
cd ~/CANopener
git pull
.venv/bin/pip install -e ".[gui]"
```

The menu entry and desktop icon keep working; there's nothing to reinstall.

## Uninstalling

```bash
~/CANopener/.venv/bin/canopener --uninstall-desktop
rm -rf ~/CANopener
```

Your saved captures and reports stay in `~/canopener/` (lower-case), and settings in
`~/.config/canopener/`. Delete those too if you want them gone.

## Troubleshooting

| Problem | Fix |
|---|---|
| `libEGL.so.1: cannot open shared object file` (or another `.so` missing) | `sudo apt install -y libegl1 libxkbcommon0 libfontconfig1 libdbus-1-3 libxcb-cursor0` |
| `Could not load the Qt platform plugin "xcb"` / `xcb-cursor0 or libxcb-cursor0 is needed` | `sudo apt install -y libxcb-cursor0` (required by Qt 6.5+) |
| `python3 -m venv` says ensurepip is not available | `sudo apt install -y python3-venv` (on some versions `python3.12-venv`), then repeat step 3 |
| `Could not open can0: No such device` | The adapter isn't detected. Check `ip link show`; replug it; see the adapter notes above. |
| Connected but no traffic, or lots of errors in the status bar | Wrong bitrate. Use **Auto**, or try 500k. Check the key is on. |
| `Network is down` | Press **Set Up Interface** (the interface has to be brought up after every replug or reboot). |
| The menu icon does nothing | Run `~/CANopener/.venv/bin/canopener` in a terminal to see the error, then re-run step 5. Re-run step 5 after moving the folder. |

Still stuck? Open an issue on GitHub with the error text, or attach a capture's `summary.txt`.
