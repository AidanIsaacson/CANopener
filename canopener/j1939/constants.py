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
DM3 = 0xFECC    # Clear previously active (never sent by this app)
DM4 = 0xFECD    # Freeze frame
DM5 = 0xFECE    # Diagnostic readiness
DM6 = 0xFECF    # Pending DTCs
DM11 = 0xFED3   # Clear active (never sent by this app)
DM12 = 0xFED4   # Emissions-related active DTCs
DM23 = 0xFDB5   # Emissions-related previously active DTCs

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
}
STATUS_ORDER = {"Active": 0, "Pending": 1, "Emis. active": 2, "Stored": 3, "Emis. stored": 4}

INFO_PGNS = {
    PGN_COMPONENT_ID: "Component ID",
    PGN_SOFTWARE_ID: "Software ID",
    PGN_ECU_ID: "ECU ID",
    PGN_VIN: "VIN",
    PGN_ENGINE_HOURS: "Engine hours",
    PGN_FUEL_CONSUMPTION: "Fuel consumption",
}

PGN_LABEL = {
    PGN_ACK: "Acknowledgment",
    PGN_REQUEST: "Request",
    PGN_TPDT: "TP data transfer",
    PGN_TPCM: "TP connection mgmt",
    PGN_ADDRESS_CLAIM: "Address claim",
    PGN_PROPA: "Proprietary A",
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
