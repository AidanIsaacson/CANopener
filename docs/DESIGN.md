# CAN OPENER
### Field Diagnostic Cyberdeck — Build Notes
*Architecture & design decisions as of April 2026, brought up to the electronics bring-up decisions in November 2026.*

> **Where the detail lives.** Pinouts, protection, the probe front end, timing, stages and the BOM:
> [ELECTRONICS.md](ELECTRONICS.md) (decisions D1-D13 are referred to below). Code for each part:
> ELECTRONICS.md section 18. The custom board: [hardware/README.md](../hardware/README.md). The
> handheld UI: [DEVICE.md](DEVICE.md).
 
---
 
## Concept & Motivation
 
CAN Opener is a purpose-built rugged handheld diagnostic platform, primarily targeting industrial forklift CAN/J1939 systems but designed from the ground up for general-purpose field diagnostics. The goal is a single instrument that replaces a laptop, a DMM, a logic analyzer, and a basic oscilloscope in a shop or warehouse environment.
 
Justified over a phone/laptop by:
- Native hardware interfaces (CAN, SPI, I2C, GPIO) as first-class citizens
- Ruggedized for warehouse/shop environments — glove-friendly, drop-tolerant
- Synchronized multi-domain capture: CAN frames, logic channels, and analog voltages on a shared timebase
- Full control over the software stack
- Modular expansion via magnetic peripheral modules
---
 
## Form Factor
 
Roughly T-shaped handheld:
- **Crossbar:** 7" DSI touchscreen (toughened glass if available), navigation encoder, hardware buttons
- **Grip/stem:** Battery pack, BMS, connectors — kept slim
- **Compute board** mounts behind the screen on the crossbar, not in the grip
- **Connectors** (CAN harness, probe inputs) exit from the top edge — cables hang away from the hands during use
- **Back face** kept deliberately clear for pogo-pin contact with magnetic peripheral modules
One-handed hold supported; most-used functions (start/stop log, save snapshot) reachable by thumb without shifting grip.
 
---
 
## Enclosure
 
**Chassis: Dual-PCB sandwich**
- Front PCB and back PCB form the primary structural frame — electronics are protected between them
- Significantly more rigid than a single-board design; provides precision mounting surfaces for all hardware
- M3 standoffs between boards for primary structural load; 3D printed parts with threaded inserts for geometry-specific mounting and alignment
- Printed shells (Bambu P1S) handle ergonomics and cosmetics only; structural load is carried by the PCB sandwich
**TPU bumper**
- Runs around the full outer edge, pronounced enough to protect the screen face from gravel contact or a standing-height drop
- Corner sections wrap onto both front and back face — corners are where drop energy concentrates
- Designed in from day one; not retrofitted
**Thermal**
- CM5 will run warm in an enclosed sandwich (warmer than a CM4) — vent slots or a thermally conductive wall section required on the back PCB
- Do not fully seal
**Module footprint**
- Back face expansion area is larger than the main unit profile — peripheral modules can be as thick as needed
---
 
## ON-DECK Hardware
 
### Core Compute
 
**Bring-up: Raspberry Pi 3B+** (on hand)
- Stage 1 of the hand-wired prototype (CAN stack A), headless over SSH. Same 40-pin header as the Pi 5, so wiring carries over. Any SPI TFT hat comes off: it sits on SPI0 with the CAN controller
- No RTC; time jumps when NTP arrives after boot

**Prototype: Raspberry Pi 5** + 10.1" Touch Display 2 (arriving)
- Development and software validation platform for the adapter-board prototype ([ELECTRONICS.md](ELECTRONICS.md))
- Runs full Linux, boots straight into the diagnostic app (`canopener-device`)
- Storage: 64–128GB microSD, SanDisk or Samsung Pro Endurance (rated for sustained writes)
- 5 V / 5 A supply (or it limits USB current, and both Picos are on USB) and a cooler
- RTC on board

**Final build: Compute Module 5 (CM5)**
- Target platform for the finished instrument (was CM4); same software as the Pi 5
- eMMC storage, cleaner layout, no dangling ports; custom carrier board exposes exactly what's needed
- **RAM: 2GB** is still expected to be enough — Pi OS headless base runs ~150–200MB at idle; full diagnostic stack with sigrok and active logging estimated 300–500MB under load. Confirm on the Pi 5 prototype before choosing
- Built-in WiFi/Bluetooth is optional on CM5; wireless is still the internal ESP32's job (see Wireless & Bluetooth)
- The carrier is a hard board (PCIe, USB 3, DSI pairs): it comes after a Pi 5 add-on board has proven the rest (hardware/README.md)
### CAN Interface
 
