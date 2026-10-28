"""CANopen (CiA 301) over 11-bit CAN: the protocol controllers on forklifts and other battery
machines often speak (Curtis, ZAPI and many more, with their own message layouts on top).

COB-ID classification (``cob``), NMT states and commands (``nmt``), EMCY errors with the standard
meanings (``emcy``), SDO frames and abort codes (``sdo``), standard object dictionary entries
(``objects``), and a passive decoder that follows all of it from frames (``sniffer``). Building an
NMT command or SDO read is here; sending one is the client's job (``core/canopen.py``), and the
commands go behind a confirmation.

Like ``j1939/`` this package imports nothing from ``core/``, ``gui/`` or ``sim/``.
"""

from . import cob, emcy, nmt, objects, sdo
from .sniffer import CanopenSniffer, CobStats, EmcyEvent, NodeInfo

__all__ = ["cob", "emcy", "nmt", "objects", "sdo", "CanopenSniffer", "CobStats", "EmcyEvent", "NodeInfo"]


def node_name(node: int, with_hex: bool = True) -> str:
    """'Node 05 Traction controller' using the active profile's names (``NODE_OVERRIDES``)."""
    label = NODE_OVERRIDES.get(node, "")
    if not with_hex:
        return label or f"Node {node:02X}"
    return f"Node {node:02X} {label}".rstrip()


# Names for node IDs from the active machine profile (``"nodes"``), filled by core.profiles.
NODE_OVERRIDES: dict[int, str] = {}
