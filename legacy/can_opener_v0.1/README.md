# CAN Opener — J1939 Field Diagnostic Tool

Touch-friendly J1939/CAN diagnostic app for Raspberry Pi + USB2CAN dongle.

## Setup

```bash
# 1. Install dependency
pip install python-can --break-system-packages

# 2. Allow sudo ip link without password (add to /etc/sudoers.d/can):
# your_user ALL=(ALL) NOPASSWD: /sbin/ip

# 3. Run
cd can_opener
python main.py
```

## Interface bring-up (manual)
```bash
chmod +x can_up.sh
./can_up.sh can0 250000
```

## Project structure
```
can_opener/
├── main.py              # App shell, status bar, tab routing
├── core/
│   ├── can_manager.py   # CAN interface, reader thread
│   ├── j1939_defs.py    # PGN/SPN/FMI tables, DM1 parser
│   ├── logger.py        # CSV log manager
│   ├── settings.py      # JSON settings persistence
│   └── theme.py         # Colors, fonts, shared widgets
├── tabs/
│   ├── config_tab.py    # Interface config, saved profiles
│   ├── raw_tab.py       # Live frame stream, node filter, logging
│   ├── codes_tab.py     # DM1 fault collection, SPN/FMI decode
│   ├── pids_tab.py      # Live SPN sensor view
│   ├── system_overlay.py # System settings (status bar tap)
│   └── tetris.py        # 🎮 (long-press clock to find)
├── can_up.sh            # Interface bring-up helper
└── requirements.txt
```

## Easter egg
Long-press the clock in the status bar for 2 seconds.

## Porting to final CAN Opener hardware (CM4 + MCP2518FD)
Change one line in `core/can_manager.py` `_open_bus()`:
```python
# From:
self._bus = can.interface.Bus(channel=iface, interface="socketcan", ...)
# To (MCP2518FD via SPI):
self._bus = can.interface.Bus(channel="can0", interface="socketcan", ...)
# (The kernel driver exposes MCP2518FD as a standard socketcan interface)
```
Everything else is unchanged.