**Two CAN stacks [D]**, each an MCP2518FD + TCAN1051V-Q1, so two buses can be watched at once:
- **Stack A:** SPI0 CE0, INT on GPIO25 → `can0` (stage 1)
- **Stack B:** SPI0 CE1, INT on GPIO20 → `can1` (stage 5; D7). The software needs one session per bus for this (ARCHITECTURE.md, readiness item 4)

**Controller: MCP2518FD** (bare IC, soldered directly to PCB)
- 2KB RAM buffer eliminates dropped frames under Linux scheduler load; missing a fault frame on a diagnostic tool is unacceptable
- Supports CAN FD for future-proofing with automotive and newer industrial networks
- Runs on 3.3V SPI to Pi; requires stable external crystal: **20 MHz on the prototype, 40 MHz on the custom board** (D10). Silicon errata cap SPI at 0.85 x SYSCLK/2, about 8.5 MHz at 20 MHz; the Linux driver enforces it
- Kernel overlay: `dtoverlay=mcp251xfd,spi0-0,oscillator=20000000,interrupt=25` — appears as standard `can0`, existing Python logger works as-is (all stages: `deploy/raspberry-pi/config-hardware.txt`)
**Transceiver: TCAN1051V-Q1** (bare IC — no module available; hand-solder with hot air)
- Split VCC/VIO architecture: VCC on 5V for full-strength industrial bus drive, VIO on 3.3V for direct logic connection to MCP2518FD; no level shifters needed
- Automotive-grade (Q1), ±58V fault tolerance on CAN-H/L — survives accidental battery contact on a 48V system. Custom board: **TCAN1051HV-Q1** (±70 V) if pin- and price-compatible, for 48 V packs reading ~60 V while charging (D10)
- Built-in thermal shutdown and bus timeout prevents taking down the bus if the Pi crashes mid-transmission
- *On-hand fallback: SN65HVD230 module — usable but lacks ±58V fault tolerance*
**Line protection & conditioning**
- **No external CAN TVS on the prototype** (D5, was PESD1CAN / NUP2105L). The TCAN1051V already has ±15 kV IEC ESD protection on CANH/CANL, and a 24 V TVS would conduct and likely fail short in the in-scope miswire (harness onto 36/48 V battery +), pulling the machine's CAN line to ground. The custom board keeps an unpopulated footprint for a >60 V, low-capacitance part if field use shows a need
- **Split termination** — two 60Ω resistors in series with a 4.7nF cap from center tap to ground; acts as a low-pass RF filter for motor controller noise
- **Termination switch** — physical switch near DB9 connector replaces previous jumper design; off when sniffing an intact bus, on when bench testing a single ECU. Termination isn't miswire-tolerant (48 V across 120 Ω is ~19 W): a burnt resistor is the accepted bench failure
- Twisted pair harness provides common-mode noise rejection — no ferrite beads needed on the CAN lines
**Repairability / fallback layout**
- Primary circuit uses bare MCP2518FD and TCAN1051V-Q1 ICs for clean low-profile assembly
- Adjacent 0.1" female header footprints matching standard MCP2515/transceiver breakout module pinouts populated on PCB
- SPI and CAN lines routed through 0-ohm resistor jumpers (removable with soldering iron) — field repair path: remove jumpers, slot in replacement module, back running
- DB9 connector on top edge for harness connection: **male on the device** (CiA 303-1: pin 2 CAN_L, 7 CAN_H, 3 CAN_GND), so off-the-shelf cables and OBD-II adapters fit (D9)
### Pico Coprocessors: two Picos [D2]
One Pico can't run stock sigrok and custom meter firmware at once, so there are two. Both talk to the Pi over USB (virtual serial) and are powered by it.

