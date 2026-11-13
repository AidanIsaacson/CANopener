"""Turn other tools' description files into CANopener profiles.

* **DBC** (the Vector database format most CAN tools read and write): messages and their signals,
  with scale, offset, unit and value tables. 29-bit IDs become J1939 PGN signals, 11-bit IDs
  become plain-ID signals.
* **EDS / DCF** (a CANopen device's own description, from its manufacturer): the transmit PDOs it
  sends by default, each value named from the file.
* **A node's PDO mapping read off the bus** (``CanopenClient.read_tpdos``): the same split
  without the file, named by object number until someone labels them.

Each gives a profile document (the JSON described in ``core.profiles``) plus notes about anything
that couldn't be carried over; ``save`` writes it to the user's profiles folder, where it shows up
in the profile list like any other. Nothing here talks to a bus.

A file you were given under a licence or NDA makes a profile that is yours to use, not to
publish: it's saved in ``~/.config/canopener/profiles``, never in the app's own folder.
"""

import json
import os
import re

from ..canopen import eds as edslib
from ..canopen import pdo as pdolib
from . import profiles

_BO = re.compile(r"^BO_\s+(\d+)\s+(\w+)\s*:\s*(\d+)\s+(\S+)")
_SG = re.compile(r"^\s*SG_\s+(\w+)\s*(M|m\d+M?)?\s*:\s*(\d+)\|(\d+)@([01])([+-])\s*"
                 r"\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)\s*\[[^\]]*\]\s*\"([^\"]*)\"")
_VAL = re.compile(r"^VAL_\s+(\d+)\s+(\w+)\s+(.*);\s*$")
_VAL_PAIR = re.compile(r"(-?\d+)\s+\"([^\"]*)\"")
EXTENDED = 0x80000000
VECTOR_INDEPENDENT = 0xC0000000        # the placeholder message DBC editors keep unused signals in


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "imported"


def _decimals(scale: float) -> int:
    text = f"{abs(scale):.6f}".rstrip("0")
    return min(4, len(text.split(".")[1])) if "." in text else 0


def _words(name: str) -> str:
    return name.replace("_", " ").strip()


def dbc_profile(text: str, name: str, protocol: str | None = None) -> tuple[dict, list[str]]:
    """A profile from the text of a DBC file. Returns ``(document, notes)``."""
    messages, values, cur = {}, {}, None
    for line in text.splitlines():
        m = _BO.match(line)
        if m:
            cur = {"id": int(m.group(1)), "name": m.group(2), "dlc": int(m.group(3)), "signals": []}
            messages[cur["id"]] = cur
            continue
        m = _SG.match(line)
        if m and cur is not None:
            cur["signals"].append(m.groups())
            continue
        m = _VAL.match(line)
        if m:
            values[(int(m.group(1)), m.group(2))] = {raw: label for raw, label in _VAL_PAIR.findall(m.group(3))}
        elif line.strip() and not line[:1].isspace():
            cur = None if not line.startswith(("BO_", "SG_")) else cur
    signals, pgns, skipped, ext, std = [], {}, {"multiplexed": 0, "layout": 0, "long": 0}, 0, 0
    for mid, msg in messages.items():
        if mid == VECTOR_INDEPENDENT or not msg["signals"]:
            continue
        where, sa = {}, None
        if mid & EXTENDED:
            raw = mid & 0x1FFFFFFF
            pf = (raw >> 16) & 0xFF
            pgn = (raw >> 8) & (0x3FF00 if pf < 240 else 0x3FFFF)
            where, sa = {"pgn": f"0x{pgn:04X}"}, raw & 0xFF
            pgns[f"0x{pgn:04X}"] = _words(msg["name"])
            ext += 1
        else:
            where = {"id": f"0x{mid & 0x7FF:03X}"}
            std += 1
        for sname, mux, start, length, order, sign, scale, offset, unit in msg["signals"]:
            start, length = int(start), int(length)
            if mux:
                skipped["multiplexed"] += 1
                continue
            sig = {"name": _words(sname), **where, "group": _words(msg["name"])}
            if order == "1":                                    # Intel: start is the least significant bit
                if start + length > 64:
                    skipped["long"] += 1
                    continue
                sig["start"] = start // 8
                if start % 8 == 0 and length in (8, 16, 32):
                    sig["length"] = length // 8
                else:
                    sig["bit"], sig["bits"] = start % 8, length
            elif start % 8 == 7 and length in (8, 16, 32) and start // 8 + length // 8 <= 8:
                sig["start"], sig["length"] = start // 8, length // 8       # Motorola, whole bytes
                if length > 8:
                    sig["order"] = "big"
            elif length <= 8 - (7 - start % 8) and start // 8 < 8:
                # Motorola inside one byte: the same bits either way round
                sig["start"], sig["bit"], sig["bits"] = start // 8, start % 8 - length + 1, length
                if sig["bit"] == 0 and length == 8:
                    del sig["bit"], sig["bits"]
                    sig["length"] = 1
            else:
                skipped["layout"] += 1
                continue
            try:
                sc, off = float(scale), float(offset)
            except ValueError:
                sc, off = 1.0, 0.0
            if sc != 1:
                sig["scale"] = sc
            if off:
                sig["offset"] = off
            if sign == "-":
                sig["signed"] = True
            if unit.strip():
                sig["unit"] = unit.strip()
            table = values.get((mid, sname))
            if table:
                sig["enum"] = table
            else:
                sig["decimals"] = _decimals(sc)
            if sa is not None and sa != 0xFE and "pgn" in where:
                sig["sa"] = f"0x{sa:02X}"
            signals.append(sig)
    proto = protocol or ("j1939+canopen" if ext and std else "j1939" if ext else "canopen")
    doc = {"name": name, "protocol": proto,
           "description": f"Imported from a DBC file: {ext + std} message(s), {len(signals)} signal(s). "
                          "Check the values against the machine before trusting them.",
           "signals": signals}
    if pgns:
        doc["pgns"] = pgns
    if proto == "j1939":
        doc["extends"] = "generic"
    notes = []
    if skipped["multiplexed"]:
        notes.append(f"{skipped['multiplexed']} multiplexed signal(s) left out (one value shared by several meanings)")
    if skipped["layout"]:
        notes.append(f"{skipped['layout']} big-endian signal(s) that aren't whole bytes left out")
    if skipped["long"]:
        notes.append(f"{skipped['long']} signal(s) past the eighth byte left out")
    if not signals:
        notes.append("no signals found: is it a DBC file?")
    return doc, notes


