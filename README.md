# CAN OPENER
### Field Diagnostic Cyberdeck — Build Notes
*Architecture & design decisions as of April 2026*
 
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
- CM4 will run warm in an enclosed sandwich — vent slots or a thermally conductive wall section required on the back PCB
- Do not fully seal
**Module footprint**
- Back face expansion area is larger than the main unit profile — peripheral modules can be as thick as needed
---
 
## ON-DECK Hardware
 
### Core Compute
 
**Prototype: Raspberry Pi 3B+**
- Development and software validation platform
- Runs full Linux headless, boots straight into diagnostic app, no desktop environment
- Storage: 64–128GB microSD, SanDisk or Samsung Pro Endurance (rated for sustained writes)
**Final build: Compute Module 4 (CM4)**
- Target platform for finished instrument
- eMMC storage, cleaner layout, no dangling ports; custom carrier board exposes exactly what's needed
- **RAM: 2GB** — Pi OS headless base runs ~150–200MB at idle; full diagnostic stack with sigrok and active logging estimated 300–500MB under load. 2GB provides comfortable headroom without paying for 4GB/8GB that won't be used on a dedicated single-purpose instrument
- No built-in Bluetooth or WiFi required on CM4 — handled by internal ESP32 subsystem (see Wireless & Bluetooth)
### CAN Interface
 
**Controller: MCP2518FD** (bare IC, soldered directly to PCB)
- 2KB RAM buffer eliminates dropped frames under Linux scheduler load; missing a fault frame on a diagnostic tool is unacceptable
- Supports CAN FD for future-proofing with automotive and newer industrial networks
- Runs on 3.3V SPI to Pi; requires stable external crystal (20MHz or 40MHz)
- Kernel overlay: `dtoverlay=mcp251xfd,spi0-0,interrupt=25` — appears as standard `can0`, existing Python logger works as-is
**Transceiver: TCAN1051V-Q1** (bare IC — no module available; hand-solder with hot air)
- Split VCC/VIO architecture: VCC on 5V for full-strength industrial bus drive, VIO on 3.3V for direct logic connection to MCP2518FD; no level shifters needed
- Automotive-grade (Q1), ±58V fault tolerance on CAN-H/L — survives accidental battery contact on a 48V system
- Built-in thermal shutdown and bus timeout prevents taking down the bus if the Pi crashes mid-transmission
- *On-hand fallback: SN65HVD230 module — usable but lacks ±58V fault tolerance*
**Line protection & conditioning**
- **PESD1CAN or NUP2105L** dual TVS array — placed directly behind DB9 connector, catches ESD before it reaches the transceiver
- **Split termination** — two 60Ω resistors in series with a 4.7nF cap from center tap to ground; acts as a low-pass RF filter for motor controller noise
- **Termination switch** — physical switch near DB9 connector replaces previous jumper design; off when sniffing an intact bus, on when bench testing a single ECU
- Twisted pair harness provides common-mode noise rejection — no ferrite beads needed on the CAN lines
**Repairability / fallback layout**
- Primary circuit uses bare MCP2518FD and TCAN1051V-Q1 ICs for clean low-profile assembly
- Adjacent 0.1" female header footprints matching standard MCP2515/transceiver breakout module pinouts populated on PCB
- SPI and CAN lines routed through 0-ohm resistor jumpers (removable with soldering iron) — field repair path: remove jumpers, slot in replacement module, back running
- DB9 connector on top edge for harness connection
### Pico Coprocessor
- **Raspberry Pi Pico** — handles logic analyzer and analog capture simultaneously
- Communicates with Pi over USB (virtual serial); powered through same connection
- PIO state machines handle deterministic digital sampling — no OS jitter
- Compatible with **sigrok/PulseView** protocol decoder ecosystem
#### Logic Analyzer (Pico — PIO)
- Up to 8 digital channels via GPIO
- Practical sample rates: 20–40MHz multi-channel, higher single-channel
- Protocol decoders via sigrok: CAN, I2C, SPI, UART, PWM — covers all common forklift/automotive bus work
- Triggered capture on edge or pattern
#### Analog Capture / Waveform Viewer (Pico — ADC + ADS8584S)
- **3x internal channels** (Pico ADC GP26/27/28) — hardwired on PCB to CAN-H, CAN-L, and CAN GND for passive bus voltage monitoring; lighter divider/clamp sized for 0–3.5V range
- **4x external probe channels** via **ADS8584S** (SPI to Pico) — for field voltage measurements
- Practical use: ground float, voltage sag, supply droop, sensor signal shape, dead spots, crank sensor dropout
- Bandwidth: 330kSPS per channel — captures transient faults in the millisecond range
- Stäubli banana jacks for probe connections
**ADS8584S specifics**
- 4-channel, 16-bit, simultaneous-sampling SAR ADC — all channels sampled at the same instant, no inter-channel skew
- ±10V or ±5V bipolar input range on single 5V supply
- 1MΩ input impedance with integrated PGA and ADC driver — no external op-amp buffer required
- Built-in input clamp handles up to ±15V before activation; external protection still recommended for hard industrial transients
- SPI interface to Pico; needs 5V supply rail
**Analog front-end signal chain (per external probe channel)**
`Probe tip → PTC resettable fuse → TVS diode → precision voltage divider → ADS8584S`
 
- **PTC thermistor** (Bourns MF-MSMF, 500mA trip) — series input protection, resets automatically after fault
- **TVS diode** (Littelfuse SMAJ60A, 60V standoff) — clamps transient spikes to ground before the divider
- **Precision voltage divider** — 0–60V input scaled to 0–10V for ADS8584S ±10V range; 0.1% thin-film resistors (Susumu or Vishay) for temperature stability; R1=50kΩ, R2=10kΩ
- ADS8584S internal clamp and driver handle the rest — no op-amp buffer stage needed
*Note: Satellite DMM modules use ADS1115 (I2C, simpler) — the ADS8584S is on-deck only.*
 