- **Logic Pico: original Pico (RP2040), stock sigrok-pico.** Nothing custom runs on it, so sigrok/PulseView's decoders work unchanged. Driven from the Pi through `sigrok-cli` (built from source: Pi OS's 0.7.2 predates the Pico driver).
- **Meter Pico: Pico 2 (RP2350), CANopener firmware.** ADS8584S, comparator thresholds, the ohms channel, the component test source, the CAN monitors and the SYNC pulse; twice the RAM of the RP2040 for burst captures. MicroPython for bring-up, C SDK once waveform streaming needs PIO + DMA (`firmware/meter-pico/`).
- Both list in the Pi's **installed options** (see Subsystem Fault Management), so a missing or unflashed Pico is reported, not waited on.

#### Logic Analyzer (logic Pico — PIO)
- **Shared probes [D11]:** the four meter probes double as logic channels. Each probe has a second x10 attenuator into a fast comparator whose threshold comes from a DAC on the meter Pico; the comparator outputs feed logic channels D3-D6. One probe gives the waveform and the decoded bits of the same wire at the same instant (CAN-H on the meter, its frames from sigrok)
- **Auto-threshold:** the meter Pico watches each probe's min/max on the ADC and sets the threshold halfway, with presets for 3.3 / 5 / 12 / 24 / 48 V logic. Thresholds reach ~33 V at the tip
- **Low-voltage logic header:** 8 more channels (D7-D14) for board-level SPI/I2C/UART, 2.2 kΩ series + Schottky clamp per pin, labelled "3.3/5 V only"
- D2 records SYNC in every capture
- Speed per the sigrok-pico guide: up to 120 Msps on 1-4 channels, 60 Msps or less for 8+; the probe path (attenuator + comparator) is expected to reach the low MHz (CAN, LIN, UART, PWM), to be measured
- Protocol decoders via sigrok: CAN, I2C, SPI, UART, PWM, LIN — covers all common forklift/automotive bus work
- Triggered capture on edge or pattern

#### Analog Capture / Waveform Viewer (meter Pico — ADC + ADS8584S)
**Stage 3, first analog:** an ADS1115 on the Pi's own I2C (kernel IIO driver, `dtoverlay=ads1115`), through a ~1 MΩ divider and a clamp per input, gives slow probe voltages before the meter Pico board exists. Never bare probes into it: it's on the Pi's I2C.

- **3x internal channels** (Pico ADC GP26/27/28) — hardwired on PCB to CAN-H, CAN-L, and CAN GND for passive bus voltage monitoring; lighter divider/clamp sized for 0–3.5V range
- **4x external probe channels** via **ADS8584S** (SPI to Pico) — for field voltage measurements
- Practical use: ground float, voltage sag, supply droop, sensor signal shape, dead spots, crank sensor dropout
- Bandwidth: up to 330kSPS per channel — captures transient faults in the millisecond range. The ADC's own anti-alias filter cuts off at 24 kHz, so fast edges are the logic path's job
- **Rate scales with channels in use [D4]:** all four always convert together; only enabled channels are sent, within a ~800 KB/s USB budget (1 ch at 330 kSPS ... 4 ch at 100 kSPS). Full rate on all four is a triggered burst into the Pico 2's RAM (~0.15 s). Meter mode uses the ADC's oversampling for 10-50 low-noise readings a second
- Stäubli banana jacks for probe connections
**ADS8584S specifics**
- 4-channel, 16-bit, simultaneous-sampling SAR ADC — all channels sampled at the same instant, no inter-channel skew
- ±10V or ±5V bipolar input range on single 5V supply
- 1MΩ input impedance (0.85-1.15 MΩ, so each channel is calibrated) with integrated PGA and ADC driver — no external op-amp buffer required
- Built-in input clamp handles up to ±15V before activation; external protection still recommended for hard industrial transients
- SPI interface to Pico; needs 5V supply rail
**Analog front-end signal chain (per external probe channel)** [D6, D11; ELECTRONICS.md section 6]
`Probe tip → SMAJ60A to ground → 9 MΩ ∥ Ccomp (attenuator A) → ADS8584S (1 MΩ internal)`, and in parallel `9 MΩ ∥ Ccomp → 1 MΩ ∥ Cshunt (attenuator B) → comparator → logic Pico`

