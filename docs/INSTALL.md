# Install & update

* [Quick install (Linux laptop)](#quick-install-linux-laptop)
* [Pick your setup](#pick-your-setup)
* [Updating](#updating)
* [Plugins](#plugins)
* [Uninstalling](#uninstalling)

## Quick install (Linux laptop)

Linux Mint 21+, Ubuntu 22.04+ or Debian 12+. Open a terminal (**Ctrl+Alt+T**) and paste:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/AidanIsaacson/CANopener.git ~/CANopener
~/CANopener/deploy/linux/install.sh
```

Type your password when it asks (nothing shows while you type). About 10 minutes later
CANopener is in the application menu and on the desktop, and a USB CAN adapter works the first
time you plug it in. The installer is safe to run again at any time: it repairs anything missing.

What it installs, your adapter's firmware, and troubleshooting:
[the Linux laptop guide](install/linux-laptop.md).

## Pick your setup

| You have | Guide |
|---|---|
| A Linux laptop (Mint, Ubuntu, Debian) | [Linux laptop](install/linux-laptop.md) |
| A Raspberry Pi with a screen, starting CANopener full screen at power-on | [Raspberry Pi](install/raspberry-pi.md) (tested on a Pi 5) |
| The handheld build: Pi 5, Touch Display 2, buttons and encoder | [Handheld, first steps](install/handheld-stage0.md) |
| Windows or macOS | Not supported for a real bus yet: CANopener uses Linux's built-in CAN support (SocketCAN) |

**Adapters:** anything Linux sees as a CAN interface. The installers set up the CANable 2.0 /
2.0 Pro (either firmware) for you; see
[Your USB adapter](install/linux-laptop.md#your-usb-adapter-canable-20--20-pro). Other adapters
(PEAK, Kvaser, Vector, IXXAT ...): install the maker's driver, then
[Other adapters](USAGE.md#other-adapters).

## Updating

**In the app** (laptop or Pi): **Help → Check for updates…** shows what's new, **Install update**
downloads it, and **Restart now** opens the new version. On the handheld screen it's **System →
Software → Check for updates**. It only checks when you ask, unless you tick **Help → Check for updates at
startup** (it stays quiet unless there is one).

If an update also changes the system setup (drivers, plug-in rules), the app says so and shows
the installer command to run once in a terminal.

**From a terminal**, the full update (code, drivers, rules and dependencies in one go):

```bash
~/CANopener/deploy/linux/install.sh --update                             # laptop
cd ~/CANopener && git pull && ./deploy/raspberry-pi/install.sh --sudoers --autostart   # Pi
```

On the Pi, use the same options you installed with, then reboot (or
`systemctl --user restart canopener`). Over SSH, `canopener-cli update` does what the app's
update does.

Your settings, captures and profiles are never touched by an update.

## Plugins

Plugins add machine-specific screens and profiles without changing CANopener. They install
straight from GitHub repositories, private ones included, with no keyboard needed:

1. **System → Software → Plugins → Sign in to GitHub** (on the handheld screen) shows a code.
2. On your phone, open `github.com/login/device`, enter the code and approve.
3. **Add a repository** installs a plugin. **Update all** brings them all up to date.

The desktop app has no Plugins screen yet; use a terminal there: `canopener-cli plugins login`,
`canopener-cli plugins add owner/name`, `canopener-cli plugins update`. **Sign out** (or
`canopener-cli plugins logout`) forgets the sign-in on the device.

Setting up a plugin repository and its GitHub App:
[DEVELOPING.md](DEVELOPING.md#private-plugins-installing-from-the-device-no-keyboard).

## Uninstalling

Laptop: [Uninstalling](install/linux-laptop.md#uninstalling) in the Linux laptop guide. Your
captures and reports stay in `~/canopener/` unless you delete them.
