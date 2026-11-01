# Meter Pico firmware (Pico 2, RP2350)

The custom firmware on the meter Pico (docs/ELECTRONICS.md, D2): probe voltages, CAN-H / CAN-L /
CAN GND monitors, comparator thresholds, the ohms channel, the component test source, and the
SYNC pulse that puts all of it on the Pi's clock.

**Status:** a MicroPython sketch for stage 2b onward. Not run on hardware yet. Every register
value marked `[verify]` in `parts.py` wants a datasheet and bench check.

## Why MicroPython first

* The bring-up stages (2b, 3, 7, 7b) are I2C parts, GPIO and 10-50 readings a second. MicroPython
  does that comfortably, and it's Python: edit a file, copy it over, reset, no toolchain.
* It finds what's fitted at boot (an I2C scan, a test conversion on the ADS8584S) and lists it in
  the hello, so the Pi's installed options (`core/hardware.py`) can check the meter's own parts.
* **Waveform streaming will outgrow it.** 100-330 kSPS on four channels (ELECTRONICS.md section 8)
  needs PIO + DMA feeding USB at about 800 KB/s. That is the point to move to the C SDK
  (pico-sdk, TinyUSB with two CDC interfaces or one framed binary stream). The line protocol
  stays the same, so nothing on the Pi changes.

## Files

| File | |
|---|---|
| `config.py` | Pins, I2C addresses, divider ratios, calibration. The pin map is ELECTRONICS.md 5.6 |
| `parts.py` | MCP4728 (thresholds), ADS1115 (ohms node), INA226 (source current), ADS8584S (bit-banged, meter rate only) |
| `main.py` | Hello, readings, SYNC, commands, the safety pre-checks |

## Flash and run

```bash
# Hold BOOTSEL, plug in, copy the MicroPython UF2 for the Pico 2 (RP2350) onto the drive. Then:
pip install mpremote
mpremote cp config.py parts.py main.py :
mpremote reset
mpremote                       # watch the JSON lines; Ctrl-] to leave
canopener-cli hardware --set meter_pico=yes --set sync=yes -v
```

It shows up as `/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_<serial>-if00`, which the Pi
recognises as the meter Pico. With both Picos plugged in, set the port exactly:
`canopener-cli hardware --port meter_pico=/dev/serial/by-id/...`.

## Protocol

The meter line protocol is documented in `canopener/core/meters.py`. This firmware sends:

```
{"hello": "canopener-meter", "name": "Meter Pico", "options": ["dac", "ohms", "src", "ads8584s"], "channels": [...]}
{"ch": "can_h", "v": 2.512, "t_us": 81234567}
{"sync": 812, "t_us": 81200003}
{"status": "refused", "text": "circuit is powered (12.4 V on probe 4)"}
```

and accepts `hello`, `rate`, `threshold`, `mode` (ohms), `src` and `pullup` commands. The Pi's
front-ends confirm `src`, `pullup` and ohms with the user before sending them (CLAUDE.md: anything
that puts something on a wire asks first), and the firmware refuses them on a powered circuit.

## SYNC

A 10 Hz hardware timer raises GP15 and records the time; the main loop reports the pulse and
drops the pin. Only the rising edge matters: the Pi stamps it in the kernel (pps-gpio on GPIO21)
and pairs it with the `sync` line (`core/sync.py`), and the logic Pico records it on D2. Change
`SYNC_PERIOD_MS` and `core/sync.PERIOD_S` together.

## Not done yet

* Waveform (burst and streaming) capture: C SDK, PIO, DMA (stage 8).
* Calibration storage (per-channel ratio from a reference voltage), kept in a file on the Pico.
* COM PTC fault sense on GP7 [P].
* CAN monitor divider values and the CAN GND bias network (ELECTRONICS.md 6.6) from the bench.
