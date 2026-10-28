"""Labels you make in the app: names for modules and signals CANopener doesn't know.

They're saved as ordinary user profiles (``~/.config/canopener/profiles/<id>.json``, the format
in ``core.profiles``), so you can keep one per machine, hand-edit them, share them or move one into
a private plugin. A profile made here usually ``extends`` the profile that was in use (say the
generic J1939 one), so it only holds what you added: built-in and plugin profiles are never
changed on disk.

Everything here edits the JSON document itself (not the parsed ``Profile``) so keys this version
doesn't know about survive a save, and every write is atomic: a crash never leaves half a file.
Front-ends call these, then reload profiles (``profiles.available_profiles`` + ``apply``) so the
labels show up everywhere at once.
"""

import json
import os
import re
import shutil

from ..j1939.signals import Signal
from . import canopen, config, profiles
from .profiles import Profile, ProfileError


def is_editable(p: Profile | None) -> bool:
    """Only user profiles are written to; built-in and plugin ones get a profile on top."""
    return p is not None and profiles.origin(p) == "user"


def path_for(pid: str) -> str:
    return os.path.join(profiles.user_dir(), pid + ".json")


def new_id(name: str, taken=()) -> str:
    """A file-name-safe id from a profile name, not clashing with ``taken`` ids."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "my-profile"
    taken = set(taken)
    pid, n = base, 2
    while pid in taken or os.path.exists(path_for(pid)):
        pid, n = f"{base}-{n}", n + 1
    return pid


def _taken() -> set[str]:
    return {p.id for p in profiles.available_profiles()[0]}


def read_doc(pid: str) -> dict:
    try:
        with open(path_for(pid), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ProfileError(f"{pid}.json: {e}") from None
    if not isinstance(doc, dict):
        raise ProfileError(f"{pid}.json: not a JSON object")
    return doc


def write_doc(pid: str, doc: dict):
    """Validate, then write atomically (temp file, fsync, rename)."""
    profiles.parse_profile(doc, pid)            # raises ProfileError: never save a broken profile
    path = path_for(pid)
    config.ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def create(name: str, base: Profile | None = None, description: str = "") -> str:
    """A new, empty user profile built on ``base`` (None = standalone J1939). Returns its id."""
    name = name.strip() or "My machine"
    doc: dict = {"name": name}
    if description:
        doc["description"] = description
    if base is not None:
        doc["extends"] = base.id
    else:
        doc["protocol"] = profiles.J1939
    doc.update({"sources": {}, "signals": []})
    pid = new_id(name, _taken())
    write_doc(pid, doc)
    return pid


def save_as(src: Profile | None, name: str) -> str:
    """'Save profile as…': a user profile is copied under the new name (labels and all); a
    built-in / plugin one (or none) gets a new profile built on it. Returns the new id."""
    if not is_editable(src):
        return create(name, src)
    doc = read_doc(src.id)
    doc["name"] = name.strip() or src.name
    pid = new_id(doc["name"], _taken())
    write_doc(pid, doc)
    return pid


def rename(pid: str, name: str):
    doc = read_doc(pid)
    doc["name"] = name.strip() or doc.get("name", pid)
    write_doc(pid, doc)


def delete_profile(pid: str):
    os.remove(path_for(pid))


# --- modules (source addresses) ----------------------------------------------------------------

def _drop_key(d: dict, value: int):
    """Remove every spelling of a numeric key ('0x2B', '43', ...)."""
    for k in list(d):
        try:
            if profiles._int(k) == value:
                del d[k]
        except ValueError:
            pass


def set_module_name(pid: str, sa: int, name: str):
    """Name a J1939 source address, or a CANopen node given as ``canopen.node_src(node)``, in
    this profile; an empty name removes it (the base's / built-in name shows again)."""
    doc = read_doc(pid)
    if canopen.is_node(sa):
        table, key = doc.setdefault("nodes", {}), sa - canopen.NODE_SRC
    else:
        table, key = doc.setdefault("sources", {}), sa
    _drop_key(table, key)
    if name.strip():
        table[f"0x{key:02X}"] = name.strip()
    write_doc(pid, doc)


def module_names(pid: str) -> dict[int, str]:
    """The module names this profile itself sets (not inherited ones): J1939 addresses, and
    CANopen nodes as ``canopen.node_src(node)``."""
    doc = read_doc(pid)
    out = profiles._num_map(doc.get("sources"), "sources")
    out.update({canopen.node_src(n): v for n, v in profiles._num_map(doc.get("nodes"), "nodes").items()})
    return out


def set_detect(pid: str, addresses, nodes=()) -> list[int]:
    """'This bus is this machine': recognise it from these J1939 source addresses and CANopen
    nodes next time. Returns what was saved (addresses, then nodes as ``node_src``)."""
    found, found_nodes = sorted(set(addresses)), sorted(set(nodes))
    doc = read_doc(pid)
    det: dict = {"min_match": min(2, len(found) + len(found_nodes))}
    if found:
        det["addresses"] = [f"0x{a:02X}" for a in found]
    if found_nodes:
        det["nodes"] = [f"0x{n:02X}" for n in found_nodes]
    doc["detect"] = det
    write_doc(pid, doc)
    return found + [canopen.node_src(n) for n in found_nodes]


# --- signals ----------------------------------------------------------------------------------

DEFAULTS = {"scale": 1, "offset": 0, "signed": False, "unit": "", "group": "", "order": "little"}


def clean_signal(d: dict) -> dict:
    """A signal dict as it's saved: hex PGN / addresses, default values left out. Raises
    ValueError (with a plain message) if it doesn't describe a valid signal."""
    out: dict = {}
    for k, v in d.items():
        if v is None or v == "" or (k in DEFAULTS and v == DEFAULTS[k]) or (k == "enum" and not v):
            continue
        out[k] = v
    if out.get("id") is not None:
        out.pop("pgn", None)
        out["id"] = f"0x{profiles._int(out['id']):03X}"
    elif "pgn" in out:
        out.pop("id", None)
        out["pgn"] = f"0x{profiles._int(out['pgn']):04X}"
    sa = out.get("sa")
    if isinstance(sa, (list, tuple)):
        out["sa"] = [f"0x{profiles._int(x):02X}" for x in sa]
    elif sa is not None:
        out["sa"] = f"0x{profiles._int(sa):02X}"
    if out.get("bits"):
        out.pop("length", None)
        out.pop("order", None)
    else:
        out.pop("bits", None)
        out.pop("bit", None)
    try:
        profiles.parse_signal(out)
    except KeyError as e:
        raise ValueError(f"missing {e.args[0]}") from None
    except (TypeError, ValueError) as e:
        raise ValueError(str(e)) from None
    if not str(out.get("name", "")).strip():
        raise ValueError("give it a name")
    return out


def own_signals(pid: str) -> list[tuple[Signal, dict]]:
    """The signals this profile itself defines, parsed, with their saved dicts."""
    return [(profiles.parse_signal(d), d) for d in read_doc(pid).get("signals", [])]


def put_signal(pid: str, d: dict, replace_key: str | None = None) -> Signal:
    """Add a signal, or replace the one whose ``Signal.key`` is ``replace_key`` (an edit). A
    signal already saved at the same place and address is replaced too, not duplicated."""
    d = clean_signal(d)
    sig = profiles.parse_signal(d)
    doc = read_doc(pid)
    kept = []
    for old in doc.get("signals", []):
        try:
            o = profiles.parse_signal(old)
        except (KeyError, TypeError, ValueError):
            kept.append(old)
            continue
        if o.key == replace_key or (o.location == sig.location and o.sa == sig.sa):
            continue
        kept.append(old)
    doc["signals"] = kept + [d]
    write_doc(pid, doc)
    return sig


def delete_signal(pid: str, key: str) -> bool:
    doc = read_doc(pid)
    sigs = doc.get("signals", [])
    kept = [s for s in sigs if profiles.parse_signal(s).key != key]
    doc["signals"] = kept
    write_doc(pid, doc)
    return len(kept) != len(sigs)


def preview(d: dict, data: bytes) -> tuple[float | None, str]:
    """Decode ``data`` with a signal being drafted, exactly as Live Data will (same decoder).
    Returns (value, display text); raises ValueError if the draft isn't valid yet."""
    sig = profiles.parse_signal(clean_signal({**d, "name": d.get("name") or "preview"}))
    v = sig.decode(data)
    return v, format_value(sig, v)


def format_value(sig: Signal, v) -> str:
    if v is None:
        return "n/a"
    t = sig.text(v)
    if t is not None:
        return t
    return f"{v:.{sig.decimals}f} {sig.unit}".strip()


# --- sharing ----------------------------------------------------------------------------------

def export_to(pid: str, folder: str) -> str:
    """Copy a profile's JSON into ``folder`` (a USB stick, say). Returns the file written."""
    dest = os.path.join(folder, pid + ".json")
    n = 2
    while os.path.exists(dest):
        dest, n = os.path.join(folder, f"{pid}-{n}.json"), n + 1
    shutil.copyfile(path_for(pid), dest)
    return dest


def import_file(path: str) -> str:
    """Install a profile JSON as a user profile (a new id if that one's taken). Returns its id."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ProfileError(f"{os.path.basename(path)}: {e}") from None
    stem = os.path.splitext(os.path.basename(path))[0]
    profiles.parse_profile(doc, stem, path)     # validate before installing
    pid = new_id(stem, _taken())
    write_doc(pid, doc)
    return pid
