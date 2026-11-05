"""SPN (suspect parameter number) descriptions.

The built-in table covers the standard SAE SPNs that commonly show up in fault codes and live
data, in plain words (the full list is SAE's J1939 Digital Annex, which can't be redistributed).
OEM-specific codes (Deutz, Genie, JLG, Cat, ...) live in the proprietary range 516096-524287
and need the OEM's fault list.
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
    # --- more SAE J1939-71 / -73 SPNs (standard names, in plain words) ---
    # Engine and fuel
    15: "Fuel supply pump inlet pressure", 16: "Fuel filter (suction side) differential pressure",
    18: "Extended range fuel pressure", 19: "Extended range engine oil pressure",
    20: "Extended range coolant pressure", 21: "Engine ECU temperature",
    22: "Extended range crankcase blow-by pressure", 30: "Crankcase blow-by pressure",
    52: "Intercooler temperature", 72: "Blower bypass valve position",
    93: "Engine net brake torque", 95: "Fuel filter differential pressure",
    99: "Engine oil filter differential pressure", 101: "Crankcase pressure",
    103: "Turbocharger 1 speed", 104: "Turbocharger 1 lube oil pressure",
    112: "Coolant filter differential pressure", 132: "Intake air mass flow rate (MAF)",
    156: "Injector timing rail 1 pressure", 159: "Gas supply pressure",
    166: "Rated engine power", 176: "Turbocharger oil temperature",
    184: "Instantaneous fuel economy", 185: "Average fuel economy",
    188: "Engine speed at idle", 189: "Rated engine speed",
    411: "EGR differential pressure", 512: "Driver's demand engine percent torque",
    514: "Nominal friction percent torque", 515: "Engine desired operating speed",
    518: "Engine requested torque / torque limit", 898: "Engine requested speed / speed limit",
    899: "Engine torque mode", 1072: "Engine brake output #1", 1073: "Engine brake output #2",
    1127: "Turbocharger 1 boost pressure", 1128: "Turbocharger 2 boost pressure",
    1135: "Engine oil temperature 2", 1172: "Turbocharger 1 compressor inlet temperature",
    1176: "Turbocharger 1 compressor inlet pressure",
    1180: "Turbocharger 1 turbine inlet temperature",
    1184: "Turbocharger 1 turbine outlet temperature",
    1188: "Turbocharger wastegate actuator 1 position", 1209: "Exhaust gas pressure",
    1239: "Fuel leakage 1", 1240: "Fuel leakage 2", 1322: "Multiple cylinder misfire",
    1323: "Cylinder 1 misfire", 1324: "Cylinder 2 misfire", 1325: "Cylinder 3 misfire",
    1326: "Cylinder 4 misfire", 1327: "Cylinder 5 misfire", 1328: "Cylinder 6 misfire",
    1348: "Fuel pump pressurizing assembly 2", 1349: "Injector metering rail 2 pressure",
    1675: "Engine starter mode", 2433: "Exhaust gas temperature, right manifold",
    2434: "Exhaust gas temperature, left manifold",
    2629: "Turbocharger 1 compressor outlet temperature",
    2630: "Charge air cooler outlet temperature", 3563: "Intake manifold 1 absolute pressure",
    5571: "Fuel rail pressure relief valve",
    # Engine actuators, drivers and supplies
    620: "5 V sensor supply", 631: "Calibration module", 632: "Fuel shutoff valve 1",
    633: "Fuel control valve 1", 635: "Timing actuator 1",
    641: "Variable geometry turbo (VGT) actuator 1", 647: "Fan clutch output driver",
    657: "Injector cylinder 7", 658: "Injector cylinder 8", 659: "Injector cylinder 9",
    660: "Injector cylinder 10", 661: "Injector cylinder 11", 662: "Injector cylinder 12",
    663: "Injector cylinder 13", 664: "Injector cylinder 14", 665: "Injector cylinder 15",
    666: "Injector cylinder 16", 667: "Injector cylinder 17", 668: "Injector cylinder 18",
    676: "Glow plug relay", 678: "ECU 8 V supply", 679: "Injection control pressure regulator",
    701: "Auxiliary I/O #1", 702: "Auxiliary I/O #2", 703: "Auxiliary I/O #3",
    704: "Auxiliary I/O #4", 705: "Auxiliary I/O #5", 706: "Auxiliary I/O #6",
    707: "Auxiliary I/O #7", 708: "Auxiliary I/O #8", 709: "Auxiliary I/O #9",
    710: "Auxiliary I/O #10", 711: "Auxiliary I/O #11", 712: "Auxiliary I/O #12",
    730: "Inlet air heater driver #2",
    3509: "Sensor supply 1", 3510: "Sensor supply 2", 3511: "Sensor supply 3",
    3512: "Sensor supply 4", 3513: "Sensor supply 5", 3514: "Sensor supply 6",
    # Engine protection, switches, lamps, fan
    970: "Engine auxiliary shutdown switch", 974: "Remote accelerator pedal position",
    975: "Estimated fan speed (percent)", 977: "Fan drive state", 1639: "Fan speed",
    1109: "Engine protection: approaching shutdown", 1110: "Engine protection: engine shut down",
    1237: "Engine shutdown override switch", 559: "Accelerator pedal kickdown switch",
    623: "Red stop lamp", 624: "Amber warning lamp", 987: "Protect lamp",
    1081: "Wait-to-start lamp", 1213: "Malfunction indicator lamp (MIL)",
    # Networks
    608: "J1587 data link", 1231: "J1939 network #2", 1235: "J1939 network #3",
    # Aftertreatment
    3363: "DEF tank heater", 3609: "DPF intake pressure", 3610: "DPF outlet pressure",
    3695: "DPF regeneration inhibit switch", 3696: "DPF regeneration force switch",
    3700: "DPF active regeneration status", 3701: "DPF status",
    3702: "DPF active regeneration inhibited", 3936: "DPF system",
    4094: "NOx limits exceeded: poor DEF quality", 4096: "NOx limits exceeded: DEF tank empty",
    4340: "DEF line heater 1", 4342: "DEF line heater 2",
    4360: "SCR intake gas temperature", 4363: "SCR outlet gas temperature",
    4376: "DEF return valve", 4792: "SCR catalyst system", 4794: "SCR catalyst missing",
    4795: "DPF missing", 5298: "DOC conversion efficiency", 5394: "DEF dosing valve",
    5397: "DPF regeneration too frequent", 5742: "DPF temperature sensor module",
    5745: "DEF dosing unit heater",
    # Vehicle, driveline, brakes, electrical
    28: "Accelerator pedal position 3", 29: "Accelerator pedal position 2",
    31: "Transmission range position", 32: "Transmission splitter position",
    38: "Fuel level 2", 46: "Pneumatic supply pressure", 69: "Two-speed axle switch",
    73: "Auxiliary pump pressure", 74: "Maximum vehicle speed limit",
    75: "Steering axle temperature", 77: "Forward rear drive axle temperature",
    78: "Rear rear drive axle temperature", 79: "Road surface temperature",
    80: "Washer fluid level", 82: "Air start pressure", 86: "Cruise control set speed",
    90: "Power take-off oil temperature", 114: "Net battery current", 115: "Alternator current",
    116: "Brake application pressure", 117: "Brake primary pressure",
    118: "Brake secondary pressure", 123: "Clutch pressure", 124: "Transmission oil level",
    126: "Transmission filter differential pressure", 127: "Transmission oil pressure",
    162: "Transmission requested range", 163: "Transmission current range",
    169: "Cargo ambient temperature", 170: "Cab interior temperature",
    177: "Transmission oil temperature", 441: "Auxiliary temperature 1",
    442: "Auxiliary temperature 2", 527: "Cruise control states", 598: "Clutch switch",
    604: "Transmission neutral switch", 904: "Front axle speed", 1624: "Tachograph vehicle speed",
    # Totals and trip data
    182: "Trip fuel", 235: "Total idle hours", 236: "Total idle fuel used", 244: "Trip distance",
    245: "Total vehicle distance", 246: "Total vehicle hours", 248: "Total PTO hours",
    249: "Total engine revolutions", 250: "Total fuel used",
    917: "Total vehicle distance (high resolution)", 918: "Trip distance (high resolution)",
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