- **x10 compensated attenuator** — 9 MΩ (three 3 MΩ 1206 parts in series, for voltage rating and creepage) against the ADC's own 1 MΩ: ±10 V range reads about **±100 V full scale**, margin above the 60 V working limit (D1). Compensated like a x10 scope probe (Ccomp × 9M = Cshunt × 1M), trimmed with a square wave
- **The tip sees ~5 MΩ** (both attenuators in parallel), close to a DMM; the April design's 50 k/10 k divider loaded a circuit with 60 kΩ
- **Current limiting is built in:** 60 V through 9 MΩ is ~7 µA, a 1 kV spike ~110 µA, far under the ADC clamp's 10 mA. That's why the PTC is gone from the voltage path (the ordered MF-MSMF parts were also only rated 15 V)
- **SMAJ60A** at the jack conducts only above ~67 V: invisible in 12-48 V work, catches surges for both strings
- ADS8584S internal clamp and driver handle the rest — no op-amp buffer stage needed
*Note: Satellite DMM modules use ADS1115 (I2C, simpler) — the ADS8584S is on-deck only.*
 
#### Resistance Measurement (One Channel)
 
One external analog channel is extended with a simple excitation circuit to support resistance measurement. Target range: near-zero (continuity) through ~20kΩ — covers wire shorts, sensor reference checks, coil resistance, and known reference values (e.g. "this pin should be 20kΩ to ground").
 
**Excitation circuit (added to one probe channel, not probe 4)**
`REF3025 (2.5V precision reference) → 10kΩ 0.1% series resistor → TS5A3153 analog switch → clamp (~5 V) → PTC (rated ≥ 60 V) → probe tip`

The excitation path touches the tip directly and the switch is a ~5 V part, so it's the one probe path that keeps a PTC (≥ 60 V rated) plus a clamp. The ohms node is read directly by a separate ADS1115 on the meter Pico (±4.096 V range, not through the x10 attenuator, which would leave ~800 counts for the 2.5 V span).
 
- **REF3025** — 2.5V precision voltage reference (SOT-23); low quiescent current, stable over temperature, safe excitation voltage that won't back-drive partially-powered circuits
- **10kΩ 0.1% series resistor** — known reference; Ohm's law derives unknown resistance from measured midpoint voltage
- **TS5A3153 analog switch** — Pico GPIO disconnects excitation when channel is in voltage mode; 2.5V must not sit on a probe tip measuring a live circuit
- Fits in ~1cm² on PCB
**Voltage-on-probe behavior**
If excitation is active and external voltage is detected on the probe (e.g. technician clips onto a live circuit by mistake), the resistance calculation produces garbage — identical to a DMM reading OL or 0.0. CAN Opener detects the condition and displays a warning with the actual measured voltage shown below it. The technician knows immediately why the reading is invalid, and the voltage readout is often useful diagnostic information in its own right. Detection falls out of the same probe's voltage reading; the firmware refuses ohms mode on a powered circuit, and the PTC and clamp protect the hardware either way.

#### Component Test Source (probe 4 "SRC") [D13, proposed]
A current-limited, metered **5 V source on probe 4**, plus switchable 4.7 kΩ pull-ups on probes 1-2, for powering a sensor off the machine: Hall quadrature encoders, Hall speed sensors, ratiometric pots, switches.
- Load switch (~100 mA limit, thermal shutdown, fault flag), 0.5 Ω shunt on an INA226, 100 V Schottky to the tip so it survives touching 12-48 V with the source on or off
- Supply current is a diagnostic in itself (shorted sensor at the limit, open near zero, 7/14 mA current-mode speed sensors become readable)
- **Component Test mode** (under TEST): pick a template, see the wiring, pre-check that nothing is powered, enable, then live count / direction / frequency / duty / A-B phase or ratiometric %, pass/fail to the capture log
- Supplies of 12 V and up stay off-deck: an accessory that gates the machine's own B+ (ELECTRONICS.md footnote 1)

#### CAN Monitors and Probe COM
- CAN-H, CAN-L and CAN GND into the meter Pico's own ADC through high-value dividers with clamps that survive the ±58 V miswire
- One probe COM jack, tied to instrument ground through a PTC rated ≥ 60 V (hold current above the SRC limit), so COM clipped to B+ while the CAN harness is grounded trips the PTC instead of burning the ground path

