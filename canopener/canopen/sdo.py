"""SDO (service data objects): reading a node's object dictionary, one entry at a time.

The client sends to 0x600 + node and the node answers on 0x580 + node. Up to four bytes come back
in one frame ("expedited"); longer values (names, versions) come in 7-byte segments the client
asks for one by one. Request builders are here for the read side (upload) and for aborting; a
download (write) is only parsed when seen on the bus, never built. ``parse`` reads any SDO frame in
either direction, so the sniffer can follow another tool's reads too.
"""

from dataclasses import dataclass

# command specifiers (top 3 bits of byte 0)
CCS_DOWNLOAD_SEGMENT, CCS_INITIATE_DOWNLOAD, CCS_INITIATE_UPLOAD, CCS_UPLOAD_SEGMENT = 0, 1, 2, 3
SCS_UPLOAD_SEGMENT, SCS_DOWNLOAD_SEGMENT, SCS_INITIATE_UPLOAD, SCS_INITIATE_DOWNLOAD = 0, 1, 2, 3
CS_ABORT = 4

ABORT_CODES = {
    0x05030000: "Toggle bit not alternated",
    0x05040000: "SDO protocol timed out",
    0x05040001: "Command specifier not valid or unknown",
    0x05040002: "Invalid block size",
    0x05040003: "Invalid sequence number",
    0x05040004: "CRC error",
    0x05040005: "Out of memory",
    0x06010000: "Unsupported access to an object",
    0x06010001: "Attempt to read a write-only object",
    0x06010002: "Attempt to write a read-only object",
    0x06020000: "Object does not exist in the object dictionary",
    0x06040041: "Object cannot be mapped to the PDO",
    0x06040042: "Mapped objects would exceed the PDO length",
    0x06040043: "General parameter incompatibility",
    0x06040047: "General internal incompatibility in the device",
    0x06060000: "Access failed due to a hardware error",
    0x06070010: "Data type does not match (length of service parameter)",
    0x06070012: "Data type does not match (length too high)",
    0x06070013: "Data type does not match (length too low)",
    0x06090011: "Sub-index does not exist",
    0x06090030: "Invalid value for parameter",
    0x06090031: "Value of parameter written too high",
    0x06090032: "Value of parameter written too low",
    0x06090036: "Maximum value is less than minimum value",
    0x060A0023: "Resource not available",
    0x08000000: "General error",
    0x08000020: "Data cannot be transferred or stored to the application",
    0x08000021: "Data cannot be transferred or stored (local control)",
    0x08000022: "Data cannot be transferred or stored (present device state)",
    0x08000023: "Object dictionary not present or could not be generated",
    0x08000024: "No data available",
}


class SdoError(Exception):
    pass


class SdoAbort(SdoError):
    def __init__(self, code: int, index: int = 0, sub: int = 0):
        self.code, self.index, self.sub = code, index, sub
        super().__init__(f"{index:04X}.{sub}: {abort_text(code)} (0x{code:08X})")


def abort_text(code: int) -> str:
    return ABORT_CODES.get(code, "Abort (manufacturer-specific reason)")


def _mux(index: int, sub: int) -> bytes:
    return bytes([index & 0xFF, index >> 8 & 0xFF, sub & 0xFF])


# --- requests (client -> node) ---------------------------------------------------------------

def upload_request(index: int, sub: int = 0) -> bytes:
    """Initiate upload (read) of object ``index`` / ``sub``."""
    return bytes([CCS_INITIATE_UPLOAD << 5]) + _mux(index, sub) + b"\x00" * 4


def segment_request(toggle: int) -> bytes:
    """Ask for the next segment of a segmented upload (toggle alternates 0, 1, 0 ...)."""
    return bytes([CCS_UPLOAD_SEGMENT << 5 | (toggle & 1) << 4]) + b"\x00" * 7


def abort_request(index: int, sub: int, code: int = 0x08000000) -> bytes:
    return bytes([CS_ABORT << 5]) + _mux(index, sub) + code.to_bytes(4, "little")


# --- parsing (either direction) ---------------------------------------------------------------

