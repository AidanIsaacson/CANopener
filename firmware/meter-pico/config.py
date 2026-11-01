# Meter Pico (Pico 2 / RP2350) pins, bus addresses and calibration.
# Pin map: docs/ELECTRONICS.md 5.6. Change a pin here, change it there too.

# SYNC: timer-driven pulse out to Pi GPIO21 (pps-gpio) and the logic Pico's D2
SYNC = 15
SYNC_PERIOD_MS = 100            # 10 Hz; core/sync.py PERIOD_S must match

# I2C0: MCP4728 threshold DAC, ADS1115 (ohms node), INA226 (test-source current)
I2C_ID, I2C_SDA, I2C_SCL, I2C_HZ = 0, 4, 5, 400_000
ADDR_DAC = 0x60
ADDR_OHMS_ADC = 0x48
ADDR_INA226 = 0x40

OHMS_EN = 6                     # TS5A3153 control: high = excitation onto the ohms probe
OHMS_PROBE = "p3"               # never probe 4 (the test source's tip)
COM_SENSE = 7                   # optional: voltage across the COM PTC (high = fault) [P]
SRC_EN = 8                      # load switch enable, active high
SRC_FAULT = 9                   # load switch nFAULT, open drain, active low (pull-up on)
PULLUP = {"p1": 10, "p2": 11}   # 4.7k pull-ups from the test-source rail

# ADS8584S, serial mode (stage 8)
ADS_SCLK, ADS_CS, ADS_DOUTA, ADS_DOUTB = 16, 17, 18, 19
ADS_CONVST, ADS_BUSY, ADS_RESET = 20, 21, 22   # CONVSTA + CONVSTB tied together

# Internal ADC: CAN monitors (GP26-28)
CAN_ADC = {"can_h": 26, "can_l": 27, "can_gnd": 28}
CAN_DIVIDER = 11.0              # volts at the bus per volt at the pin [TBD: bench, ELECTRONICS.md 6.6]
CAN_OFFSET = 0.0                # if a bias network lets CAN GND read below 0 V [TBD]

# Probe scaling: volts at the tip per volt at the ADC. Attenuator A is 9 Mohm against the
# ADS8584S's own 1 Mohm (0.85-1.15 Mohm), so calibrate each channel against a reference.
PROBE_RATIO = {"p1": 10.0, "p2": 10.0, "p3": 10.0, "p4": 10.0}
THRESHOLD_RATIO = 10.0          # attenuator B: tip volts per comparator volt
DAC_VREF = 3.3                  # MCP4728 on VDD reference

# Test source
SRC_SHUNT_OHMS = 0.5
SRC_PRECHECK_V = 0.5            # refuse to switch the source on if probe 4 already reads more
OHMS_PRECHECK_V = 0.2           # refuse ohms mode on a live circuit
OHMS_REF_V = 2.5                # REF3025
OHMS_SERIES = 10_000.0          # 10k 0.1%

# Stage 7: one probe channel on the Pico's own ADC before the ADS8584S exists.
# Set to (channel id, GPIO) to borrow an ADC pin, e.g. ("p1", 28) instead of CAN GND.
PROBE_ON_PICO_ADC = None

DEFAULT_RATE_HZ = 20
OVERSAMPLE = 16                 # conversions averaged per meter reading