### Protection Policy [D12]
Electrical scope is **12-48 V systems, designed to ~60 V working** (D1; 72/80 V trucks out of scope). The goal: a responsible tech can't smoke the expensive parts (Pi/CM5, ADS8584S, display) with a plausible mistake on a 12-48 V machine. Field-facing pins only touch **cheap, replaceable parts** (transceiver, Picos, comparators, resistors); nothing from the field reaches the Pi's GPIO directly. The hardware survives a fault without firmware reacting; firmware's role is user-facing only (warnings, refusing a mode on an unsafe reading). Not a CAT-rated instrument: never mains, and the label and manual say so. The scenario table is ELECTRONICS.md section 2.
 
### Wireless & Bluetooth (Internal ESP32)
 
An ESP32 module lives inside the enclosure as a dedicated wireless subsystem, so wireless doesn't depend on which compute module is fitted. **Prototype: on the Pi's UART** (GPIO14/15, `/dev/ttyAMA0`; D8), as the ESP32-WROOM-32UE has no USB. It speaks the same JSON line protocol as the meter Pico (`firmware/esp32-bridge/`).
 
**ESP32 variant: Full ESP32 (not ESP32-C3)**
The C3 is single-core RISC-V with BLE only. The full ESP32 is dual-core with dual-mode Bluetooth (Classic BR/EDR + BLE). For a field diagnostic tool where the technician doesn't know in advance what Bluetooth-equipped equipment they'll encounter, dual-mode coverage is the correct choice. Classic Bluetooth is still common on older BMS units, industrial adapters, and test equipment.
 
**Functions:**
- **ESP-NOW receiver** — receives readings from wireless DMM satellite modules; pipes data to Pi over the UART. Connectionless, 1–2ms latency, purpose-built for this use case
- **Bluetooth gateway** — pairs with Bluetooth-enabled field equipment: BMS units, newer diagnostic adapters, Bluetooth-capable test equipment (e.g. Fluke meters with Bluetooth output). Readings routed to Pi and surfaced in the diagnostic UI alongside wired inputs
- **Future: WiFi data offload** — log transfer without a physical cable; OTA firmware conduit for Pico and ESP32 itself
### Synchronized Multi-Domain Capture [D3]
- **The meter Pico drives a periodic SYNC pulse** (10 Hz, hardware timer) to Pi GPIO21 and the logic Pico's D2. Reversed from the April plan because the Pi time-stamps inputs far more precisely than it generates outputs
- The Pi's kernel stamps each edge (`pps-gpio`) on the same clock SocketCAN uses; the meter Pico reports each pulse on its own microsecond clock; the logic capture contains the edges. Every pulse corrects crystal drift (tens of ppm, milliseconds within minutes) — `core/sync.py`
- **T+0** is the first pulse after capture start
- CAN frames (decoded, timestamped), logic channels, and all analog channels aligned on one timebase. The limit is CAN frame timestamp latency (interrupt + SPI, likely tens to hundreds of µs), well inside the 2 ms example below
- Enables root-cause correlation: e.g., "ECU ground floated at T+0.147s → CAN error frame at T+0.149s → bus quiet for 0.2s → recovery at T+0.350s"
### Display & UI Hardware
- **7" DSI touchscreen** (Touch Display 2, 720×1280) — design target; toughened glass if available; direct Pi connection, minimal CPU overhead. A 10.1" Touch Display 2 (1200×1920) is arriving for the prototype; it's bigger than the crossbar is designed around
- Touch targets sized for gloved hands (10mm minimum)
- **Alps Alpine RKJXT1F42001 navigation encoder** — 4-way directional (up/down/left/right) + center push + rotary encoder in one component; single-thumb operation with gloves; 17×17mm footprint, through-hole, 50,000 cycle rated
- **Hardware buttons** — fixed functions (start/stop log, snapshot, alerts); tactile, glove-friendly
- USB keyboard port available for terminal/config work; keyboard stored in unit, not built in
---
 
## ON-DECK Software
 
- Boot straight to diagnostic app — no desktop environment
- CAN logger core (`can_logger.py`) is the foundation for the on-device version
- J1939 source address filtering and dropout detection (from field logger work)
- **CAN Node & PID Label Profiles** — on-deck database of known source addresses and PIDs; frames annotated with human-readable labels in the live view (e.g., "Steer Axle Position," "Service Brake Pressure"). Profiles saved per machine make/model and loadable on the fly
- `tmux` for context switching during development phase
- Fault code → manual page lookup (SQLite + pre-rendered PDF images) — planned, low priority
- sigrok/PulseView integration for logic capture decoding
### UI Framework: Pygame
 
