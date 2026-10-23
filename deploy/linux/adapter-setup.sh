#!/bin/bash
# System part of the CANopener install (run as root; install.sh calls it with sudo). Safe to
# re-run. Makes a USB CAN adapter work the first time it's plugged in:
#   * the kernel CAN drivers (gs_usb for candleLight firmware, slcan for slcan firmware),
#     installing Ubuntu/Mint's linux-modules-extra package if they're missing;
#   * can-utils (slcand, candump);
#   * 70-canopener.rules + canopener-slcan@.service, so an slcan-firmware CANable is attached
#     as canN automatically on plug-in and ModemManager leaves it alone.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "adapter-setup.sh must run as root: sudo $0" >&2
    exit 1
fi

echo "== can-utils"
apt-get update -qq
apt-get install -y can-utils kmod

missing_modules() {
    local m out=()
    for m in can can_raw can_dev gs_usb slcan; do
        modinfo "$m" >/dev/null 2>&1 || out+=("$m")
    done
    echo "${out[@]:-}"
}

echo "== CAN drivers for kernel $(uname -r)"
missing="$(missing_modules)"
if [ -n "$missing" ]; then
    # Ubuntu and Mint ship the USB CAN drivers in a separate package.
    extra="linux-modules-extra-$(uname -r)"
    if apt-cache show "$extra" >/dev/null 2>&1; then
        echo "   missing: $missing - installing $extra"
        apt-get install -y "$extra"
        missing="$(missing_modules)"
    fi
fi
if [ -n "$missing" ]; then
    echo "   WARNING: this kernel has no driver for: $missing" >&2
    echo "   USB CAN adapters may not work. Install your distro's full kernel package and reboot." >&2
else
    echo "   ok (gs_usb, slcan, can_raw)"
fi
modprobe -a can can_raw 2>/dev/null || true

IFS=. read -r kmaj kmin _ <<<"$(uname -r)"
if [ "$kmaj" -lt 6 ]; then
    echo "   NOTE: Linux $kmaj.$kmin. A CANable with slcan firmware needs Linux 6.0 or newer to have its" >&2
    echo "   bitrate set by CANopener. Either flash it with candleLight firmware (works on any kernel)," >&2
    echo "   or install a newer kernel (Mint: Update Manager -> View -> Linux kernels)." >&2
fi

echo "== Plug-in rules for CANable adapters"
if ! command -v udevadm >/dev/null || [ ! -d /run/systemd/system ]; then
    echo "   WARNING: no udev/systemd here, so slcan-firmware adapters won't attach by themselves." >&2
    echo "   Attach one by hand: sudo slcand /dev/ttyACM0 can0" >&2
    exit 0
fi
install -D -m 0644 "$HERE/70-canopener.rules" /etc/udev/rules.d/70-canopener.rules
install -D -m 0644 "$HERE/canopener-slcan@.service" /etc/systemd/system/canopener-slcan@.service
systemctl daemon-reload
udevadm control --reload-rules
# Attach an slcan adapter that is already plugged in (others are unaffected: no rule matches).
udevadm trigger --action=add --subsystem-match=tty --sysname-match='ttyACM*' || true
echo "   ok"
