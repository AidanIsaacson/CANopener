"""UDS (ISO 14229-1 / "Unified Diagnostic Services") message layer: service IDs, negative-response
codes, and request builders / response parsers. Transport is ISO-TP (reuses ``obd.isotp``).

UDS is how non-emissions modules — ABS, transmission, body, instrument clusters — are read and
controlled on modern vehicles, where generic OBD-II only exposes the emissions powertrain data.
The *protocol* here is fully public (ISO 14229). The meaning of a given data identifier (DID),
routine or I/O control is **manufacturer-specific**: this module never hardcodes OEM identifiers.

Read services get first-class helpers (ReadDataByIdentifier, ReadDTCInformation). Bi-directional
services (session control, routines, I/O control, reset, clear) are reachable through the generic
``build_request`` composer so a user who knows the standard IDs for their equipment can issue them
— always behind the app's confirm-first transmit gate — without this public repo shipping any
specific OEM command.
"""

POSITIVE_OFFSET = 0x40
NEGATIVE = 0x7F

# Service IDs (ISO 14229-1). Names are reference only; see build_request for sending arbitrary ones.
SERVICES = {
    0x10: "DiagnosticSessionControl", 0x11: "ECUReset", 0x14: "ClearDiagnosticInformation",
    0x19: "ReadDTCInformation", 0x22: "ReadDataByIdentifier", 0x23: "ReadMemoryByAddress",
    0x27: "SecurityAccess", 0x28: "CommunicationControl", 0x2E: "WriteDataByIdentifier",
    0x2F: "InputOutputControlByIdentifier", 0x31: "RoutineControl", 0x34: "RequestDownload",
    0x35: "RequestUpload", 0x36: "TransferData", 0x37: "RequestTransferExit",
    0x3E: "TesterPresent", 0x85: "ControlDTCSetting",
}

# Negative-response codes (ISO 14229-1). 0x78 "response pending" is handled specially by clients.
NRC = {
    0x10: "general reject", 0x11: "service not supported", 0x12: "sub-function not supported",
    0x13: "incorrect message length or invalid format", 0x14: "response too long",
    0x21: "busy, repeat request", 0x22: "conditions not correct", 0x24: "request sequence error",
    0x25: "no response from subnet component", 0x26: "failure prevents execution",
    0x31: "request out of range", 0x33: "security access denied", 0x35: "invalid key",
    0x36: "exceeded number of attempts", 0x37: "required time delay not expired",
    0x70: "upload/download not accepted", 0x71: "transfer data suspended",
    0x72: "general programming failure", 0x73: "wrong block sequence counter",
    0x78: "request received, response pending", 0x7E: "sub-function not supported in active session",
    0x7F: "service not supported in active session", 0x81: "RPM too high", 0x82: "RPM too low",
    0x83: "engine is running", 0x84: "engine is not running", 0x85: "engine run time too low",
    0x86: "temperature too high", 0x87: "temperature too low", 0x88: "vehicle speed too high",
    0x89: "vehicle speed too low", 0x8A: "throttle/pedal too high", 0x8B: "throttle/pedal too low",
    0x8C: "transmission range not in neutral", 0x8D: "transmission range not in gear",
    0x8F: "brake switch(es) not closed", 0x90: "shifter lever not in park",
    0x91: "torque converter clutch locked", 0x92: "voltage too high", 0x93: "voltage too low",
}

# ReadDTCInformation (0x19) sub-functions (public ISO 14229 list; common ones).
RDTC_BY_STATUS_MASK = 0x02
RDTC_SUPPORTED_NUMBER = 0x01
RDTC_SUPPORTED_DTCS = 0x0A
RDTC_SUBFUNCTIONS = {
    0x01: "reportNumberOfDTCByStatusMask", 0x02: "reportDTCByStatusMask",
    0x03: "reportDTCSnapshotIdentification", 0x04: "reportDTCSnapshotRecordByDTCNumber",
    0x06: "reportDTCExtDataRecordByDTCNumber", 0x0A: "reportSupportedDTC",
    0x0B: "reportFirstTestFailedDTC", 0x0C: "reportFirstConfirmedDTC",
    0x0D: "reportMostRecentTestFailedDTC", 0x0E: "reportMostRecentConfirmedDTC",
    0x14: "reportDTCFaultDetectionCounter", 0x15: "reportDTCWithPermanentStatus",
}