def pdo_profile(pdos_by_node: dict, name: str, namer=None, describe=None, node_names: dict | None = None,
                source: str = "read from the nodes' own PDO mapping") -> tuple[dict, list[str]]:
    """A CANopen profile from each node's transmit PDOs (``{node: [canopen.pdo.Pdo, ...]}``)."""
    signals, notes = [], []
    for node, pdos in sorted(pdos_by_node.items()):
        for p in pdos:
            got = pdolib.signals(p, namer, f"Node {node:02X} {p.label}", describe)
            signals += got
            if p.enabled and p.mapped and not got:
                notes.append(f"node {node:02X} {p.label}: nothing usable in its mapping")
            elif not p.enabled and p.mapped:
                notes.append(f"node {node:02X} {p.label} is switched off in the node")
    doc = {"name": name, "protocol": "canopen",
           "description": f"CANopen process data, {source}. Values are raw: scale and units come from the "
                          "device's manual. Rename them with Label… as you work out what they are.",
           "nodes": {f"0x{n:02X}": (node_names or {}).get(n) or f"Node {n:02X}" for n in sorted(pdos_by_node)},
           "signals": signals}
    if not signals:
        notes.append("no PDO values found")
    return doc, notes


def eds_profile(eds: "edslib.Eds", node: int, name: str | None = None) -> tuple[dict, list[str]]:
    """A profile for the device an EDS / DCF file describes, at node ID ``node``."""
    title = name or eds.product or "CANopen device"
    return pdo_profile({node: edslib.tpdos(eds, node)}, title, eds.name, eds.describe,
                       {node: eds.product or None}, "from the device's EDS file (its default PDOs)")


def check(doc: dict) -> None:
    """Raise ``profiles.ProfileError`` / ``ValueError`` if the app couldn't load ``doc``."""
    profiles.parse_profile(doc, slug(doc.get("name", "")))


def save(doc: dict, file_name: str | None = None, force: bool = False) -> str:
    """Write ``doc`` to the user's profiles folder; returns the path. Refuses to replace an
    existing file unless ``force``."""
    check(doc)
    folder = profiles.user_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, slug(file_name or doc["name"]) + ".json")
    if os.path.exists(path) and not force:
        raise FileExistsError(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    return path