Target framework is **Pygame**, running fullscreen on the DSI display with no desktop environment. The UI model is a purpose-built instrument interface — not a desktop application, not a terminal.
 
Layout follows commercial handheld scan tool conventions:
- **Persistent chrome** — header showing device/connection status; footer with context-sensitive soft-key labels mapped to hardware buttons
- **Paged screens** — discrete functional views navigated by the RKJXT1F42001: Live Data, Fault Codes, Active Tests, Capture Log, Settings
- **Navigation model** — rotate encoder to scroll/adjust values; 4-way directional for field/cursor movement; center push to select/confirm; dedicated hardware button to go back or jump home
- Touch targets sized for gloved hands throughout
Pygame is appropriate because: direct framebuffer access, consistent frame rate, no compositor overhead, and manual pixel-level layout is a feature not a bug for a fixed-layout instrument UI that never needs to reflow.
 
### Subsystem Fault Management
 
The Pi/CM5 treats each peripheral (each Pico, each CAN interface, ESP32) as a managed subsystem with a known state. Unexpected state changes surface as actionable dialogue rather than silent failures or hangs.
 
**Examples:**
 
*"Pico not detected — Retry?"*
Pi polls the Pico's USB serial heartbeat. On dropout: offer retry before assuming hardware failure. On successful retry, resume transparently. On repeated failure, flag Pico-dependent features (logic analyzer, analog channels) as unavailable and continue on CAN-only — the tool remains useful.
 
*"Unconfigured Pico detected — Flash?"*
A factory-fresh or blank Pico enumerates differently on USB. Pi detects the condition, offers to flash firmware in-situ: puts Pico into BOOTSEL mode programmatically, copies the UF2, Pico reboots ready. Field-replaceable with zero tools and no laptop.
 
Same pattern applies to CAN interface, ESP32, and future satellite modules. Every subsystem has an expected state; deviations get a human-readable dialogue.

**Installed options.** The build is tiered, so "expected state" starts with whether a part is fitted at all. Each on-deck part is an installed option (`core/hardware.py`): **Not installed** (hidden, never touched — the UI can't poll an ADC that isn't there), **Installed** (offered; missing = the dialogue above, retried) or **Use if found**. "Set all from build stage" fills them in from the bring-up stage; parts behind the meter Pico are confirmed by its own boot-time probe of its I2C/SPI parts. Detection only looks at sysfs and `/dev` entries, and every hardware conversation runs on its own thread with timeouts, so a part that dies mid-session is a red lamp, not a frozen tool. Set on the handheld (System > Installed options) or `canopener-cli hardware`. Details: ELECTRONICS.md section 19.
 
---
 
## Unknown / Proprietary Protocol Coverage
 
Not every system on industrial equipment speaks J1939. Battery BMS units, engine sub-systems, and third-party modules sometimes run proprietary protocols on top of CAN, or use different physical buses entirely. CAN Opener's layered hardware stack provides a systematic approach to identifying and communicating with unknown buses.
 
### Coverage Hierarchy
 
**Tier 1 — Try CAN first**
The MCP2518FD handles standard CAN and CAN FD natively. If the target system puts frames on the bus, CAN Opener will see them. A PC-to-CAN dongle (PCAN, CANable, etc.) is electrically equivalent to the MCP2518FD at the silicon level — if a PCAN dongle can communicate with a module, so can CAN Opener.
 
**Tier 2 — Unknown bus: identify the physical layer**
If the CAN stack sees nothing, drop to the Pico + sigrok. Connect the logic analyzer channels to the suspect lines and capture. sigrok's protocol decoder library covers CAN, LIN, RS-485/Modbus, SPI, I2C, UART, PWM, and more. Physical layer characteristics narrow the field quickly:
- Differential pair, 120Ω termination → likely CAN or RS-485
- Single-ended with a separate clock line → likely SPI or I2C
- Two wires, asymmetric timing, break characters → likely LIN or UART variant
**Tier 3 — Not CAN: Pico handles it, or a satellite module provides the transceiver**
The Pico can bit-bang or PIO a wide range of protocols once the physical layer is identified. RS-485 needs only a cheap MAX485 driver IC; LIN needs a small LIN transceiver. These are strong candidates for dedicated satellite modules on the back-face expansion rather than permanent on-deck hardware.
 
