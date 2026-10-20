"""
J1939 definitions: PGN names, SA names, SPN descriptions, FMI descriptions,
and SPN scaling tables.
"""

# ── PGN Names ──────────────────────────────────────────────────────────────────
PGN_NAMES = {
    0xEA00: "Request",
    0xE800: "ACK",
    0xEC00: "TP.CM",
    0xEB00: "TP.DT",
    0xFECA: "DM1 - Active Faults",
    0xFECB: "DM2 - Previously Active Faults",
    0xFECC: "DM3 - Fault Clear",
    0xFECD: "DM4 - Freeze Frame",
    0xFECE: "DM5 - Diag Readiness",
    0xFECF: "DM6 - Pending Faults",
    0xFED0: "DM7 - Test Results",
    0xFED1: "DM8 - Test Results",
    0xFED2: "DM9 - Emissions",
    0xFED3: "DM10 - Emissions",
    0xFEDA: "DM11 - Fault Clear",
    0xFEDB: "LFE - Fuel Economy",
    0xFEDC: "LFE2",
    0xFEDE: "IC2 - Inlet/Exhaust",
    0xFEDF: "IC1 - Inlet/Exhaust",
    0xFEE0: "VD - Vehicle Distance",
    0xFEE1: "RCF",
    0xFEE5: "HOURS - Engine Hours",
    0xFEE6: "ET1 - Engine Temp",
    0xFEE9: "EFL_P1 - Fluid Pressures",
    0xFEEA: "EFL_P2",
    0xFEEB: "EFL_P3",
    0xFEEC: "EFL_P4",
    0xFEED: "EFL_P5",
    0xFEEE: "ET2 - Engine Temp 2",
    0xFEEF: "EFL_P6",
    0xFEF0: "PTO - Power Takeoff",
    0xFEF1: "CCVS - Vehicle Speed",
    0xFEF2: "LFC - Fuel Consumption",
    0xFEF5: "TURBO - Turbocharger",
    0xFEF6: "AIR2 - Air Supply",
    0xFEF7: "AIR1 - Air Supply",
    0xFEF8: "VEP1 - Vehicle Electrical",
    0xFEF9: "TRF1 - Trans Fluids",
    0xFEFA: "TRF2",
    0xFEFB: "TCI - Trans Current Gear",
    0xFEFC: "SHUTDN - Shutdown",
    0xFEFD: "DD - Dash Display",
    0xFEFE: "A1 - Alternator",
    0xFEFF: "SERV - Service",
    0xFF00: "PROP_A",
    0xFF01: "PROP_B",
    0xF004: "EEC1 - Engine Control",
    0xF003: "EEC2 - Engine Control 2",
    0xF002: "EEC3 - Engine Control 3",
    0xF001: "EBC1 - Brake Control",
    0xF000: "EBC2 - Brake Control 2",
    0xFEBA: "TRANS1 - Transmission",
    0xFEBF: "DD2",
    0xFEB7: "SERV2",
    0xFEC1: "ECU_ID",
    0xFEC0: "SOFT_ID",
    0xFEBD: "AT1T1I",
    0xFEBE: "AT1T2I",
    0xFEB4: "AT1OG1",
    0xFEB5: "AT1OG2",
    0xFEAF: "EEC5",
    0xFEAE: "EEC6",
    0xFEAD: "EEC7",
}

# ── Source Address Names ───────────────────────────────────────────────────────
SA_NAMES = {
    0x00: "Engine #1",
    0x01: "Engine #2",
    0x02: "Turbocharger",
    0x03: "Transmission #1",
    0x04: "Transmission #2",
    0x05: "Brake - Axle #1",
    0x06: "Brake - Axle #2",
    0x07: "Brake - Axle #3",
    0x08: "Brake - Axle #4",
    0x09: "Brake - Axle #5",
    0x0A: "Brake - System Controller",
    0x0B: "Instrument Cluster #1",
    0x0C: "Trip Recorder",
    0x0D: "Vehicle Management #1",
    0x0E: "Fuel System",
    0x0F: "Steering Column",
    0x10: "Axle - Steering",
    0x11: "Axle #1",
    0x12: "Axle #2",
    0x13: "Axle #3",
    0x14: "Axle #4",
    0x15: "Axle #5",
    0x16: "Cab Climate Control",
    0x17: "Cab Controller - Primary",
    0x18: "Cab Controller - Secondary",
    0x19: "Tire Pressure Controller",
    0x1A: "Ignition Control",
    0x1B: "Seat Control",
    0x1C: "Lighting - Operator Controls",
    0x1D: "Rear Axle Steering",
    0x1E: "Off-Vehicle Gateway",
    0x1F: "Virtual Terminal",
    0x20: "Management Computer #1",
    0x21: "Propulsion Battery Charger",
    0x22: "Headway Controller",
    0x23: "On-Board Diagnostics",
    0x24: "Proximity Detector",
    0x25: "Hydraulic Pump Controller",
    0x26: "Suspension - System Controller",
    0x27: "Pneumatic - System Controller",
    0x28: "Cab Controller #3",
    0x29: "Cab Display #3",
    0x2A: "Articulation Turntable",
    0x2B: "Forward Road Image Processor",
    0x2C: "Sideward Looking Radar",
    0x2D: "Rearward Looking Radar",
    0x2E: "Winch #1",
    0x2F: "Winch #2",
    0xEE: "Reserved",
    0xEF: "Null",
    0xF9: "Diagnostic Tool",
    0xFE: "Null Address",
    0xFF: "Global",
}

