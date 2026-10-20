#!/bin/bash
# can_up.sh — Bring up CAN interface on Raspberry Pi
# Usage: ./can_up.sh [interface] [bitrate]
# Defaults: can0, 250000

IFACE=${1:-can0}
BITRATE=${2:-250000}

echo "Bringing up $IFACE @ ${BITRATE}bps..."

sudo ip link set $IFACE down 2>/dev/null
sudo ip link set $IFACE up type can bitrate $BITRATE

if [ $? -eq 0 ]; then
    echo "✓ $IFACE is up at ${BITRATE}bps"
    ip -details link show $IFACE
else
    echo "✗ Failed to bring up $IFACE"
    exit 1
fi