### Frame Replay & Command Injection
 
Where OEM software performs a specific action (forced regen, active output test, fault clear), the underlying operation is a CAN frame or sequence of frames. The bisection workflow (SavvyCAN playback + binary halving of a captured log) isolates the exact CAN ID and payload responsible. That frame can then be saved to a machine profile and sent from CAN Opener on command — no OEM software or proprietary dongle required.
 
J1939 note: command frames targeting engine or transmission ECUs are typically Request PGNs (PGN 0xEA00) asking the ECU to execute or report, not raw data writes. Manufacturer-specific PGNs (0xFF00–0xFFFF range) are where most active test and calibration commands live.
 
### Candidate Satellite Modules
 
Based on field experience with forklift, engine, and BMS systems:
- **LIN transceiver module** — LIN appears on body/comfort systems and some BMS units
- **RS-485 / Modbus module** — some battery chargers and BMS comms use Modbus RTU over RS-485
---
 
## Use Case Example: Synchronized Multi-Domain Diagnosis
 
> *This workflow illustrates what "synchronized multi-domain capture" means in practice. Use it as a reference when designing the live data UI and multi-channel analog view.*
 
**Scenario:** Arrive on site. Customer states there's a fault code.
 
1. **Plug in, scan, query the ECU**
   CAN Opener connects to the machine's CAN bus and pulls active fault codes.
   *Result: SPN 91 FMI 2 — Accelerator Pedal Position, Data Erratic*
2. **Pull the live PID**
   One button on the fault code screen opens the associated sensor PID in the live data view.
   *Result: PID reads within normal range at rest — fault is intermittent, not hard.*
3. **Clip four probes to the sensor**
   Back-probe all four wires on the pedal sensor connector: +5V supply, ground, Channel A signal, Channel B signal. One probe per analog input channel on CAN Opener.
4. **Start synchronized capture**
   Live view shows the SPN 91 PID value and a real-time graph of all four analog channels on a shared timebase. Technician sweeps the pedal through full travel while watching the screen.
5. **Fault identified**
   The PID reads plausibly throughout the sweep. Channel B on the graph shows a dropout mid-travel — the kind of intermittent that only appears during movement and would pass a static DMM check.
**Result:** Bad pedal sensor confirmed in under 15 minutes, with the evidence saved to the capture log.
 
---
 
## PCB Design Notes
 
Groundwork, sheet plan, layout rules and the pre-order checklist: [hardware/README.md](../hardware/README.md). Requirements come from the adapter-board prototype's measurements (ELECTRONICS.md section 14).

- **First PCB project** — KiCad
- **Rev A [P]:** a Pi 5 add-on board carrying the proven circuits (both CAN stacks, both Picos as soldered-down modules, probe front end); **Rev B:** the CM5 carrier, started from the CM5 IO board's published design
- **Dual-board chassis** — front PCB and back PCB; 2.0mm thickness each for rigidity
- Electronics sandwiched between boards; M3 standoffs carry structural load
- Panel-mount connectors on top edge: DB9 (CAN), probe inputs, USB-C charging, expansion port
- Compute board (Pi 5 or CM5 carrier) mounted on standoffs; DSI cable routes to screen on front face
- Button and encoder footprints at exact ergonomic positions — shells just need matching cutouts
- MCP2518FD as bare IC (40 MHz crystal); TCAN1051V-Q1 (or HV) as bare IC; both with fallback module headers and 0-ohm jumpers; unpopulated CAN TVS footprint (D5)
- ADS8584S on PCB, SPI to the meter Pico; requires 5V supply
- Probe attenuator nodes are the most layout-sensitive nets: guard rings, keep-outs, creepage slots, identical layout per channel
- Modules preferred over bare ICs wherever possible — easier to hand-solder and field-replace
- Order 5 copies of first revision — there will be something to fix
### Back Face Expansion Pad Layout
 
Back face kept clear of tall components. Exposed pads serve as both test points during development and contact points for peripheral modules.
 