# ── FMI Descriptions ───────────────────────────────────────────────────────────
FMI_DESCRIPTIONS = {
    0:  "Data valid but above normal operational range (most severe)",
    1:  "Data valid but below normal operational range (most severe)",
    2:  "Data erratic, intermittent or incorrect",
    3:  "Voltage above normal or shorted to high source",
    4:  "Voltage below normal or shorted to low source",
    5:  "Current below normal or open circuit",
    6:  "Current above normal or grounded circuit",
    7:  "Mechanical system not responding properly",
    8:  "Abnormal frequency, pulse width, or period",
    9:  "Abnormal update rate",
    10: "Abnormal rate of change",
    11: "Root cause not known",
    12: "Bad intelligent device or component",
    13: "Out of calibration",
    14: "Special instructions",
    15: "Data valid but above normal operating range (least severe)",
    16: "Data valid but above normal operating range (moderately severe)",
    17: "Data valid but below normal operating range (least severe)",
    18: "Data valid but below normal operating range (moderately severe)",
    19: "Received network data in error",
    20: "Data drifted high",
    21: "Data drifted low",
    31: "Condition exists (not available / not J1939 fault)",
}

# ── SPN Definitions: (name, unit, scale, offset, min, max) ───────────────────
# scale and offset convert raw value: engineering = raw * scale + offset
SPN_DEFS = {
    # Engine
    190:  ("Engine Speed",                  "rpm",  0.125,  0,     0,    8031.875),
    512:  ("Driver Demand Torque",          "%",    1,     -125,  -125,  125),
    513:  ("Actual Engine Torque",          "%",    1,     -125,  -125,  125),
    514:  ("Nominal Friction Torque",       "%",    1,     -125,  -125,  125),
    515:  ("Engine Ref Torque",             "Nm",   1,      0,     0,    64255),
    91:   ("Accelerator Pedal Position 1",  "%",    0.4,    0,     0,    100),
    92:   ("Engine Load",                   "%",    1,      0,     0,    250),
    94:   ("Fuel Delivery Pressure",        "kPa",  4,      0,     0,    1000),
    100:  ("Engine Oil Pressure",           "kPa",  4,      0,     0,    1000),
    101:  ("Crankcase Pressure",            "kPa",  0.05,  -250,  -250,  252.5),
    102:  ("Boost Pressure",                "kPa",  2,      0,     0,    500),
    103:  ("Turbo Speed",                   "rpm",  4,      0,     0,    250000),
    105:  ("Intake Manifold Temp",          "°C",   1,     -40,   -40,  210),
    106:  ("Air Inlet Pressure",            "kPa",  2,      0,     0,    500),
    107:  ("Air Filter Diff Pressure",      "kPa",  0.05,   0,     0,    12.5),
    108:  ("Barometric Pressure",           "kPa",  0.5,    0,     0,    125),
    109:  ("Coolant Pressure",              "kPa",  2,      0,     0,    500),
    110:  ("Engine Coolant Temp",           "°C",   1,     -40,   -40,  210),
    111:  ("Coolant Level",                 "%",    0.4,    0,     0,    100),
    174:  ("Fuel Temp",                     "°C",   1,     -40,   -40,  210),
    175:  ("Engine Oil Temp",               "°C",   0.03125,-273, -273,  1734.96875),
    176:  ("Turbo Oil Temp",                "°C",   0.03125,-273, -273,  1734.96875),
    183:  ("Fuel Rate",                     "L/h",  0.05,   0,     0,    3212.75),
    184:  ("Instantaneous Fuel Economy",    "km/L", 1/512,  0,     0,    125.5),
    185:  ("Average Fuel Economy",          "km/L", 1/512,  0,     0,    125.5),
    247:  ("Engine Total Hours",            "h",    0.05,   0,     0,    210554060.75),
    # Transmission
    161:  ("Trans Input Shaft Speed",       "rpm",  0.125,  0,     0,    8031.875),
    163:  ("Trans Output Shaft Speed",      "rpm",  0.125,  0,     0,    8031.875),
    177:  ("Trans Oil Temp",                "°C",   0.03125,-273, -273,  1734.96875),
    191:  ("Trans Requested Gear",          "gear", 1,     -125,  -125,  125),
    524:  ("Trans Current Gear",            "gear", 1,     -125,  -125,  125),
    525:  ("Trans Selected Gear",           "gear", 1,     -125,  -125,  125),
    # Vehicle speed / distance
    84:   ("Vehicle Speed (Wheel-based)",   "km/h", 1/256,  0,     0,    250.996),
    517:  ("Vehicle Speed (Cruise)",        "km/h", 1/256,  0,     0,    250.996),
    245:  ("Total Vehicle Distance",        "km",   0.125,  0,     0,    526385151.875),
    # Electrical
    158:  ("Battery Voltage (Keyswitch)",   "V",    0.05,   0,     0,    3212.75),
    168:  ("Battery Voltage #2",            "V",    0.05,   0,     0,    3212.75),
    167:  ("Alternator Voltage",            "V",    0.05,   0,     0,    3212.75),
    # Hydraulic / Lift truck relevant
    1638: ("Hydraulic Temp",               "°C",   1,     -40,   -40,  210),
    1639: ("Hydraulic Pressure",           "kPa",  16,     0,     0,    4000),
    # Brakes
    116:  ("Brake Application Pressure",    "kPa",  4,      0,     0,    1000),
    # Misc
    1: ("Brake Switch",                     "",     1,      0,     0,    1),
    70: ("Park Brake Switch",               "",     1,      0,     0,    1),
    597: ("Brake Pedal Position",           "%",    0.4,    0,     0,    100),
    3031: ("Aftertreatment 1 Exhaust Temp", "°C",  0.03125,-273, -273,  1734.96875),
}

