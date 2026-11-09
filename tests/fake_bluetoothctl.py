"""A stand-in for BlueZ's bluetoothctl, for the Bluetooth tests: a dual-mode vLinker (its Classic
and BLE sides listed separately, as the real one can be), a pair of headphones, and a PIN prompt
without a newline the way bluetoothctl prints it. State (paired devices) lives in a JSON file
named by FAKE_BT_STATE."""

import json
import os
import sys

STATE = os.environ["FAKE_BT_STATE"]
DEVICES = {
    "00:1D:A5:68:98:8A": {"name": "vLinker MC-Android", "info": "Class: 0x00001f00\n\tUUID: Serial Port "
                          "(00001101-0000-1000-8000-00805f9b34fb)\n\tRSSI: -48"},
    "C4:7F:0E:11:22:33": {"name": "vLinker MC-IOS", "info": "AddressType: random\n\tUUID: Generic Attribute "
                          "Profile (00001801-0000-1000-8000-00805f9b34fb)"},
    "AC:80:0A:44:55:66": {"name": "WH-1000XM4", "info": "Class: 0x00240404\n\tIcon: audio-headset"},
}


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except OSError:
        return {"powered": True, "paired": [], "pin": "1234"}


def save(st):
    with open(STATE, "w") as f:
        json.dump(st, f)


def main(args):
    st = load()
    if not args:
        return interactive(st)
    if args[0] == "show":
        print("Controller B8:27:EB:00:00:01 (public)\n\tName: canopener\n\tPowered: "
              + ("yes" if st["powered"] else "no") + "\n\tPowerState: " + ("on" if st["powered"] else "off"))
    elif args[0] == "power":
        st["powered"] = args[1] == "on"
        save(st)
        print("Changing power on succeeded")
    elif args[:2] == ["devices", "Paired"]:
        for a in st["paired"]:
            print(f"Device {a} {DEVICES[a]['name']}")
    elif args[0] == "devices":
        for a, d in DEVICES.items():
            print(f"Device {a} {d['name']}")
    elif args[0] == "--timeout":
        print("Discovery started\n\x1b[0;92m[NEW]\x1b[0m Device 00:1D:A5:68:98:8A vLinker MC-Android\n"
              "[NEW] Device C4:7F:0E:11:22:33 vLinker MC-IOS\n[NEW] Device AC:80:0A:44:55:66 WH-1000XM4")
    elif args[0] == "info":
        d = DEVICES.get(args[1])
        if d is None:
            print(f"Device {args[1]} not available")
            return 1
        paired = "yes" if args[1] in st["paired"] else "no"
        print(f"Device {args[1]} (public)\n\tName: {d['name']}\n\tAlias: {d['name']}\n\tPaired: {paired}\n"
              f"\tTrusted: {paired}\n\tConnected: no\n\t{d['info']}")
    elif args[0] == "remove":
        if args[1] in st["paired"]:
            st["paired"].remove(args[1])
            save(st)
        print("Device has been removed")
    elif args[0] == "trust":
        print(f"Changing {args[1]} trust succeeded")
    return 0


def interactive(st):
    out = sys.stdout
    pairing = None
    for line in sys.stdin:
        cmd = line.strip().split()
        if not cmd:
            continue
        if pairing is not None:                 # the PIN we asked for
            if cmd[0] == st["pin"]:
                st["paired"].append(pairing)
                save(st)
                out.write(f"[CHG] Device {pairing} Paired: yes\nPairing successful\n")
            else:
                out.write("Failed to pair: org.bluez.Error.AuthenticationFailed\n")
            pairing = None
        elif cmd[0] == "agent":
            out.write("Agent registered\n")
        elif cmd[0] == "default-agent":
            out.write("Default agent request successful\n")
        elif cmd[0] == "pair":
            if cmd[1] not in DEVICES:
                out.write(f"Device {cmd[1]} not available\n")
            else:
                out.write(f"Attempting to pair with {cmd[1]}\n\x1b[0;94m[agent]\x1b[0m Enter PIN code: ")
                pairing = cmd[1]
        elif cmd[0] == "trust":
            out.write(f"Changing {cmd[1]} trust succeeded\n")
        elif cmd[0] == "quit":
            return 0
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
