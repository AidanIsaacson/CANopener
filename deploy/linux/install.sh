#!/bin/bash
# Install or update CANopener on a Linux laptop or desktop (Mint, Ubuntu, Debian).
#
#   ./deploy/linux/install.sh            install everything (safe to re-run any time)
#   ./deploy/linux/install.sh --update   download the latest version first, then the same
#
# Options:
#   --no-desktop-icon   only add CANopener to the application menu, not the desktop
#   --no-sudoers        don't install the passwordless CAN rule (bring-up asks for a password)
#
# Run it as your normal user, not with sudo: it asks for your password when it needs root.
# Ends with CANopener in the menu / on the desktop and the USB adapter drivers in place.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
VENV="$REPO/.venv"

UPDATE=0 ICON=1 SUDOERS=1
PASS=()
for arg in "$@"; do
    case "$arg" in
    --update) UPDATE=1 ;;
    --no-desktop-icon) ICON=0; PASS+=("$arg") ;;
    --no-sudoers) SUDOERS=0; PASS+=("$arg") ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option $arg (try --help)" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not with sudo (it asks for your password when needed)." >&2
    exit 1
fi
if ! command -v apt-get >/dev/null; then
    echo "This installer is for Debian-family systems (Mint, Ubuntu, Debian, Raspberry Pi OS)." >&2
    exit 1
fi

if [ "$UPDATE" -eq 1 ]; then
    echo "== Downloading the latest CANopener"
    git -C "$REPO" pull --ff-only
    # Re-run the (possibly updated) installer itself.
    exec "$HERE/install.sh" "${PASS[@]}"
fi

echo "== System packages (asks for your password)"
# A broken third-party source (an expired key, say) makes 'apt-get update' fail even though the
# Ubuntu/Debian lists it needs were refreshed, so that's a warning, not a reason to stop.
sudo apt-get update || echo "   WARNING: apt-get update reported errors (see above; usually a third-party" \
    "source, not something CANopener needs). Carrying on." >&2
# python3-venv: the app's private environment. lib*: what the Qt GUI needs to open a window
# (without them it installs fine but never appears).
sudo apt-get install -y git python3 python3-venv can-utils \
    libegl1 libgl1 libfontconfig1 libdbus-1-3 libxkbcommon0 libxkbcommon-x11-0 \
    libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
    libxcb-render-util0 libxcb-shape0 libxcb-xkb1

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo "CANopener needs Python 3.10 or newer; this system has $(python3 --version). Use Mint 21+," >&2
    echo "Ubuntu 22.04+ or Debian 12+." >&2
    exit 1
fi

echo "== USB CAN adapter drivers"
sudo "$HERE/adapter-setup.sh"

echo "== CANopener in $VENV"
if [ -e "$VENV" ] && ! "$VENV/bin/python" -c 'import sys' >/dev/null 2>&1; then
    echo "   the old environment no longer runs (system Python upgraded?) - rebuilding it"
    rm -rf "$VENV"
fi
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --upgrade -e "${REPO}[gui,device]"     # device: the handheld UI (Pygame)
if ! err="$("$VENV/bin/python" -c 'from canopener.gui.qt import QtWidgets' 2>&1)"; then
    echo "   WARNING: the GUI library doesn't load:" >&2
    echo "$err" >&2
fi

if [ "$SUDOERS" -eq 1 ]; then
    echo "== Passwordless CAN bring-up (only 'ip link set canN ...', no password stored)"
    tmp="$(mktemp)"
    if "$VENV/bin/python" -c 'import getpass; from canopener.core.interfaces import sudoers_content
print(sudoers_content(getpass.getuser()), end="")' >"$tmp" \
        && sudo visudo -cf "$tmp" >/dev/null \
        && sudo install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/canopener; then
        echo "   ok"
    else
        echo "   skipped: couldn't install it. Connecting will ask for your password instead." >&2
    fi
    rm -f "$tmp"
fi

echo "== Menu entry and desktop icon"
if [ "$ICON" -eq 1 ]; then
    "$VENV/bin/canopener" --install-desktop --desktop-shortcut
else
    "$VENV/bin/canopener" --install-desktop
fi

echo
echo "Done. Start CANopener from the application menu$([ "$ICON" -eq 1 ] && echo " or the desktop icon")."
echo "CAN adapters seen right now:"
"$VENV/bin/canopener-cli" interfaces | sed 's/^/   /'