Pad zones by region:
- **Power corner** — GND, 3.3V, 5V, battery voltage; well-spaced from signal pads
- **CAN corner** — CAN-H, CAN-L, CAN GND; naturally isolated from digital signals
- **GPIO field (center)** — unassigned Pi and Pico GPIO pins; modules grab what they need
- **Active comms cluster (fourth corner)** — small formal defined array for bidirectional signals: I2C SDA/SCL, UART TX/RX, power enable line; these are the only pads where a module drives signals back into the deck
All pads labelled on silkscreen (e.g. "3V3", "CANH", "GP14") — back of board is self-documenting.
 
Magnet positions at corners provide attraction; mechanical alignment via enclosure features (recessed pocket for coarse alignment, raised lip for precise seating before pogo pins make contact). Keyways are a likely simplification in practice.
 
---
 
## PERIPHERAL / SATELLITE / ACCESSORY
 
### OBD-II Adapter
- OBD-II is CAN (ISO 15765) with a standardized 16-pin connector
- Peripheral adapter only — routes to the on-deck MCP2518FD via the DB9 harness connector
- No additional hardware required; software support via existing CAN stack
- Near-term: live PID data. Longer-term: see "Maybe Someday" below
### Wireless DMM Satellites
- Small clip-lead satellite modules for remote voltage monitoring while operating the unit
- **Protocol: ESP-NOW** — connectionless, 1–2ms latency, low power, purpose-built for this
- **Satellite hardware:** ESP32 + ADS1115 ADC front-end (I2C) + same PTC/TVS input protection + LiPo + clip leads
- **Deck-side:** Internal ESP32 subsystem receives ESP-NOW and pipes readings to Pi over its UART (`firmware/esp32-bridge/`, satellite packet v1)
- Readings appear as additional channels in the synchronized capture view alongside wired inputs
- Multiple satellites can connect simultaneously
- Priority: lower — core wired functionality comes first
### Dust Cover
- TPU printed cover — attaches via the same corner magnets as accessory modules; no additional hardware
- Protects back face expansion pads from metal dust, forklift exhaust, and general shop environment
- Tethered to enclosure (lanyard point or slot) so it stays accessible while working — a cover that lives loose in a toolbag will disappear
- Designed in from day one, not retrofitted
### Future Module Ideas (Magnetic, Pogo-Pin Contact)
All modules self-identify via EEPROM or resistor ID. Interface: I2C, UART, or USB routed through active comms cluster.
 
- **Bench power module** — gates the machine's own B+ through a 60 V eFuse with current limiting and metering, rather than generating 12 V+ on-deck (ELECTRONICS.md footnote 1); 5 V is on-deck as the component test source
- **Extended logic/scope module** — additional channels or faster sampling if Pico limits are hit in practice
- **Current clamp ADC module** — useful for forklift electrical fault-finding (motor current, contactor inrush)
- **Expanded CAN module** — second isolated CAN channel (MCP2515/2518) for bridging or sniffing two buses simultaneously
- **LIN transceiver module** — LIN appears on body/comfort systems and some BMS units
- **RS-485 / Modbus module** — some battery chargers and BMS comms use Modbus RTU over RS-485
---
 
## Maybe Someday
 
These are real possibilities given the hardware platform, but each is a significant rabbit hole in its own right. Not planned for any near-term revision.
 
- **OBD-II live sensor data beyond standard PIDs** — requires per-make PID databases; doable but research-heavy
- **OEM proprietary protocol support** — GM, Ford, Toyota, etc. have proprietary layers on top of CAN; open-source work exists (see `opendbc`, comma.ai ecosystem) but nothing plug-and-play
- **ECU flashing via CAN** — protocol-specific, often requires reverse-engineered seed/key authentication; fascinating, very deep rabbit hole
- **Custom machine PID reverse engineering** — passive sniffing + labelling tools on-deck could make this more tractable over time
- **WiFi data offload via ESP32** — log transfer and OTA updates without a physical cable; groundwork already in place
---
 
## Open Questions / Deferred
 
- Electronics open questions (comparator part, SYNC rate, charging while connected, SRC jack): ELECTRONICS.md section 15
- Hardware licence for the board files (GPL vs CERN-OHL-S): hardware/README.md
- Exact grip diameter (~40–45mm target)
- Pogo pad array pinout (deferred until module designs mature)
- Battery capacity and charging/BMS solution
- ESP32 internal mounting (dedicated small PCB vs. module on main board); module chosen: ESP32-WROOM-32UE with U.FL antenna. Whether the Pi drives EN/IO0 so it can flash the ESP32 itself
 
