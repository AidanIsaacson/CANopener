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
