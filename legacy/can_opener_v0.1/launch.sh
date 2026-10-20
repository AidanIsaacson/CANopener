#!/bin/bash
# launch.sh — CAN Opener launcher
# Brings up the CAN interface, then starts the app.
# Place this in ~/can_opener/ and make executable: chmod +x launch.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IFACE="can0"
BITRATE="250000"
LOGFILE="/tmp/can_opener.log"

# Wait for desktop to be ready (relevant on autostart)
sleep 2

# Bring up CAN interface (ignore failure — app handles reconnect)
sudo ip link set "$IFACE" down 2>/dev/null
sudo ip link set "$IFACE" up type can bitrate "$BITRATE" 2>/dev/null
if [ $? -eq 0 ]; then
    echo "$(date): $IFACE up at ${BITRATE}bps" >> "$LOGFILE"
else
    echo "$(date): WARNING — could not bring up $IFACE, app will handle" >> "$LOGFILE"
fi

# Launch app (fullscreen on Pi display)
cd "$SCRIPT_DIR"
exec python3 main.py >> "$LOGFILE" 2>&1
