"""EDS / DCF files: a CANopen device's own description of its object dictionary (CiA 306).

Manufacturers hand these out with the device. One names every object, gives its data type and
default value, and so also says what's in each PDO by default. ``load`` reads the file;
``tpdos`` works out the transmit PDOs for a node ID; together with ``canopen.pdo.signals`` that
makes a profile for the device without typing anything in.

A DCF is an EDS with one particular device's configured values (``ParameterValue``), which win
over the defaults here. Only the standard keys are read; anything else in the file is ignored.
"""

import configparser
import re
from dataclasses import dataclass

from . import pdo

# CiA 301 data types -> (bits, signed). Others (strings, domains, floats) aren't PDO numbers here.
DATA_TYPES = {0x01: (1, False), 0x02: (8, True), 0x03: (16, True), 0x04: (32, True), 0x05: (8, False),
              0x06: (16, False), 0x07: (32, False), 0x10: (24, True), 0x16: (24, False)}
_SUB = re.compile(r"^([0-9A-Fa-f]{4})sub([0-9A-Fa-f]{1,2})$")
_INDEX = re.compile(r"^[0-9A-Fa-f]{4}$")


@dataclass(frozen=True)
class Entry:
    index: int
    sub: int
    name: str
    data_type: int | None
    default: str            # as written: may be "$NODEID+0x180"
    pdo_mappable: bool


@dataclass
class Eds:
    entries: dict           # (index, sub) -> Entry
    parents: dict           # index -> the object's own name (for "Parent: sub name")
    product: str = ""
    vendor: str = ""
    node_id: int | None = None      # a DCF says which node it was saved from

    def get(self, index: int, sub: int = 0) -> Entry | None:
        return self.entries.get((index, sub))

    def name(self, index: int, sub: int = 0) -> str:
        e, parent = self.get(index, sub), self.parents.get(index, "")
        if e is None:
            return parent
        if parent and e.name and e.name.lower() != parent.lower() and (index, 1) in self.entries:
            return f"{parent}: {e.name}"
        return e.name or parent

    def describe(self, index: int, sub: int = 0) -> dict:
        e = self.get(index, sub)
        if e is None or e.data_type not in DATA_TYPES:
            return {}
        return {"signed": True} if DATA_TYPES[e.data_type][1] else {}

    def value(self, index: int, sub: int, node: int) -> int | None:
        e = self.get(index, sub)
        return number(e.default, node) if e else None


def number(text: str, node: int = 0) -> int | None:
    """An EDS number: decimal, 0x hex or 0 octal, optionally ``$NODEID+`` in front."""
    t = (text or "").strip().replace(" ", "")
    if not t:
        return None
    total = 0
    if t.upper().startswith("$NODEID"):
        total, t = node, t[7:].lstrip("+")
        if not t:
            return total
    try:
        if t.lower().startswith("0x"):
            return total + int(t, 16)
        if len(t) > 1 and t.startswith("0") and t.isdigit():
            return total + int(t, 8)
        return total + int(t, 10)
    except ValueError:
        return None


def loads(text: str) -> Eds:
    cp = configparser.RawConfigParser(strict=False, interpolation=None, delimiters=("=",), comment_prefixes=(";",))
    cp.optionxform = str.lower
    cp.read_string(text)
    entries, parents = {}, {}
    for section in cp.sections():
        m, s = _SUB.match(section), cp[section]
        if m:
            index, sub = int(m.group(1), 16), int(m.group(2), 16)
        elif _INDEX.match(section):
            index, sub = int(section, 16), 0
            parents[index] = s.get("parametername", "").strip()
        else:
            continue
        default = s.get("parametervalue") or s.get("defaultvalue") or ""
        entries[(index, sub)] = Entry(index, sub, s.get("parametername", "").strip(),
                                      number(s.get("datatype", "")), default.strip(),
                                      (s.get("pdomapping", "0").strip() == "1"))
    dev = cp["DeviceInfo"] if cp.has_section("DeviceInfo") else {}
    com = cp["DeviceComissioning"] if cp.has_section("DeviceComissioning") else {}      # (sic: the standard's spelling)
    return Eds(entries, parents, str(dev.get("productname", "")).strip(), str(dev.get("vendorname", "")).strip(),
               number(str(com.get("nodeid", ""))))


def load(path: str) -> Eds:
    with open(path, encoding="latin-1") as f:
        return loads(f.read())


def tpdos(eds: Eds, node: int) -> list[pdo.Pdo]:
    """The transmit PDOs the file describes for ``node``, mapping included."""
    out = []
    for n in range(pdo.MAX_PDOS):
        cob = eds.value(pdo.TPDO_COMM + n, 1, node)
        count = eds.value(pdo.TPDO_MAP + n, 0, node)
        if cob is None and count is None:
            if n >= 4:
                break
            continue
        if cob is None:                             # the usual default when the file doesn't say
            cob = (0x180 + 0x100 * n + node) if n < 4 else pdo.COB_INVALID
        mapped = []
        for i in range(1, (count or 0) + 1):
            v = eds.value(pdo.TPDO_MAP + n, i, node)
            if v:
                mapped.append(pdo.Mapped.from_value(v))
        out.append(pdo.Pdo(n + 1, cob, tuple(mapped)))
    return out