# ── PGN → SPN mapping (which SPNs live in which PGN, byte positions) ──────────
# Format: pgn -> list of (spn, start_byte, start_bit, length_bits)
# This is a simplified subset for decoding
PGN_SPN_MAP = {
    0xF004: [  # EEC1
        (512, 0, 0, 8),
        (513, 1, 0, 8),
        (190, 3, 0, 16),
        (1483, 2, 0, 4),
    ],
    0xF003: [  # EEC2
        (91,  0, 0, 8),
        (92,  2, 0, 8),
        (94,  3, 0, 8),
    ],
    0xFEF1: [  # CCVS
        (84,  1, 0, 16),
        (70,  0, 6, 2),
        (597, 5, 0, 8),
    ],
    0xFEE6: [  # ET1
        (110, 0, 0, 8),
        (175, 2, 0, 16),
    ],
    0xFEE9: [  # EFL/P1
        (94,  0, 0, 8),
        (100, 3, 0, 8),
        (109, 5, 0, 8),
        (111, 7, 0, 8),
    ],
    0xFEDF: [  # IC1
        (102, 0, 0, 8),
        (105, 2, 0, 8),
        (106, 3, 0, 8),
        (107, 4, 0, 8),
        (108, 5, 0, 8),
    ],
    0xFEF5: [  # TURBO
        (103, 0, 0, 16),
        (176, 2, 0, 16),
    ],
    0xFEF8: [  # VEP1
        (158, 4, 0, 16),
        (167, 2, 0, 16),
    ],
    0xFEF9: [  # TRF1
        (177, 0, 0, 16),
        (161, 2, 0, 16),
        (163, 4, 0, 16),
    ],
    0xFEFB: [  # TCI
        (524, 3, 0, 8),
        (525, 4, 0, 8),
        (191, 5, 0, 8),
    ],
    0xFEE5: [  # HOURS
        (247, 0, 0, 32),
    ],
    0xFEE0: [  # VD
        (245, 4, 0, 32),
    ],
    0xFEDB: [  # LFE
        (183, 0, 0, 16),
        (184, 2, 0, 16),
        (185, 4, 0, 16),
    ],
}


def decode_j1939_id(arb_id: int):
    """Decode 29-bit J1939 arbitration ID → (priority, pgn, sa, da)."""
    priority = (arb_id >> 26) & 0x07
    pgn_raw  = (arb_id >> 8) & 0x3FFFF
    sa       = arb_id & 0xFF
    pf = (pgn_raw >> 8) & 0xFF
    if pf < 0xF0:
        da  = pgn_raw & 0xFF
        pgn = pgn_raw & 0x3FF00
    else:
        da  = 0xFF
        pgn = pgn_raw
    return priority, pgn, sa, da


def pgn_name(pgn: int) -> str:
    return PGN_NAMES.get(pgn, f"PGN 0x{pgn:04X}")