# The identification data identifiers every UDS module may answer (ISO 14229-1 Annex C), in plain
# words. Everything else below 0xF100 and in 0xFD00-0xFEFF is the manufacturer's own.
DID_NAMES = {
    0xF180: "Boot software identification", 0xF181: "Application software identification",
    0xF182: "Application data identification", 0xF183: "Boot software fingerprint",
    0xF184: "Application software fingerprint", 0xF185: "Application data fingerprint",
    0xF186: "Active diagnostic session", 0xF187: "Manufacturer spare part number",
    0xF188: "Manufacturer ECU software number", 0xF189: "Manufacturer ECU software version",
    0xF18A: "System supplier", 0xF18B: "ECU manufacturing date", 0xF18C: "ECU serial number",
    0xF18D: "Supported functional units", 0xF18E: "Manufacturer kit assembly part number",
    0xF190: "VIN", 0xF191: "Manufacturer ECU hardware number", 0xF192: "Supplier ECU hardware number",
    0xF193: "Supplier ECU hardware version", 0xF194: "Supplier ECU software number",
    0xF195: "Supplier ECU software version", 0xF196: "Exhaust regulation / type approval number",
    0xF197: "System name / engine type", 0xF198: "Repair shop code / tester serial number",
    0xF199: "Programming date", 0xF19A: "Calibration repair shop code / equipment serial number",
    0xF19B: "Calibration date", 0xF19C: "Calibration equipment software number",
    0xF19D: "ECU installation date", 0xF19E: "ODX file", 0xF19F: "Entity",
}
# The ones worth asking for when identifying a module (reads only).
IDENTIFICATION_DIDS = (0xF190, 0xF197, 0xF18A, 0xF187, 0xF191, 0xF193, 0xF188, 0xF189, 0xF194, 0xF195,
                       0xF18C, 0xF18B, 0xF199, 0xF180, 0xF181)


def did_name(did: int) -> str:
    if did in DID_NAMES:
        return DID_NAMES[did]
    if 0xF400 <= did <= 0xF4FF:
        return f"OBD PID 0x{did & 0xFF:02X} (OBD on UDS)"
    if 0xF800 <= did <= 0xF8FF:
        return f"OBD vehicle info 0x{did & 0xFF:02X} (OBD on UDS)"
    return ""


def did_text(data: bytes) -> str:
    """A data identifier's value for display: its text when it is text, its hex otherwise (a
    date is usually three BCD bytes, which read correctly as hex: ``20 24 03``)."""
    data = bytes(data)
    stripped = data.strip(b"\x00\xff ")
    if stripped and all(0x20 <= b < 0x7F for b in stripped):
        return stripped.decode("ascii")
    return data.hex(" ").upper()

# Standard DTC status-byte bits (ISO 14229-1).
STATUS_BITS = [
    (0x01, "testFailed"), (0x02, "testFailedThisOperationCycle"), (0x04, "pendingDTC"),
    (0x08, "confirmedDTC"), (0x10, "testNotCompletedSinceLastClear"),
    (0x20, "testFailedSinceLastClear"), (0x40, "testNotCompletedThisOperationCycle"),
    (0x80, "warningIndicatorRequested"),
]


class UdsError(Exception):
    def __init__(self, message: str, nrc: int | None = None):
        super().__init__(message)
        self.nrc = nrc


def build_request(sid: int, payload: bytes = b"") -> bytes:
    """A raw UDS request: the service id followed by its payload. The caller supplies the payload
    from the public ISO 14229 definition (and, for OEM routines/DIDs, from their own data)."""
    return bytes([sid]) + bytes(payload)


def read_data_by_identifier(did: int) -> bytes:
    return bytes([0x22, (did >> 8) & 0xFF, did & 0xFF])


def read_dtc_by_status_mask(mask: int = 0xFF) -> bytes:
    return bytes([0x19, RDTC_BY_STATUS_MASK, mask & 0xFF])


def read_supported_dtcs() -> bytes:
    """Every code the module can set, whatever its state (0x19 0x0A)."""
    return bytes([0x19, RDTC_SUPPORTED_DTCS])


ALL_DTC_GROUPS = 0xFFFFFF
RESET_HARD, RESET_KEY_OFF_ON, RESET_SOFT = 0x01, 0x02, 0x03
RESETS = {RESET_HARD: "Hard reset", RESET_KEY_OFF_ON: "Key off/on reset", RESET_SOFT: "Soft reset"}


def clear_diagnostic_information(group: int = ALL_DTC_GROUPS) -> bytes:
    """ClearDiagnosticInformation (0x14) for a DTC group (0xFFFFFF = all)."""
    return bytes([0x14]) + (group & 0xFFFFFF).to_bytes(3, "big")


def ecu_reset(kind: int = RESET_HARD) -> bytes:
    return bytes([0x11, kind & 0xFF])


def tester_present() -> bytes:
    return bytes([0x3E, 0x00])


def diagnostic_session(session_type: int = 0x03) -> bytes:
    """Switch session (0x01 default, 0x03 extended). Extended is the prerequisite for most
    bi-directional services."""
    return bytes([0x10, session_type & 0xFF])


def status_flags(status: int) -> list[str]:
    return [name for bit, name in STATUS_BITS if status & bit]


def parse_response(request_sid: int, data: bytes) -> bytes:
    """Validate a UDS response and return its payload (bytes after the echoed SID).

    Raises ``UdsError`` on a negative response (with the NRC) or a mismatched/short reply. A
    0x78 "response pending" is surfaced as an NRC so the client can decide to keep waiting.
    """
    if not data:
        raise UdsError("empty response")
    if data[0] == NEGATIVE:
        nrc = data[2] if len(data) > 2 else 0
        raise UdsError(f"negative response: {NRC.get(nrc, f'NRC 0x{nrc:02X}')}", nrc)
    if data[0] != request_sid + POSITIVE_OFFSET:
        raise UdsError(f"unexpected response service 0x{data[0]:02X}")
    return data[1:]
