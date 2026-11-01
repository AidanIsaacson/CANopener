# Firmware for the handheld's coprocessors

| Folder | Board | Language | Stage | Status |
|---|---|---|---|---|
| [`meter-pico/`](meter-pico/) | Pico 2 (RP2350): probes, thresholds, ohms, test source, CAN monitors, SYNC | MicroPython (C SDK later for waveform streaming) | 2b-8 | Sketch |
| [`logic-pico/`](logic-pico/) | Pico (RP2040): logic analyzer | Stock sigrok-pico, nothing custom | 2a | Instructions |
| [`esp32-bridge/`](esp32-bridge/) | ESP32-WROOM-32UE: ESP-NOW satellites, later Bluetooth | Arduino (ESP32 core 3.x) | 6 | Sketch, not compiled |

All of them talk to the Pi in the **meter line protocol** (JSON lines, documented in
`canopener/core/meters.py`), so on the Pi each is just a measurement source, started only when its
installed option says it's fitted (`canopener/core/hardware.py`). Wiring and the reasons for it:
[docs/ELECTRONICS.md](../docs/ELECTRONICS.md); what each part's software does:
ELECTRONICS.md section 18.

Firmware here is GPL-2.0-or-later like the rest of the repo. It never runs on the Pi and isn't
imported by `canopener/`.
