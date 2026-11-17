"""PGNs and addresses used throughout the app."""

TOOL_SA = 0xF9          # Off-board diagnostic-service tool #1
NULL_SA = 0xFE
GLOBAL_SA = 0xFF

# Network management / transport
PGN_ACK = 0xE800            # 59392 Acknowledgment
PGN_REQUEST = 0xEA00        # 59904 Request
PGN_TPDT = 0xEB00           # 60160 Transport protocol data transfer
PGN_TPCM = 0xEC00           # 60416 Transport protocol connection management
PGN_ADDRESS_CLAIM = 0xEE00  # 60928 Address claimed
PGN_PROPA = 0xEF00          # 61184 Proprietary A

# Diagnostic messages (J1939-73)
DM1 = 0xFECA    # Active DTCs
DM2 = 0xFECB    # Previously active DTCs
DM3 = 0xFECC    # Clear previously active: requested only by core.j1939_clear, after asking
DM4 = 0xFECD    # Freeze frame
DM5 = 0xFECE    # Diagnostic readiness
DM6 = 0xFECF    # Pending DTCs
DM11 = 0xFED3   # Clear active: requested only by core.j1939_clear, after asking
DM12 = 0xFED4   # Emissions-related active DTCs
DM23 = 0xFDB5   # Emissions-related previously active DTCs
DM19 = 0xD300   # Calibration IDs and their verification numbers (CVN)
DM20 = 0xC200   # Monitor performance ratios
DM21 = 0xC100   # Distance and time with the MIL on / since codes were cleared
DM24 = 0xFDB6   # SPNs supported (expanded freeze frame, data stream, tests)
DM25 = 0xFDB7   # Expanded freeze frame
DM26 = 0xFDB8   # Diagnostic readiness, this operating cycle
DM27 = 0xFD82   # All pending DTCs
DM28 = 0xFD80   # Permanent DTCs
DM29 = 0x9E00   # How many emissions-related DTCs of each kind
DM30 = 0xA400   # Scaled test results
DM31 = 0xA300   # Which lamp each DTC is lighting

# Identification / counters (mostly on-request only)
PGN_COMPONENT_ID = 0xFEEB
PGN_SOFTWARE_ID = 0xFEDA
PGN_ECU_ID = 0xFDC5
PGN_VIN = 0xFEEC
PGN_ENGINE_HOURS = 0xFEE5
PGN_FUEL_CONSUMPTION = 0xFEE9

DTC_PGNS = {
    DM1: "Active",
    DM6: "Pending",
    DM12: "Emis. active",
    DM2: "Stored",
    DM23: "Emis. stored",
    DM27: "All pending",
    DM28: "Permanent",
}
STATUS_ORDER = {"Active": 0, "Pending": 1, "All pending": 1.5, "Emis. active": 2, "Stored": 3, "Emis. stored": 4,
                "Permanent": 5}

INFO_PGNS = {
    PGN_COMPONENT_ID: "Component ID",
    PGN_SOFTWARE_ID: "Software ID",
    PGN_ECU_ID: "ECU ID",
    PGN_VIN: "VIN",
    PGN_ENGINE_HOURS: "Engine hours",
    PGN_FUEL_CONSUMPTION: "Fuel consumption",
    DM19: "Calibration ID",
    DM21: "MIL distance / time",
}

