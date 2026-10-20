#!/bin/bash
# Start CANopener on a Raspberry Pi: bring up the CAN interface, then launch the GUI fullscreen.
#
# Settings come from environment variables (set them in the systemd unit or ~/.profile):
#   CANOPENER_IFACE    CAN interface            (default can0)
#   CANOPENER_BITRATE  bits/s, or "auto"        (default 250000)
#   CANOPENER_VENV     virtualenv with canopener installed (default ~/canopener-venv)
#   CANOPENER_ARGS     extra GUI arguments      (default "--fullscreen")
#   QT_QPA_PLATFORM    leave unset under a desktop; "eglfs" to run with no desktop on a
#                      DSI/HDMI screen, or "linuxfb:fb=/dev/fb1" for an SPI TFT
set -u
IFACE="${CANOPENER_IFACE:-can0}"
BITRATE="${CANOPENER_BITRATE:-250000}"
VENV="${CANOPENER_VENV:-$HOME/canopener-venv}"
ARGS="${CANOPENER_ARGS:---fullscreen}"
LOG="${XDG_STATE_HOME:-$HOME/.local/state}/canopener/launch.log"
mkdir -p "$(dirname "$LOG")"

[ -x "$VENV/bin/canopener" ] && export PATH="$VENV/bin:$PATH"

{
    echo "=== $(date) starting (iface $IFACE, bitrate $BITRATE)"
    # Failure is not fatal: the app can still be used for logs, the simulator, or setting
    # the interface up from its toolbar.
    canopener-cli setup --iface "$IFACE" --bitrate "$BITRATE" || echo "interface setup failed, continuing"
} >>"$LOG" 2>&1

# shellcheck disable=SC2086  # ARGS is intentionally word-split
exec canopener --iface "$IFACE" $ARGS >>"$LOG" 2>&1
