"""SPN (suspect parameter number) descriptions.

The built-in table covers common standard SAE SPNs. OEM-specific codes (Deutz, Genie,
JLG, Cat, ...) live in the proprietary range 516096-524287 and need the OEM's fault list.
Machine profiles (``canopener.core.profiles``) and a user CSV (``spn,name`` per line, see
``load_spn_csv``) can add or override names.
"""

import csv
import os

PROPRIETARY_SPN_RANGE = range(516096, 524288)

SPN_NAMES = {
    27: "EGR valve position", 51: "Throttle position", 81: "DPF inlet pressure",
    84: "Wheel-based vehicle speed", 91: "Accelerator pedal position 1",
    92: "Engine percent load at current speed", 94: "Fuel delivery pressure",
    96: "Fuel level", 97: "Water in fuel", 98: "Engine oil level",
    100: "Engine oil pressure", 102: "Intake manifold pressure (boost)",
    105: "Intake manifold temperature", 106: "Air inlet pressure",
    107: "Air filter differential pressure", 108: "Barometric pressure",
    109: "Coolant pressure", 110: "Engine coolant temperature", 111: "Coolant level",
    157: "Fuel rail pressure", 158: "Keyswitch battery potential",
    164: "Injection control pressure", 167: "Charging system potential (alternator)",
    168: "Battery potential / power input", 171: "Ambient air temperature",
    172: "Air inlet temperature", 173: "Exhaust gas temperature", 174: "Fuel temperature",
    175: "Engine oil temperature", 183: "Fuel rate", 190: "Engine speed", 237: "VIN",
    247: "Total engine hours", 412: "EGR temperature", 513: "Actual engine torque",
    70: "Parking brake switch", 161: "Transmission input shaft speed",
    191: "Transmission output shaft speed", 521: "Brake pedal position",
    522: "Transmission percent clutch slip", 523: "Transmission current gear",
    524: "Transmission selected gear", 525: "Transmission requested gear",
    526: "Transmission actual gear ratio", 560: "Transmission driveline engaged",
    561: "Anti-slip regulation (ASR) engine control active", 563: "Anti-lock braking (ABS) active",
    573: "Torque converter lockup engaged", 574: "Transmission shift in process",
    597: "Brake switch", 1121: "EBS brake switch",
    558: "Accelerator pedal low-idle switch", 626: "Start enable device (intake heater)",
    627: "ECU power supply", 629: "Controller #1 (ECU internal)", 630: "Calibration memory",
    636: "Camshaft position sensor", 637: "Crankshaft position/timing sensor",
    639: "J1939 network #1", 651: "Injector cylinder 1", 652: "Injector cylinder 2",
    653: "Injector cylinder 3", 654: "Injector cylinder 4", 655: "Injector cylinder 5",
    656: "Injector cylinder 6", 677: "Starter motor relay", 723: "Engine speed sensor #2",
    729: "Inlet air heater driver", 1136: "ECU temperature",
    1347: "Fuel pump pressurizing assembly (metering unit)",
    1569: "Engine protection torque derate", 1638: "Hydraulic oil temperature",
    1761: "DEF tank level", 2659: "EGR mass flow rate", 2791: "EGR valve control",
    3031: "DEF tank temperature", 3216: "Aftertreatment intake NOx",
    3226: "Aftertreatment outlet NOx", 3242: "DPF intake temperature",
    3246: "DPF outlet temperature", 3251: "DPF differential pressure",
    3361: "DEF dosing unit", 3364: "DEF quality",
    3464: "Throttle actuator control command", 3516: "DEF concentration",
    3719: "DPF soot load", 3720: "DPF ash load", 4334: "DEF doser pressure",
    4364: "SCR conversion efficiency", 4765: "DOC intake temperature",
    4766: "DOC outlet temperature", 5246: "SCR operator inducement severity",
}

SPN_OVERRIDES: dict[int, str] = {}


def spn_name(spn: int) -> str:
    if spn in SPN_OVERRIDES:
        return SPN_OVERRIDES[spn]
    if spn in SPN_NAMES:
        return SPN_NAMES[spn]
    if spn in PROPRIETARY_SPN_RANGE:
        return "Manufacturer-specific - check the OEM fault list"
    return "(not in built-in table)"


def load_spn_csv(path: str) -> int:
    """Merge user SPN names from a CSV (``spn,name``; '#' lines ignored). Returns count loaded."""
    if not os.path.exists(path):
        return 0
    n = 0
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f):
            if not row or row[0].lstrip().startswith("#") or len(row) < 2:
                continue
            try:
                spn = int(row[0].strip())
            except ValueError:
                continue    # header line or junk
            SPN_OVERRIDES[spn] = row[1].strip()
            n += 1
    return n
