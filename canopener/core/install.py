"""Which install of CANopener this is: a random ID made once, and the serial people read out.

``install_id()`` is a random hex ID written to ``~/.config/canopener/device-id`` the first time
it's needed. Uploads, remote help and the SSH tunnel name the unit by it, and team access
(``core.team``) grants the team plugin to it. ``serial()`` is the same ID in a form you can read
over the phone (``CO-7F3K-9QXA``): no 0/O or 1/I mix-ups, shown in **Help > About** and
**Help > Team access**. The upload server works it out the same way.

The ID is tied loosely to the computer: next to it is a fingerprint of the system's machine ID
(``/etc/machine-id``, salted and shortened, so the raw ID is never stored or sent). If the config
folder is copied to another computer the fingerprints differ, so that computer gets an ID of its
own instead of passing itself off as the first one (and team access asks again). A computer with
no machine ID simply isn't checked. Standard library only.
"""

import hashlib
import os
import re
import uuid

from . import config

ID_FILE = "device-id"
MACHINE_FILE = "device-machine"
MACHINE_ID_FILES = ("/etc/machine-id", "/var/lib/dbus/machine-id")
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"        # Crockford's base 32: no I, L, O, U
SERIAL_RE = re.compile(r"^(?:CO)?[0-9A-HJKMNP-TV-Z]{8}$")


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _write(path: str, text: str):
    config.ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text + "\n")
    os.replace(tmp, path)


def machine() -> str:
    """A short fingerprint of this computer's machine ID ("" when it has none)."""
    for path in MACHINE_ID_FILES:
        v = _read(path)
        if v:
            return hashlib.sha256(f"canopener:{v}".encode()).hexdigest()[:16]
    return ""


def install_id() -> str:
    """This install's ID, made the first time (a new one when the config folder came from another
    computer)."""
    folder = config.config_dir()
    id_path, m_path = os.path.join(folder, ID_FILE), os.path.join(folder, MACHINE_FILE)
    v, here = _read(id_path), machine()
    if v:
        seen = _read(m_path)
        if not here or seen == here:
            return v
        if not seen:                # made before fingerprints were kept: adopt this computer
            _write(m_path, here)
            return v
    v = uuid.uuid4().hex
    _write(id_path, v)
    if here:
        _write(m_path, here)
    return v


def serial(iid: str | None = None) -> str:
    """``CO-XXXX-XXXX`` from the first 40 bits of the ID (any text works; hex is used as is)."""
    iid = (iid if iid is not None else install_id()).strip().lower()
    hexpart = iid[:10] if re.fullmatch(r"[0-9a-f]{10,}", iid) else hashlib.sha256(iid.encode()).hexdigest()[:10]
    n = int(hexpart, 16)
    chars = "".join(ALPHABET[(n >> (5 * i)) & 31] for i in range(7, -1, -1))
    return f"CO-{chars[:4]}-{chars[4:]}"


def normalize_serial(text: str) -> str:
    """``co 7f3k 9qxa`` -> ``CO-7F3K-9QXA``; "" when it isn't one. O/I/L typed by mistake count
    as 0/1/1, as Crockford intended."""
    t = re.sub(r"[\s-]", "", (text or "").upper())
    t = t[:2] + t[2:].translate(str.maketrans("OIL", "011")) if t.startswith("CO") else \
        t.translate(str.maketrans("OIL", "011"))
    if not SERIAL_RE.match(t):
        return ""
    t = t[-8:]
    return f"CO-{t[:4]}-{t[4:]}"