PGN_LABEL = {
    PGN_ACK: "Acknowledgment",
    PGN_REQUEST: "Request",
    PGN_TPDT: "TP data transfer",
    PGN_TPCM: "TP connection mgmt",
    PGN_ADDRESS_CLAIM: "Address claim",
    PGN_PROPA: "Proprietary A",
    DM19: "DM19 calibration IDs", DM20: "DM20 monitor performance ratios",
    DM21: "DM21 MIL distance / time", DM24: "DM24 SPNs supported", DM25: "DM25 expanded freeze frame",
    DM26: "DM26 readiness this cycle", DM27: "DM27 all pending codes", DM28: "DM28 permanent codes",
    DM29: "DM29 code counts", DM30: "DM30 scaled test results", DM31: "DM31 lamp for each code",
    0xFECC: "DM3 clear stored codes", 0xFED3: "DM11 clear active codes",
    0xF000: "ERC1 electronic retarder controller 1", 0xF008: "HPG hydraulic pressure governor",
    0xF00A: "EGF1 engine gas flow", 0xF00E: "AT1IG1 aftertreatment 1 intake gas (NOx)",
    0xF00F: "AT1OG1 aftertreatment 1 outlet gas (NOx)", 0xFD7C: "DPFC1 DPF control 1",
    0xFD92: "EOI engine operating information", 0xFDB2: "AT1IMG aftertreatment 1 intermediate gas",
    0xFDB3: "AT1OG2 aftertreatment 1 outlet gas 2", 0xFDB4: "AT1IG2 aftertreatment 1 intake gas 2",
    0xFE69: "ET3 engine temperature 3", 0xFE6C: "TCO1 tachograph", 0xFEA4: "ET2 engine temperature 2",
    0xFE92: "EI engine information", 0xFEBD: "FD fan drive", 0xFEBF: "EBC2 wheel speeds",
    0xFEC1: "VDHR vehicle distance (high resolution)", 0xFED5: "AS alternator speed",
    0xFEDB: "EFL/P2 engine fluid level / pressure 2", 0xFEDC: "IO idle operation",
    0xFEDD: "TC turbocharger", 0xFEDF: "EEC3 electronic engine controller 3",
    0xFEE0: "VD vehicle distance", 0xFEE4: "SHUTDN shutdown", 0xFEE6: "TD time / date",
    0xFEE8: "VDS vehicle direction / speed", 0xFEF0: "PTO power take-off", 0xFEF3: "VP vehicle position",
    0xFEF8: "TRF1 transmission fluids 1", 0xFEFF: "WFI water in fuel indicator",
    0xFEE3: "EC1 engine configuration 1", 0xFEDE: "ACL address claimed (commanded address)",
    # Seen on real machines and named from the public J1939 PGN list (our wording)
    0x0000: "TSC1 torque / speed control 1", 0x0100: "TC1 transmission control 1",
    0x8500: "CM2 cab message 2", 0xD000: "CL instrument lighting level", 0xE000: "CM1 cab message 1",
    0xD700: "DM16 binary data transfer", 0xD800: "DM15 memory access response",
    0xD900: "DM14 memory access request", 0xDA00: "ISO 15765 diagnostics (KWP2000 / UDS)",
    0xF01A: "TFAC throttle / fuel actuator command",
    0xFC02: "SCRSI SCR system information", 0xFC08: "VEP9 vehicle electrical power 9",
    0xFC2A: "OII1 operator inducement information 1", 0xFC3E: "HEDPI heat exchanger debris purge",
    0xFC4A: "SCRSC SCR system cleaning", 0xFC56: "SCROII2 SCR operator inducement 2",
    0xFC57: "SCROII1 SCR operator inducement 1", 0xFC96: "EI2 engine information 2",
    0xFCD1: "DM53 active service-only codes", 0xFCE7: "EC3 engine configuration 3",
    0xFD07: "DLCC1 direct lamp control command 1",
    0xFD0E: "AT1OGC2 outlet NOx sensor correction 2", 0xFD0F: "AT1OGC1 outlet NOx sensor correction 1",
    0xFD10: "AT1IGC2 intake NOx sensor correction 2", 0xFD11: "AT1IGC1 intake NOx sensor correction 1",
    0xFD16: "OCSI occupant classification", 0xFD20: "A1DOC1 diesel oxidation catalyst 1",
    0xFD38: "A2SCREGT1 SCR 2 exhaust gas temperature", 0xFD39: "A2SCREGP SCR 2 exhaust gas pressure",
    0xFD3E: "A1SCREGT1 SCR exhaust gas temperature", 0xFD3F: "A1SCREGP SCR exhaust gas pressure",
    0xFD40: "A1SCRDSR2 SCR dosing system requests 2", 0xFD95: "TRF2 transmission fluids 2",
    0xFD9B: "A1DEFI1 DEF information 1", 0xFDC6: "CSA cold start aids",
    0xFDCA: "ISC intermediate speed control", 0xFDCB: "OHECS off-highway engine control selection",
    0xFE00: "GPBAC generator phase B AC", 0xFE4A: "ETC7 electronic transmission controller 7",
    0xFE8C: "AAI auxiliary analog information", 0xFE99: "TCI3 turbocharger information 3",
    0xFE9A: "TCI2 turbocharger information 2", 0xFEAF: "GFC fuel consumption (gaseous)",
    0xFEC3: "ETC5 electronic transmission controller 5", 0xFEED: "CCSS cruise control / vehicle speed setup",
    0xFEFB: "RF retarder fluids",
    DM1: "DM1 active codes",
    DM2: "DM2 stored codes",
    DM4: "DM4 freeze frames",
    DM5: "DM5 diagnostic readiness",
    DM6: "DM6 pending codes",
    DM12: "DM12 emissions-related active",
    DM23: "DM23 emissions-related previously active",
    0xF001: "EBC1 electronic brake controller 1",
    0xF002: "ETC1 electronic transmission controller 1",
    0xF003: "EEC2 electronic engine controller 2",
    0xF004: "EEC1 electronic engine controller 1",
    0xF005: "ETC2 electronic transmission controller 2",
    0xFD7B: "AT1S aftertreatment 1 service",
    0xFE56: "AT1T1I aftertreatment 1 DEF tank",
    0xFE68: "VF vehicle fluids",
    0xFEE5: "HOURS engine hours / revolutions",
    0xFEEE: "ET1 engine temperature 1",
    0xFEEF: "EFL/P1 engine fluid level / pressure 1",
    0xFEF1: "CCVS cruise control / vehicle speed",
    0xFEF2: "LFE fuel economy (liquid)",
    0xFEF5: "AMB ambient conditions",
    0xFEF6: "IC1 inlet / exhaust conditions 1",
    0xFEF7: "VEP1 vehicle electrical power 1",
    0xFEFC: "DD dash display",
}
PGN_LABEL.update(INFO_PGNS)


# Filled from the active machine profile (see canopener.core.profiles).
PGN_OVERRIDES: dict[int, str] = {}


def pgn_label(pgn: int) -> str:
    if pgn in PGN_OVERRIDES:
        return PGN_OVERRIDES[pgn]
    if pgn in PGN_LABEL:
        return PGN_LABEL[pgn]
    if 0xFF00 <= pgn <= 0xFFFF:
        return "Proprietary B"
    return ""


def is_proprietary(pgn: int) -> bool:
    return pgn == PGN_PROPA or 0xFF00 <= pgn <= 0xFFFF
