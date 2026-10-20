# Legacy code (frozen, for reference)

Nothing here is imported by the `canopener` package or covered by the tests.

## `j1939_codes.py`

The single-file Tkinter code reader written on a jobsite for a Genie GTH-1056 (Deutz EMR4).
It reads DM1/DM2/DM4/DM5/DM6/DM12/DM23 and ID PGNs, with transport-protocol support. Its
protocol logic was the starting point for `canopener/j1939` and `canopener/core`. Still
handy as a zero-install script: `python3 j1939_codes.py` (needs `python3-tk`).

## `can_opener_v0.1/`

The first attempt at an on-device app, run on a Raspberry Pi 3B+ with a TFT: Tkinter,
800x480, python-can. What carried forward:

* **UI layout for small touch screens**: left icon tab bar, a dark status strip (clock, CAN
  status pill, REC indicator, frame rate), big flat buttons, touch confirm dialogs, drag
  scrolling, a "system" overlay (brightness, units, reboot/shutdown). This is the reference
  for the planned on-device UI (see `docs/ARCHITECTURE.md`).
* **Pi deployment**: launcher script, systemd unit, desktop entry, sudoers for `ip link`.
  Reworked into `deploy/raspberry-pi/` (tighter sudoers, no shell as root).
* **Field features**: copy logs to a USB stick, CSV logs, per-ECU filter, auto bitrate.
  Now `core/storage.py`, the Codes tab ECU filter, and `canopener-cli setup --bitrate auto`.
  Auto-detect now probes in listen-only mode: v0.1 tried each bitrate in normal mode, which
  puts error frames on a live machine bus.

**Do not copy J1939 data from v0.1.** Its tables and decoder have errors that its own
simulator hid, because the simulator used the same wrong numbers:

| v0.1 | Correct (J1939-71 / -73) |
|---|---|
| ET1 engine temperatures at PGN 0xFEE6 | ET1 is 0xFEEE (65262); 0xFEE6 is Time/Date |
| EFL/P1 oil pressure at 0xFEE9 | EFL/P1 is 0xFEEF (65263); 0xFEE9 is fuel consumption |
| VEP1 voltages at 0xFEF8 | VEP1 is 0xFEF7 (65271) |
| IC1 boost/intake at 0xFEDF; 0xFEF5 "TURBO", 0xFEF6 "AIR2" | IC1 is 0xFEF6 (65270); 0xFEF5 is AMB ambient conditions |
| 0xFEDA labelled DM11, 0xFEEB/0xFEEC labelled EFL_P3/P4 | 0xFEDA is Software ID, 0xFEEB Component ID, 0xFEEC VIN; DM11 is 0xFED3 |
| EEC1 SPN 512/513 at bytes 0/1 | Driver demand (512) is byte 2, actual torque (513) byte 3 (1-based) |
| EEC2 accelerator pedal (SPN 91) at byte 0 | Byte 2 (1-based); byte 1 is switch states |
| Source-address table (0x0B instrument cluster, 0x17 cab controller, ...) | 0x0B brakes system controller, 0x17 instrument cluster #1, 0x31 cab controller |
| A lamp counts as lit for status 1, 2 or 3 | Only 01 is "on"; 11 means not available, so 0xFF lamp bytes showed every lamp lit |
| The standard "no faults" DM1 (00 00 00 00 after the lamps) decoded as a fault "SPN 0 FMI 0" | That pattern means no active DTCs |
| No transport protocol | DM1 with 2+ codes, DM2, IDs etc. arrive as BAM / RTS-CTS multi-packet messages and were never decoded |

So on a real machine v0.1 would have shown live values from the wrong PGNs (or nothing),
and missed any multi-code DM1.
