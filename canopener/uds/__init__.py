"""UDS (ISO 14229) message layer — a pure, protocol-only package like ``j1939`` and ``obd``.

Reads (DTCs with status, data by identifier) and the standard negative-response codes are built
in. Bi-directional services (session control, routines, I/O control, reset, clear) are reachable
through ``build_request`` so a user can issue the standardized ones for their equipment behind the
app's confirm-first transmit gate. No OEM identifiers are hardcoded here, and it imports nothing
from ``core/`` / ``gui/`` / ``sim/``.
"""

from .dtc import decode_uds_dtc, parse_dtc_by_status_mask, read_dtcs
from .services import (
    NRC,
    SERVICES,
    STATUS_BITS,
    UdsError,
    build_request,
    diagnostic_session,
    parse_response,
    read_data_by_identifier,
    read_dtc_by_status_mask,
    status_flags,
    tester_present,
)

__all__ = [
    "SERVICES", "NRC", "STATUS_BITS", "UdsError", "build_request", "read_data_by_identifier",
    "read_dtc_by_status_mask", "tester_present", "diagnostic_session", "parse_response",
    "status_flags", "decode_uds_dtc", "parse_dtc_by_status_mask", "read_dtcs",
]
