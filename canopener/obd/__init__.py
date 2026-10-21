"""OBD-II (SAE J1979) automotive diagnostics over CAN — a pure, read-only protocol library,
parallel to ``j1939/``. ISO-TP transport, Mode 01 PID decode, and DTC decode for Modes 03/07/0A.

Like ``j1939/`` this package imports nothing from ``core/``, ``gui/`` or ``sim/``. Mode 04 (clear
DTCs) is deliberately absent: clearing is a write, which belongs behind the confirm-first plugin
path, not here.
"""

from .decode import (
    MODE_CURRENT_DATA,
    MODE_PENDING_DTCS,
    MODE_PERMANENT_DTCS,
    MODE_STORED_DTCS,
    MODE_VEHICLE_INFO,
    ObdResponse,
    decode,
)
from .dtc import decode_dtc, decode_dtc_list, encode_dtc
from .isotp import (
    FUNCTIONAL_REQUEST_ID,
    RESPONSE_IDS,
    RESPONSE_OFFSET,
    IsoTpReassembler,
    flow_control_frame,
    is_response_id,
    request_frame,
)
from .pids import PIDS, Pid, supported_pids

__all__ = [
    "IsoTpReassembler", "request_frame", "flow_control_frame", "is_response_id",
    "FUNCTIONAL_REQUEST_ID", "RESPONSE_IDS", "RESPONSE_OFFSET", "decode", "ObdResponse", "PIDS", "Pid",
    "supported_pids", "decode_dtc", "encode_dtc", "decode_dtc_list",
    "MODE_CURRENT_DATA", "MODE_STORED_DTCS", "MODE_PENDING_DTCS", "MODE_PERMANENT_DTCS",
    "MODE_VEHICLE_INFO",
]
