#!/bin/bash
# Install CANopener on a Raspberry Pi (Raspberry Pi OS 64-bit) for the current user.
#   ./deploy/raspberry-pi/install.sh            # venv + launcher + menu entry + the root permissions below
#   ./deploy/raspberry-pi/install.sh --autostart  # also start at login (systemd user service)
#   ./deploy/raspberry-pi/install.sh --device     # handheld: encoder + brightness access (see docs/DEVICE.md)
#   ./deploy/raspberry-pi/install.sh --no-sudoers # leave out the sudoers rule and boot-settings helper
# Without --no-sudoers it installs /etc/sudoers.d/canopener (CAN interfaces up / down without a
# password) and /usr/local/sbin/canopener-boot-config (System > Installed options sets the Pi up).
# --sudoers is still accepted (it was needed before; now it's the default).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
VENV="${CANOPENER_VENV:-$HOME/canopener-venv}"
BIN="$HOME/.local/bin"
LAUNCHER="$BIN/canopener-launch"

SUDOERS=1
for arg in "$@"; do
    case "$arg" in
    --no-sudoers) SUDOERS=0 ;;
    --sudoers|--autostart|--device) ;;
    *) echo "unknown option $arg" >&2; exit 2 ;;
    esac
done

echo "== System packages"
sudo apt-get install -y python3-venv can-utils \
    libegl1 libgl1 libfontconfig1 libdbus-1-3 libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0
# The font the handheld's themes use (System > Display > Theme); without it, DejaVu Sans.
sudo apt-get install -y fonts-inter || echo "(fonts-inter isn't available: the themes use DejaVu Sans)"

echo "== USB CAN adapter drivers"
sudo "$REPO/deploy/linux/adapter-setup.sh"

echo "== Python environment in $VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -e "${REPO}[gui,device,adapters]"
# The Pi is the handheld: start the touch UI unless a UI was already chosen (switch either way
# in the app: Settings > Handheld (Pi) screen / System > Desktop app instead).
"$VENV/bin/python" -c 'from canopener.core.settings import Settings; s = Settings(); s.get("ui") or s.set("ui", "device")'

echo "== Launcher $LAUNCHER"
mkdir -p "$BIN"
install -m 755 "$HERE/canopener-launch.sh" "$LAUNCHER"

echo "== Menu entry"
mkdir -p "$HOME/.local/share/applications"
sed -e "s|CANOPENER_LAUNCHER|$LAUNCHER|" -e "s|CANOPENER_ICON|$REPO/canopener/assets/canopener.svg|" "$HERE/canopener.desktop" > "$HOME/.local/share/applications/canopener.desktop"

if [ "$SUDOERS" = 1 ]; then
    echo "== /etc/sudoers.d/canopener (user $USER)"
    tmp="$(mktemp)"
    sed "s/USERNAME/$USER/g" "$HERE/canopener.sudoers" > "$tmp"
    sudo visudo -cf "$tmp"
    sudo install -m 440 -o root -g root "$tmp" /etc/sudoers.d/canopener
    rm -f "$tmp"
    echo "== /usr/local/sbin/canopener-boot-config (Installed options can set up the Pi)"
    sudo install -m 755 -o root -g root "$HERE/canopener-boot-config" /usr/local/sbin/canopener-boot-config
fi

for arg in "$@"; do
    case "$arg" in
    --autostart)
        echo "== systemd user service"
        mkdir -p "$HOME/.config/systemd/user"
        sed "s|CANOPENER_LAUNCHER|$LAUNCHER|" "$HERE/canopener.service" > "$HOME/.config/systemd/user/canopener.service"
        systemctl --user daemon-reload
        systemctl --user enable canopener
        # Raspberry Pi OS's desktop (labwc) never reaches graphical-session.target: its login
        # autostart starts the service instead. Replaces an older copy of the menu entry there.
        mkdir -p "$HOME/.config/autostart"
        install -m 644 "$HERE/canopener-autostart.desktop" "$HOME/.config/autostart/canopener.desktop"
        echo "   starts at next login; 'systemctl --user start canopener' to start now"
        ;;
    --device)
        echo "== Handheld: rotary encoder (input group), screen brightness (video group + udev rule)"
        sudo usermod -aG input,video "$USER"
        sudo install -m 644 "$HERE/71-canopener-backlight.rules" /etc/udev/rules.d/71-canopener-backlight.rules
        sudo udevadm control --reload-rules
        sudo udevadm trigger --subsystem-match=backlight || true
        echo "   log out and in again for the groups to apply"
        echo "   wire the buttons / encoder, then set Buttons and encoder to Installed (System > Installed options)"
        ;;
    esac
done
echo "Done. Run 'canopener-launch', or find CANopener in the menu."
