# ESP32 bridge (stage 6)

The ESP32-WROOM-32UE inside the handheld: an ESP-NOW receiver for wireless satellite meters now,
a Bluetooth gateway later. It prints what it hears to the Pi in the meter line protocol, so on the
Pi it is just another serial meter (installed option `esp32`, port `/dev/ttyAMA0`).

**Status:** a sketch. Not compiled or run yet.

## Wiring (docs/ELECTRONICS.md 5.9, D8)

| ESP32 | Pi | |
|---|---|---|
| TX0 (GPIO1) | GPIO15 (RXD, pin 10) | 3.3 V both sides, no level shifter |
| RX0 (GPIO3) | GPIO14 (TXD, pin 8) | |
| GND | GND | |
| 3V3 | 3V3 | The module peaks around 500 mA on WiFi transmit: check the Pi's 3V3 budget, or give it its own regulator |
| EN, IO0 | pull-ups + two buttons (or a USB-UART bridge) | For flashing |

Pi side: on a Pi 5, `dtparam=uart0=on` (its `/dev/serial0` is the separate debug connector, so
use `/dev/ttyAMA0`). On a 3B+, `enable_uart=1` and `dtoverlay=disable-bt`, so the GPIO UART isn't
the Bluetooth one. Either way, turn the serial console off (raspi-config, Interface Options,
Serial Port: login shell No, hardware Yes). See `deploy/raspberry-pi/config-hardware.txt`.

**Open question:** wiring EN and IO0 to two free Pi GPIOs (GPIO19 is free, one more is needed)
would let the Pi flash the ESP32 itself with `esptool`, the "Unconfigured, Flash?" idea from
DESIGN.md. Decide before the custom board.

## Build and flash

Arduino IDE or `arduino-cli`, ESP32 core 3.x, board "ESP32 Dev Module". First flash over a USB-UART
bridge on TX0/RX0 with IO0 held low through reset.

## Satellite packet, version 1

ESP-NOW payload from a satellite (ESP32 + ADS1115 + LiPo, DESIGN.md). Little-endian, packed:

| Bytes | Field | |
|---|---|---|
| 0-1 | magic | `'C' 'O'` |
| 2 | version | 1 |
| 3 | count | readings in this packet, 1-8 |
| 4-7 | seq | +1 per packet; a gap = lost packets |
| 8-11 | t_ms | the satellite's own clock |
| 12 + 9n | id[4], value (float32), flag (u8) | flag: 0 ok, 1 over range, 2 low battery; NaN = no reading |

The bridge names each channel `<last 3 MAC bytes>_<id>` (`a1b2c3_v1`), so two satellites never
collide and a satellite keeps its channel ids across restarts. Satellite firmware isn't written yet.

Timing: ESP-NOW latency is 1-2 ms, and the Pi stamps these lines when they arrive, which is good
enough for meter readings. Satellites have no SYNC wire.
