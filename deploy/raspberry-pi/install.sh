#!/bin/bash
# Install CANopener on a Raspberry Pi (Raspberry Pi OS 64-bit) for the current user.
#   ./deploy/raspberry-pi/install.sh            # venv + launcher + menu entry
#   ./deploy/raspberry-pi/install.sh --sudoers  # also allow passwordless CAN setup
#   ./deploy/raspberry-pi/install.sh --autostart  # also start at login (systemd user service)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
VENV="${CANOPENER_VENV:-$HOME/canopener-venv}"
BIN="$HOME/.local/bin"
LAUNCHER="$BIN/canopener-launch"

echo "== Python environment in $VENV"
sudo apt-get install -y python3-venv can-utils
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -e "$REPO[gui]"

echo "== Launcher $LAUNCHER"
mkdir -p "$BIN"
install -m 755 "$HERE/canopener-launch.sh" "$LAUNCHER"

echo "== Menu entry"
mkdir -p "$HOME/.local/share/applications"
sed "s|CANOPENER_LAUNCHER|$LAUNCHER|" "$HERE/canopener.desktop" > "$HOME/.local/share/applications/canopener.desktop"

for arg in "$@"; do
    case "$arg" in
    --sudoers)
        echo "== /etc/sudoers.d/canopener (user $USER)"
        tmp="$(mktemp)"
        sed "s/USERNAME/$USER/g" "$HERE/canopener.sudoers" > "$tmp"
        sudo visudo -cf "$tmp"
        sudo install -m 440 -o root -g root "$tmp" /etc/sudoers.d/canopener
        rm -f "$tmp"
        ;;
    --autostart)
        echo "== systemd user service"
        mkdir -p "$HOME/.config/systemd/user"
        sed "s|CANOPENER_LAUNCHER|$LAUNCHER|" "$HERE/canopener.service" > "$HOME/.config/systemd/user/canopener.service"
        systemctl --user daemon-reload
        systemctl --user enable canopener
        echo "   starts at next login; 'systemctl --user start canopener' to start now"
        ;;
    *) echo "unknown option $arg" >&2; exit 2 ;;
    esac
done
echo "Done. Run 'canopener-launch', or find CANopener in the menu."