#### Resistance Measurement (One Channel)
 
One external analog channel is extended with a simple excitation circuit to support resistance measurement. Target range: near-zero (continuity) through ~20kΩ — covers wire shorts, sensor reference checks, coil resistance, and known reference values (e.g. "this pin should be 20kΩ to ground").
 
**Excitation circuit (added to one probe channel)**
`REF3025 (2.5V precision reference) → 10kΩ 0.1% series resistor → TS5A3153 analog switch → probe tip`
 
- **REF3025** — 2.5V precision voltage reference (SOT-23); low quiescent current, stable over temperature, safe excitation voltage that won't back-drive partially-powered circuits
- **10kΩ 0.1% series resistor** — known reference; Ohm's law derives unknown resistance from measured midpoint voltage
- **TS5A3153 analog switch** — Pico GPIO disconnects excitation when channel is in voltage mode; 2.5V must not sit on a probe tip measuring a live circuit
- Fits in ~1cm² on PCB
**Voltage-on-probe behavior**
If excitation is active and external voltage is detected on the probe (e.g. technician clips onto a live circuit by mistake), the resistance calculation produces garbage — identical to a DMM reading OL or 0.0. CAN Opener detects the condition and displays a warning with the actual measured voltage shown below it. The technician knows immediately why the reading is invalid, and the voltage readout is often useful diagnostic information in its own right. No additional hardware required beyond the excitation circuit — detection falls out of the ADC reading naturally.
 
### Wireless & Bluetooth (Internal ESP32)
 
An ESP32 module lives inside the enclosure as a dedicated wireless subsystem. The CM4 has no built-in Bluetooth or WiFi; the ESP32 provides both. Communicates with the Pi/CM4 over USB serial.
 
**ESP32 variant: Full ESP32 (not ESP32-C3)**
The C3 is single-core RISC-V with BLE only. The full ESP32 is dual-core with dual-mode Bluetooth (Classic BR/EDR + BLE). For a field diagnostic tool where the technician doesn't know in advance what Bluetooth-equipped equipment they'll encounter, dual-mode coverage is the correct choice. Classic Bluetooth is still common on older BMS units, industrial adapters, and test equipment.
 
**Functions:**
- **ESP-NOW receiver** — receives readings from wireless DMM satellite modules; pipes data to Pi over USB serial. Connectionless, 1–2ms latency, purpose-built for this use case
- **Bluetooth gateway** — pairs with Bluetooth-enabled field equipment: BMS units, newer diagnostic adapters, Bluetooth-capable test equipment (e.g. Fluke meters with Bluetooth output). Readings routed to Pi and surfaced in the diagnostic UI alongside wired inputs
- **Future: WiFi data offload** — log transfer without a physical cable; OTA firmware conduit for Pico and ESP32 itself
### Synchronized Multi-Domain Capture
- Single GPIO wire from Pi to Pico establishes shared T+0 at capture start
- CAN frames (decoded, timestamped), logic channels, and all analog channels aligned on one timebase
- Enables root-cause correlation: e.g., "ECU ground floated at T+0.147s → CAN error frame at T+0.149s → bus quiet for 0.2s → recovery at T+0.350s"
### Display & UI Hardware
- **7" DSI touchscreen** — toughened glass if available; direct Pi connection, minimal CPU overhead
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
 
The Pi/CM4 treats each peripheral (Pico, CAN interface, ESP32) as a managed subsystem with a known state. Unexpected state changes surface as actionable dialogue rather than silent failures or hangs.
 
**Examples:**
 
*"Pico not detected — Retry?"*
Pi polls the Pico's USB serial heartbeat. On dropout: offer retry before assuming hardware failure. On successful retry, resume transparently. On repeated failure, flag Pico-dependent features (logic analyzer, analog channels) as unavailable and continue on CAN-only — the tool remains useful.
 
*"Unconfigured Pico detected — Flash?"*
A factory-fresh or blank Pico enumerates differently on USB. Pi detects the condition, offers to flash firmware in-situ: puts Pico into BOOTSEL mode programmatically, copies the UF2, Pico reboots ready. Field-replaceable with zero tools and no laptop.
 
Same pattern applies to CAN interface, ESP32, and future satellite modules. Every subsystem has an expected state; deviations get a human-readable dialogue.
 
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
 
- **First PCB project** — KiCad
- **Dual-board chassis** — front PCB and back PCB; 2.0mm thickness each for rigidity
- Electronics sandwiched between boards; M3 standoffs carry structural load
- Panel-mount connectors on top edge: DB9 (CAN), probe inputs, USB-C charging, expansion port
- Compute board (3B+ or CM4 carrier) mounted on standoffs; DSI cable routes to screen on front face
- Button and encoder footprints at exact ergonomic positions — shells just need matching cutouts
- MCP2518FD as bare IC; TCAN1051V-Q1 as bare IC; both with fallback module headers and 0-ohm jumpers
- ADS8584S on PCB, SPI to Pico; requires 5V supply
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
- **Deck-side:** Internal ESP32 subsystem receives ESP-NOW and pipes readings to Pi over USB serial
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
 
- **Bench power module** — switchable 3.3V/5V/12V with current limiting
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
 
- Exact grip diameter (~40–45mm target)
- Pogo pad array pinout (deferred until module designs mature)
- Battery capacity and charging/BMS solution
- ESP32 module selection and internal mounting (dedicated small PCB vs. module on main board)
 