@dataclass(frozen=True)
class SdoFrame:
    """One SDO frame. ``kind``: "upload" (initiate upload request or answer), "upload_segment",
    "download" (initiate download), "download_segment", "abort", or "other" (block transfer)."""
    kind: str
    index: int = 0
    sub: int = 0
    data: bytes = b""       # the value (expedited) or segment bytes
    expedited: bool = False
    size: int | None = None  # total size announced by a segmented initiate answer
    toggle: int = 0
    last: bool = False      # last segment
    abort_code: int = 0


def parse(data: bytes, from_node: bool) -> SdoFrame | None:
    """Parse an SDO frame. ``from_node``: True for 0x580+ answers, False for 0x600+ requests."""
    if len(data) < 8:
        return None
    cmd = data[0]
    cs = cmd >> 5
    index, sub = data[1] | data[2] << 8, data[3]
    if cs == CS_ABORT:
        return SdoFrame("abort", index, sub, abort_code=int.from_bytes(data[4:8], "little"))
    initiate_upload = SCS_INITIATE_UPLOAD if from_node else CCS_INITIATE_UPLOAD
    initiate_download = CCS_INITIATE_DOWNLOAD if not from_node else SCS_INITIATE_DOWNLOAD
    if cs == initiate_upload:
        if not from_node:
            return SdoFrame("upload", index, sub)
        return _initiate(cmd, "upload", index, sub, data)
    if cs == initiate_download:
        if from_node:
            return SdoFrame("download", index, sub)
        return _initiate(cmd, "download", index, sub, data)
    seg_upload = SCS_UPLOAD_SEGMENT if from_node else CCS_UPLOAD_SEGMENT
    if cs == seg_upload:
        toggle = cmd >> 4 & 1
        if not from_node:
            return SdoFrame("upload_segment", toggle=toggle)
        n = cmd >> 1 & 7
        return SdoFrame("upload_segment", data=bytes(data[1:8 - n]), toggle=toggle, last=bool(cmd & 1))
    seg_download = CCS_DOWNLOAD_SEGMENT if not from_node else SCS_DOWNLOAD_SEGMENT
    if cs == seg_download:
        toggle = cmd >> 4 & 1
        if from_node:
            return SdoFrame("download_segment", toggle=toggle)
        n = cmd >> 1 & 7
        return SdoFrame("download_segment", data=bytes(data[1:8 - n]), toggle=toggle, last=bool(cmd & 1))
    return SdoFrame("other", index, sub)


def _initiate(cmd: int, kind: str, index: int, sub: int, data: bytes) -> SdoFrame:
    e, s = bool(cmd & 2), bool(cmd & 1)
    if e:
        n = cmd >> 2 & 3 if s else 0
        return SdoFrame(kind, index, sub, data=bytes(data[4:8 - n]), expedited=True)
    size = int.from_bytes(data[4:8], "little") if s else None
    return SdoFrame(kind, index, sub, size=size)


# --- answers (for the simulator, and tests) -------------------------------------------------------

def upload_answer(index: int, sub: int, value: bytes) -> list[bytes]:
    """The node's side of an upload of ``value``: the initiate answer, then (for more than four
    bytes) each segment in order, to be sent one per segment request."""
    value = bytes(value)
    if len(value) <= 4:
        n = 4 - len(value)
        return [bytes([SCS_INITIATE_UPLOAD << 5 | n << 2 | 0x03]) + _mux(index, sub) + value.ljust(4, b"\x00")]
    out = [bytes([SCS_INITIATE_UPLOAD << 5 | 0x01]) + _mux(index, sub) + len(value).to_bytes(4, "little")]
    toggle = 0
    for i in range(0, len(value), 7):
        chunk = value[i:i + 7]
        last = i + 7 >= len(value)
        n = 7 - len(chunk)
        out.append(bytes([SCS_UPLOAD_SEGMENT << 5 | toggle << 4 | n << 1 | int(last)]) + chunk.ljust(7, b"\x00"))
        toggle ^= 1
    return out


def abort_answer(index: int, sub: int, code: int) -> bytes:
    return abort_request(index, sub, code)