def sa_name(sa: int) -> str:
    return SA_NAMES.get(sa, f"Node 0x{sa:02X}")


def fmi_desc(fmi: int) -> str:
    return FMI_DESCRIPTIONS.get(fmi, f"FMI {fmi}")


def spn_info(spn: int):
    """Return (name, unit, scale, offset) or None."""
    return SPN_DEFS.get(spn)


def decode_dm1(data: bytes):
    """
    Parse a DM1 frame payload.
    Returns list of dicts: {spn, fmi, oc, cm, lamp}
    Returns [] if no active faults.
    Returns None if data is malformed.
    """
    if len(data) < 2:
        return None

    # Lamp status byte 0 bits[7:0] — any lamp lit?
    lamp_byte = data[0]
    # No active faults signal: lamp byte == 0x00 AND first SPN bytes are 0xFF
    if lamp_byte == 0x00:
        # Check if all fault bytes are 0xFF (no fault present)
        if len(data) >= 8 and all(b == 0xFF for b in data[2:8]):
            return []
        if len(data) < 8:
            return []

    faults = []
    # Faults start at byte 2, each fault is 4 bytes
    for i in range(2, len(data) - 3, 4):
        b0, b1, b2, b3 = data[i], data[i+1], data[i+2], data[i+3]
        # SPN: bits spread across b0[7:0], b1[7:0], b2[7:5]
        spn = ((b2 & 0xE0) << 11) | (b1 << 8) | b0
        fmi = b2 & 0x1F
        oc  = b3 & 0x7F
        cm  = (b3 >> 7) & 0x01

        # Skip 0xFFFFFF placeholder entries
        if spn == 0x1FFFFF and fmi == 0x1F:
            continue
        # Skip FMI 31 "condition exists / not available"
        if fmi == 31 and spn == 0:
            continue

        lamp = lamp_byte
        faults.append({
            "spn": spn,
            "fmi": fmi,
            "oc":  oc,
            "cm":  cm,
            "lamp": lamp,
        })

    return faults


# Module-level units flag — set via set_units() from app settings
USE_IMPERIAL = False

def set_units(imperial: bool):
    global USE_IMPERIAL
    USE_IMPERIAL = imperial

# Conversion table: metric_unit -> (imperial_unit, multiplier, offset)
_IMPERIAL = {
    "°C":   ("°F",  1.8,     32),
    "km/h": ("mph", 0.62137, 0),
    "kPa":  ("psi", 0.14504, 0),
    "km":   ("mi",  0.62137, 0),
    "L/h":  ("gal/h", 0.26417, 0),
    "km/L": ("mpg", 2.35215, 0),
}

def decode_spn_value(spn: int, raw: int) -> str:
    """Convert raw integer to engineering value string, respecting units setting."""
    info = SPN_DEFS.get(spn)
    if info is None:
        return f"0x{raw:X}"
    name, unit, scale, offset, lo, hi = info
    val = raw * scale + offset
    if raw in (0xFE, 0xFF, 0xFFFE, 0xFFFF, 0xFFFFFE, 0xFFFFFF):
        return "N/A"
    if not unit:
        return f"{val:.2f}"
    if USE_IMPERIAL and unit in _IMPERIAL:
        imp_unit, mult, imp_off = _IMPERIAL[unit]
        val = val * mult + imp_off
        return f"{val:.2f} {imp_unit}"
    return f"{val:.2f} {unit}"


def extract_spn_readings(pgn: int, data: bytes):
    """
    Extract SPN readings from a known PGN frame.
    Returns list of (spn, raw_value, display_string).
    """
    mappings = PGN_SPN_MAP.get(pgn)
    if not mappings or len(data) < 1:
        return []
    results = []
    for (spn, start_byte, start_bit, length_bits) in mappings:
        try:
            if start_byte >= len(data):
                continue
            # Simple byte-aligned extraction for common cases
            if length_bits == 8:
                raw = data[start_byte]
            elif length_bits == 16:
                if start_byte + 1 >= len(data):
                    continue
                raw = data[start_byte] | (data[start_byte+1] << 8)
            elif length_bits == 32:
                if start_byte + 3 >= len(data):
                    continue
                raw = (data[start_byte] |
                       (data[start_byte+1] << 8) |
                       (data[start_byte+2] << 16) |
                       (data[start_byte+3] << 24))
            elif length_bits == 4:
                raw = (data[start_byte] >> start_bit) & 0x0F
            elif length_bits == 2:
                raw = (data[start_byte] >> start_bit) & 0x03
            else:
                continue
            display = decode_spn_value(spn, raw)
            results.append((spn, raw, display))
        except Exception:
            continue
    return results
