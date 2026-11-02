# Custom board: groundwork

Nothing is designed here yet. This folder is where the KiCad project will live, and this page is
the starting point for that work: what's decided, what the bring-up must measure first, the rules
the layout has to follow, and the checks before ordering. Electrical decisions and pinouts are in
[docs/ELECTRONICS.md](../docs/ELECTRONICS.md) (the source of truth); enclosure and form factor are
in [docs/DESIGN.md](../docs/DESIGN.md).

Status markers as in ELECTRONICS.md: **[D]** decided, **[P]** proposed, **[TBD]** open.

## 1. Order of work

1. **Finish the adapter-board prototype first** (ELECTRONICS.md stages 0-8) and fill in its
   section 14 table (SPI clock, crystal caps, compensation values, SYNC jitter, current per stage,
   temperatures). Those numbers are layout requirements; a board drawn before them is a guess.
2. **Rev A: a Pi 5 add-on board** [P]. Same 40-pin header as the prototype, so the software
   doesn't change. It carries what the hand-wired build proved: both CAN stacks, the two Picos
   (as soldered-down Pico modules: their castellated edges make that easy), the probe front end,
   the test source, the low-voltage header, the DB9 and the probe jacks. Low-speed signals only,
   two or four layers, hand-solderable.
3. **Rev B: the CM5 carrier** [P]. The CM5's connectors carry PCIe, USB 3, DSI and Ethernet: fast
   differential pairs with impedance and length rules. Leave it until rev A works, and start it
   from the Raspberry Pi CM5 IO board's published KiCad design rather than a blank sheet.
4. The dual-PCB sandwich, bumper and pogo-pin back face (DESIGN.md) apply to rev B. Rev A can sit
   in a printed test enclosure.

Order five of each revision (DESIGN.md): there will be something to fix.

## 2. Tools and files

* **KiCad** (current stable release). Commit the `.kicad_pro`, `.kicad_sch`, `.kicad_pcb` and
  project library files; not backups, caches or `fp-info-cache` (see `.gitignore`).
* Planned layout:

  ```
  hardware/
    README.md               this page
    rev-a/                  the KiCad project for the Pi 5 add-on board
    lib/                    project symbols, footprints, 3D models (only parts KiCad's libraries lack)
    fab/rev-a-1/            gerbers, drill, BOM, placement exactly as ordered, one folder per order
  ```

* **Licence [TBD]:** the repo is GPL-2.0-or-later. Hardware designs are usually released under
  CERN-OHL-S-2.0 (the hardware counterpart of the GPL). Decide before the first board is pushed,
  and say which applies to `hardware/` in the README's License section.
* The repo is public: no OEM dealer-tool data here either (CLAUDE.md).

## 3. Schematic sheets (one per ELECTRONICS.md section)

| Sheet | Contents | ELECTRONICS.md |
|---|---|---|
| Power | 5 V / 3V3 rails, bulk caps, power entry, later battery/charging | 10 |
| Pi header | 40-pin header, HAT ID EEPROM on GPIO0/1 [P] | 5.1 |
| CAN A, CAN B | MCP2518FD, crystal, TCAN1051V, termination switch, split termination, unpopulated TVS footprint | 5.2-5.5, 7, D5, D10 |
| Meter Pico | Pico 2 module, I2C0 parts (MCP4728, ADS1115, INA226), SYNC out | 5.6 |
| Logic Pico | Pico module, comparator outputs in, header inputs | 5.6 |
| Probe channel (x4) | Attenuators A and B, SMAJ60A, comparator: one hierarchical sheet used four times | 6.1, 6.2 |
| Ohms | PTC, TS5A3153, REF3025, clamp | 6.3 |
| Test source | Load switch, shunt, Schottky, pull-up switches | 6.7 |
| COM and CAN monitors | COM PTC, CAN-H/L/GND dividers and clamps | 6.4, 6.6 |
| Logic header | 2.2 k + BAT54S per pin | 6.5 |
| ADS8584S | ADC, reference and supply decoupling, RANGE | 5.10 |
| ESP32 | Module, EN/IO0, antenna keep-out | 5.9 |
| Connectors | DB9, probe jacks, USB-C, test points | 5.5, 12 |

**Net names** follow ELECTRONICS.md so the schematic, the docs and the code match: `CANA_H`,
`CANA_L`, `CAN_GND`, `PROBE1`-`PROBE4`, `COM`, `SRC`, `SYNC`, `MCP_A_INT`, `MCP_B_INT`, `I2C0_SDA`
... Probe nets keep their number everywhere (`PROBE1_ATT_A`, `PROBE1_ATT_B`, `PROBE1_LOGIC`).

## 4. Layout rules to carry over

From ELECTRONICS.md section 11 (to be replaced with measured values from section 14):

* **Crystal:** caps within a few mm of OSC1/OSC2, tight loop, ground pour under it, nothing
  routed underneath.
* **Decoupling:** 0.1 uF at every supply pin with its via straight to ground; 10 uF per rail.
* **Attenuator nodes (9 Mohm strings):** the most sensitive nets on the board. Short, guard ring,
  keep-out from digital lines, and slots for creepage. Stray capacitance there breaks the probe
  compensation, so keep each channel's layout identical (that's why the probe channel is one
  sheet used four times).
* **Spacing for 60 V working [P]:** probe tips, COM and the CAN pins see up to about 60 V. Keep at
  least 1 mm between those nets and everything else on outer layers; confirm against IPC-2221
  for the chosen coating before ordering.
* **CAN:** transceiver next to the DB9, controller next to the transceiver, CANH/CANL routed as a
  pair away from SPI.
* **SPI:** short, ground alongside, footprints for 22-33 ohm series resistors on SCK.
* **Analog vs digital:** one solid ground plane (ELECTRONICS.md 10: everything shares one ground),
  with analog parts grouped over their own area of it rather than a split plane.
* **ESP32 antenna:** keep-out zone with no copper, over the board edge if possible.
* **Repairability (DESIGN.md):** 0-ohm links on SPI and CAN lines, module-header footprints next to
  bare ICs, test points on every rail and bus, silkscreen labels on all of them.

## 5. Before ordering a board

- [ ] ERC and DRC clean, with every waiver written down.
- [ ] Each footprint printed 1:1 and checked against the real part (pin 1, pitch, body size).
- [ ] Every pin in ELECTRONICS.md 5.1 / 5.6 matches the schematic, and the software
      (`deploy/raspberry-pi/config-hardware.txt`, `config-controls.txt`, `firmware/meter-pico/config.py`).
- [ ] Polarity marks for diodes, TVS, electrolytics; pin-1 marks for every IC.
- [ ] Test points on 5 V, 3V3, GND, SYNC, each CAN pair, each probe node, I2C and SPI.
- [ ] Mounting holes match the enclosure model.
- [ ] The fab's capabilities (minimum track / gap, via size, layer count) in the DRC rules.
- [ ] BOM with manufacturer part numbers and stock checked; spares for anything that's hard to rework.
- [ ] The protection policy (ELECTRONICS.md section 2) walked through on the schematic: each row,
      which part handles it.

## 6. For the PCB conversation

Bring: ELECTRONICS.md with its section 14 measurements filled in, the datasheets of every chosen
part, the enclosure dimensions and connector positions, and this page. The open questions that
touch the board are in ELECTRONICS.md section 15 (comparator part, SRC jack, charging while
connected) and DESIGN.md's open questions (the pogo-pad pinout).
