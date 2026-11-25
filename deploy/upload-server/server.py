#!/usr/bin/env python3
"""CANopener upload server: receives capture folders from the app and serves them back to readers.

Standard library only (Python 3.10+), one file, no database: everything lives under DATA_DIR.
See README.md next to this file for the API, the layout on disk and how to run it (Docker, behind
a reverse proxy).

Configuration (environment variables):

  UPLOAD_TOKENS      comma-separated tokens that may upload (the app / handheld units)
  READ_TOKENS        comma-separated tokens that may list and download (you, an AI assistant)
  ADMIN_TOKENS       comma-separated tokens that may delete captures (default none: nothing can)
  DATA_DIR           where captures are kept (default /data)
  HOST, PORT         where to listen (default 0.0.0.0:8080)
  PUBLIC_URL         the address readers use (https://logs.example.com), for the links it returns
  SERVER_NAME        a name shown to the app's Test button (default "CANopener upload server")
  MAX_UPLOAD_MB      largest capture bundle accepted (default 4096)
  MAX_PART_MB        largest single part (default 64; keep under Cloudflare's 100 MB request limit)
  KEEP_REVISIONS     earlier uploads of the same capture kept (default 3)
  INCOMING_DAYS      unfinished uploads are deleted after this many days (default 7)
  KEEP_DAYS          captures are deleted this many days after their last upload (default 0: never)
  REQUEST_TIMEOUT    seconds a connection may sit idle before it's dropped (default 120)
  TEAM_MAX_PENDING   team access: requests waiting for approval at once (default 20; 0 = invites only)
"""

import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

VERSION = "4"
CLEANUP_EVERY = 3600             # seconds between cleanups (unfinished uploads, expired captures)
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,159}$")
ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_JSON = 1024 * 1024
MAX_MEMBERS = 1000
MAX_EXTRACT_RATIO = 200          # a zip that unpacks to more than this many times its size is refused
TEXT_TYPES = {".md": "text/markdown; charset=utf-8", ".txt": "text/plain; charset=utf-8",
              ".log": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8",
              ".json": "application/json", ".zip": "application/zip"}


class Config:
    def __init__(self, env=os.environ):
        self.data = os.path.abspath(env.get("DATA_DIR") or "/data")
        self.host = env.get("HOST") or "0.0.0.0"
        self.port = int(env.get("PORT") or 8080)
        self.public_url = (env.get("PUBLIC_URL") or "").rstrip("/")
        self.name = env.get("SERVER_NAME") or "CANopener upload server"
        self.upload_tokens = [t.strip() for t in (env.get("UPLOAD_TOKENS") or "").split(",") if t.strip()]
        self.read_tokens = [t.strip() for t in (env.get("READ_TOKENS") or "").split(",") if t.strip()]
        self.admin_tokens = [t.strip() for t in (env.get("ADMIN_TOKENS") or "").split(",") if t.strip()]
        self.remote_tokens = [t.strip() for t in (env.get("REMOTE_TOKENS") or "").split(",") if t.strip()]
        self.remote_minutes = float(env.get("REMOTE_MAX_MINUTES") or 240)
        # The SSH tunnel beside remote help (the 'tunnel' container): empty host = not offered.
        self.tunnel_host = (env.get("TUNNEL_HOST") or "").strip()
        self.tunnel_port = int(env.get("TUNNEL_PORT") or 47822)
        self.tunnel_user = env.get("TUNNEL_USER") or "tunnel"
        self.tunnel_first = int(env.get("TUNNEL_FIRST_LISTEN_PORT") or 22201)
        self.tunnel_units = int(env.get("TUNNEL_MAX_UNITS") or 16)
        self.max_upload = int(float(env.get("MAX_UPLOAD_MB") or 4096) * 1024 * 1024)
        self.max_part = int(float(env.get("MAX_PART_MB") or 64) * 1024 * 1024)
        self.keep = int(env.get("KEEP_REVISIONS") or 3)
        self.incoming_days = float(env.get("INCOMING_DAYS") or 7)
        self.keep_days = float(env.get("KEEP_DAYS") or 0)
        self.timeout = float(env.get("REQUEST_TIMEOUT") or 120)
        self.team_pending = int(env.get("TEAM_MAX_PENDING") or 20)


class ApiError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


def _write_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _read_json(path: str, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Store:
    """Captures on disk::

        DATA_DIR/index.json                        one entry per capture (newest upload first)
        DATA_DIR/captures/<key>/rev-0001/          bundle.zip, manifest.json, files/<every file>
        DATA_DIR/incoming/<upload id>/             meta.json + part-000000 ... until complete
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.RLock()
        for d in ("captures", "incoming"):
            os.makedirs(os.path.join(cfg.data, d), exist_ok=True)
        self.cleanup()

    # --- paths ---
    def _incoming(self, uid: str) -> str:
        if not ID_RE.match(uid or ""):
            raise ApiError(404, "no such upload")
        return os.path.join(self.cfg.data, "incoming", uid)

    def _capture(self, key: str) -> str:
        if not KEY_RE.match(key or ""):
            raise ApiError(404, "no such capture")
        return os.path.join(self.cfg.data, "captures", key)

    def index(self) -> list[dict]:
        return _read_json(os.path.join(self.cfg.data, "index.json"), []) or []

    def _save_index(self, entries: list[dict]):
        _write_json(os.path.join(self.cfg.data, "index.json"), entries)

    # --- housekeeping ---
    def cleanup(self, now: float | None = None):
        """Delete unfinished uploads older than INCOMING_DAYS and, when KEEP_DAYS is set,
        captures last uploaded longer ago than that. Runs at start and every hour."""
        now = time.time() if now is None else now
        self.purge_incoming(now)
        if self.cfg.keep_days > 0:
            cutoff = now - self.cfg.keep_days * 86400
            for e in self.index():
                if (e.get("uploaded") or now) < cutoff:
                    try:
                        self.delete(e.get("key"))
                    except ApiError:
                        pass

    def purge_incoming(self, now: float | None = None):
        root = os.path.join(self.cfg.data, "incoming")
        cutoff = (time.time() if now is None else now) - self.cfg.incoming_days * 86400
        for uid in os.listdir(root):
            p = os.path.join(root, uid)
            try:
                if os.path.getmtime(p) < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass

    def delete(self, key: str) -> dict:
        """Remove a capture (every revision) and its index entry."""
        with self.lock:
            cdir = self._capture(key)
            idx = self.index()
            if not os.path.isdir(cdir) and not any(e.get("key") == key for e in idx):
                raise ApiError(404, "no such capture")
            shutil.rmtree(cdir, ignore_errors=True)
            self._save_index([e for e in idx if e.get("key") != key])
        return {"deleted": key}

    # --- uploading ---

    def create(self, body: dict, who: str) -> dict:
        key, size, sha = body.get("key"), body.get("size"), str(body.get("sha256") or "").lower()
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise ApiError(400, "key must be 1-160 letters, digits, dots, dashes or underscores")
        if not isinstance(size, int) or size <= 0:
            raise ApiError(400, "size must be a positive integer")
        if size > self.cfg.max_upload:
            raise ApiError(413, f"too big: this server takes up to {self.cfg.max_upload // 1048576} MB")
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ApiError(400, "sha256 must be 64 hex digits")
        part = int(body.get("part_size") or 8 * 1048576)
        part = max(64 * 1024, min(part, self.cfg.max_part))
        manifest = body.get("manifest") if isinstance(body.get("manifest"), dict) else {}
        with self.lock:
            root = os.path.join(self.cfg.data, "incoming")
            for uid in os.listdir(root):        # the same upload again: resume it
                meta = _read_json(os.path.join(root, uid, "meta.json"), {})
                if meta.get("key") == key and meta.get("sha256") == sha and meta.get("size") == size:
                    return self.describe(uid)
            uid = uuid.uuid4().hex
            os.makedirs(os.path.join(root, uid))
            _write_json(os.path.join(root, uid, "meta.json"), {
                "key": key, "size": size, "sha256": sha, "part_size": part, "manifest": manifest,
                "created": time.time(), "by": who})
        return self.describe(uid)

    def describe(self, uid: str) -> dict:
        d = self._incoming(uid)
        meta = _read_json(os.path.join(d, "meta.json"))
        if meta is None:
            raise ApiError(404, "no such upload")
        received = sorted(int(n[5:]) for n in os.listdir(d) if re.fullmatch(r"part-\d{6}", n))
        parts = -(-meta["size"] // meta["part_size"])
        return {"upload_id": uid, "key": meta["key"], "size": meta["size"], "part_size": meta["part_size"],
                "parts": parts, "received": received}

    def put_part(self, uid: str, n: int, data: bytes, sha: str | None) -> dict:
        info = self.describe(uid)
        if not 0 <= n < info["parts"]:
            raise ApiError(400, f"part {n} is out of range (0-{info['parts'] - 1})")
        expect = min(info["part_size"], info["size"] - n * info["part_size"])
        if len(data) != expect:
            raise ApiError(400, f"part {n} should be {expect} bytes, got {len(data)}")
        if sha and hashlib.sha256(data).hexdigest() != sha.lower():
            raise ApiError(422, f"part {n} arrived damaged (checksum mismatch): send it again")
        d = self._incoming(uid)
        tmp = os.path.join(d, f".part-{n:06d}.{uuid.uuid4().hex[:8]}")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(d, f"part-{n:06d}"))
        os.utime(d)
        return self.describe(uid)

    def complete(self, uid: str) -> dict:
        with self.lock:
            info = self.describe(uid)
            missing = sorted(set(range(info["parts"])) - set(info["received"]))
            if missing:
                raise ApiError(409, f"parts still missing: {missing[:20]}")
            d = self._incoming(uid)
            meta = _read_json(os.path.join(d, "meta.json"))
            bundle = os.path.join(d, "bundle.zip")
            with open(bundle, "wb") as out:
                for n in range(info["parts"]):
                    with open(os.path.join(d, f"part-{n:06d}"), "rb") as f:
                        shutil.copyfileobj(f, out, 1 << 20)
            if _sha256_file(bundle) != meta["sha256"]:
                shutil.rmtree(d, ignore_errors=True)
                raise ApiError(422, "the assembled file doesn't match its checksum: upload it again")
            key = meta["key"]
            cdir = self._capture(key)
            os.makedirs(cdir, exist_ok=True)
            revs = sorted(int(n[4:]) for n in os.listdir(cdir) if re.fullmatch(r"rev-\d{4}", n))
            rev = (revs[-1] + 1) if revs else 1
            rdir = os.path.join(cdir, f"rev-{rev:04d}")
            shutil.rmtree(rdir + ".tmp", ignore_errors=True)      # left by a crash part-way through
            files = self._extract(bundle, os.path.join(rdir + ".tmp", "files"))
            os.replace(bundle, os.path.join(rdir + ".tmp", "bundle.zip"))
            entry = {**meta["manifest"], "key": key, "revision": rev, "uploaded": time.time(),
                     "size": meta["size"], "sha256": meta["sha256"], "files": files, "uploaded_by": meta.get("by", "")}
            _write_json(os.path.join(rdir + ".tmp", "manifest.json"), entry)
            os.replace(rdir + ".tmp", rdir)
            for old in revs[:max(0, len(revs) + 1 - max(1, self.cfg.keep + 1))]:
                shutil.rmtree(os.path.join(cdir, f"rev-{old:04d}"), ignore_errors=True)
            shutil.rmtree(d, ignore_errors=True)
            idx = [e for e in self.index() if e.get("key") != key]
            self._save_index([_summary(entry)] + idx)
        return {"key": key, "revision": rev, "url": self.url(key), "files": len(files)}

    def _extract(self, bundle: str, dest: str) -> list[dict]:
        try:
            z = zipfile.ZipFile(bundle)
        except zipfile.BadZipFile as e:
            raise ApiError(422, "not a zip file") from e
        with z:
            members = [m for m in z.infolist() if not m.is_dir()]
            if len(members) > MAX_MEMBERS:
                raise ApiError(422, "too many files in the bundle")
            total = sum(m.file_size for m in members)
            if total > max(self.cfg.max_upload, os.path.getsize(bundle) * MAX_EXTRACT_RATIO):
                raise ApiError(422, "the bundle unpacks to more than this server takes")
            os.makedirs(dest, exist_ok=True)
            out = []
            for m in members:
                name = m.filename
                if "/" in name or "\\" in name or not FILE_RE.match(name):
                    raise ApiError(422, f"unexpected file name in the bundle: {name!r}")
                with z.open(m) as src, open(os.path.join(dest, name), "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                out.append({"name": name, "size": m.file_size})
        return sorted(out, key=lambda f: f["name"])

    # --- reading ---
    def url(self, key: str) -> str:
        return f"{self.cfg.public_url}/api/captures/{quote(key)}"

    def latest_dir(self, key: str) -> str:
        cdir = self._capture(key)
        try:
            revs = sorted(n for n in os.listdir(cdir) if re.fullmatch(r"rev-\d{4}", n))
        except OSError:
            revs = []
        if not revs:
            raise ApiError(404, "no such capture")
        return os.path.join(cdir, revs[-1])

    def capture(self, key: str) -> dict:
        d = self.latest_dir(key)
        m = _read_json(os.path.join(d, "manifest.json"), {})
        cdir = os.path.dirname(d)
        m["revisions"] = sorted(int(n[4:]) for n in os.listdir(cdir) if re.fullmatch(r"rev-\d{4}", n))
        m["links"] = {"readme": f"{self.url(key)}/files/README.md", "bundle": f"{self.url(key)}/bundle.zip",
                      "files": f"{self.url(key)}/files/<name>"}
        return m

    def file_path(self, key: str, name: str) -> str:
        if name == "bundle.zip":
            return os.path.join(self.latest_dir(key), "bundle.zip")
        if not FILE_RE.match(name or ""):
            raise ApiError(404, "no such file")
        p = os.path.join(self.latest_dir(key), "files", name)
        if not os.path.isfile(p):
            raise ApiError(404, "no such file")
        return p


class Profiles:
    """Machine profiles sent in by units: ``data/profiles/<name>/rev-NNNN.json`` and an index.
    A profile is whatever JSON object the app sends (it must have a "name"); every different
    upload of one name is kept as a new revision."""

    def __init__(self, cfg: Config):
        self.root = os.path.join(cfg.data, "profiles")
        self.lock = threading.Lock()
        os.makedirs(self.root, exist_ok=True)

    def _index_path(self) -> str:
        return os.path.join(self.root, "index.json")

    def index(self) -> list[dict]:
        return _read_json(self._index_path(), [])

    def put(self, name: str, doc: dict, who: str, device: str) -> dict:
        if not KEY_RE.match(name):
            raise ApiError(400, "bad profile name")
        if not isinstance(doc, dict) or not str(doc.get("name") or "").strip():
            raise ApiError(400, "a profile is a JSON object with a 'name'")
        raw = json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=True).encode()
        sha = hashlib.sha256(raw).hexdigest()
        with self.lock:
            entries = self.index()
            old = next((e for e in entries if e["key"] == name), None)
            if old and old.get("sha256") == sha:
                return {**old, "unchanged": True}
            rev = (old["revision"] if old else 0) + 1
            folder = os.path.join(self.root, name)
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, f"rev-{rev:04d}.json"), "wb") as f:
                f.write(raw)
            observed = doc.get("observed") if isinstance(doc.get("observed"), dict) else {}
            entry = {"key": name, "name": str(doc["name"]), "revision": rev, "uploaded": time.time(), "sha256": sha,
                     "size": len(raw), "by": who, "device": device[:64], "protocol": str(doc.get("protocol") or ""),
                     "extends": str(doc.get("extends") or ""), "signals": len(doc.get("signals") or []),
                     "observed": bool(observed), "machine": observed.get("machine") or {}}
            _write_json(self._index_path(), [entry] + [e for e in entries if e["key"] != name])
            return entry

    def get(self, name: str, rev: int | None = None) -> str:
        if not KEY_RE.match(name):
            raise ApiError(400, "bad profile name")
        entry = next((e for e in self.index() if e["key"] == name), None)
        if entry is None:
            raise ApiError(404, "no such profile")
        path = os.path.join(self.root, name, f"rev-{rev or entry['revision']:04d}.json")
        if not os.path.isfile(path):
            raise ApiError(404, "no such revision")
        return path


SERIAL_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
INSTALL_RE = re.compile(r"^[0-9a-f]{8,64}$")
MACHINE_RE = re.compile(r"^[0-9a-f]{0,64}$")
INVITE_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def serial(install: str) -> str:
    """The install's serial as the app shows it (canopener/core/install.py: keep the two alike)."""
    hexpart = install[:10] if re.fullmatch(r"[0-9a-f]{10,}", install) else \
        hashlib.sha256(install.encode()).hexdigest()[:10]
    n = int(hexpart, 16)
    chars = "".join(SERIAL_ALPHABET[(n >> (5 * i)) & 31] for i in range(7, -1, -1))
    return f"CO-{chars[:4]}-{chars[4:]}"


def _invite_code() -> str:
    raw = "".join(SERIAL_ALPHABET[b & 31] for b in os.urandom(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def _code_hash(code: str) -> str:
    norm = re.sub(r"[\s-]", "", (code or "").upper()).translate(str.maketrans("OIL", "011"))
    return hashlib.sha256(norm.encode()).hexdigest()


class Team:
    """Team access: which installs may download the team plugin, and the plugin itself::

        DATA_DIR/team/installs.json   {install id: {serial, name, status, key_sha256, machine, ...}}
        DATA_DIR/team/invites.json    {invite id: {code_sha256, name, expires, used_by, ...}}
        DATA_DIR/team/bundle.zip      the plugin as last published, and pack.json describing it

    An install's key and an invite's code are kept only as hashes. Status is pending (asked, not
    yet approved), approved or revoked."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = os.path.join(cfg.data, "team")
        self.lock = threading.Lock()
        os.makedirs(self.root, exist_ok=True)

    def _path(self, name: str) -> str:
        return os.path.join(self.root, name)

    def _installs(self) -> dict:
        return _read_json(self._path("installs.json"), {}) or {}

    def _invites(self) -> dict:
        return _read_json(self._path("invites.json"), {}) or {}

    def pack(self) -> dict | None:
        p = _read_json(self._path("pack.json"))
        return p if p and os.path.isfile(self._path("bundle.zip")) else None

    @staticmethod
    def _public(iid: str, i: dict) -> dict:
        return {"install": iid, **{k: v for k, v in i.items() if k != "key_sha256"}}

    @staticmethod
    def _fields(body: dict) -> tuple[str, str, str, str]:
        iid = str(body.get("install") or "")
        key = str(body.get("key") or "")
        machine = str(body.get("machine") or "")
        name = " ".join(str(body.get("name") or "").split())[:80]
        if not INSTALL_RE.match(iid) or not (32 <= len(key) <= 200) or not MACHINE_RE.match(machine):
            raise ApiError(400, "needs the install's id, its key and (optionally) its machine fingerprint")
        return iid, key, machine, name

    # --- the app's side ---
    def redeem(self, body: dict) -> dict:
        iid, key, machine, name = self._fields(body)
        h = _code_hash(str(body.get("code") or ""))
        now = time.time()
        with self.lock:
            invites = self._invites()
            inv_id, inv = next(((k, v) for k, v in invites.items() if hmac.compare_digest(v["code_sha256"], h)),
                               (None, None))
            if inv is None or inv.get("used_by") or inv["expires"] < now:
                raise ApiError(404, "that invite isn't valid: it's been used already, it has expired or it was "
                                    "mistyped. Ask for a new one.")
            inv.update(used_by=iid, used=now)
            _write_json(self._path("invites.json"), invites)
            installs = self._installs()
            i = installs.get(iid, {"created": now})
            i.update(serial=serial(iid), name=inv.get("name") or name or i.get("name", ""), status="approved",
                     key_sha256=hashlib.sha256(key.encode()).hexdigest(), machine=machine, via=f"invite {inv_id}",
                     approved=now, last_seen=now)
            installs[iid] = i
            _write_json(self._path("installs.json"), installs)
            return self._public(iid, i)

    def request(self, body: dict) -> dict:
        iid, key, machine, name = self._fields(body)
        now = time.time()
        with self.lock:
            installs = self._installs()
            old = installs.get(iid)
            if old and old.get("status") == "approved" and \
                    hmac.compare_digest(old["key_sha256"], hashlib.sha256(key.encode()).hexdigest()):
                return self._public(iid, old)
            if old and old.get("status") == "approved":
                raise ApiError(409, "this install is already approved with another key; ask for an invite")
            pending = sum(1 for k, v in installs.items() if v.get("status") == "pending" and k != iid)
            if pending >= self.cfg.team_pending:
                raise ApiError(429, "this server isn't taking requests just now; ask for an invite link instead")
            i = old or {"created": now}
            i.update(serial=serial(iid), name=name or i.get("name", ""), status="pending", requested=now,
                     key_sha256=hashlib.sha256(key.encode()).hexdigest(), machine=machine, via="request")
            installs[iid] = i
            _write_json(self._path("installs.json"), installs)
            return self._public(iid, i)

    def authenticate(self, iid: str, key: bytes, machine: str) -> dict:
        """The install the headers name, if its key matches (else 401 / 403 with a status the app
        acts on: unknown, other_computer)."""
        i = self._installs().get(iid) if INSTALL_RE.match(iid or "") else None
        if i is None or not key or not hmac.compare_digest(i["key_sha256"], hashlib.sha256(key).hexdigest()):
            raise TeamDenied(401, "this server doesn't know this computer", "unknown")
        if i.get("machine") and machine and machine != i["machine"]:
            raise TeamDenied(403, "this looks like a different computer from the one that joined", "other_computer")
        return i

    def me(self, iid: str, key: bytes, machine: str, app_version: str, pack_version: str) -> dict:
        with self.lock:
            self.authenticate(iid, key, machine)
            installs = self._installs()
            i = installs[iid]
            i.update(last_seen=time.time(), app_version=app_version[:40], pack_version=pack_version[:80])
            _write_json(self._path("installs.json"), installs)
        pack = self.pack() if i["status"] == "approved" else None
        return {**self._public(iid, i), "pack": {k: pack[k] for k in ("version", "sha256", "size")} if pack else None}

    def bundle(self, iid: str, key: bytes, machine: str) -> str:
        i = self.authenticate(iid, key, machine)
        if i.get("status") != "approved":
            raise ApiError(403, "this computer isn't approved for the team plugin")
        if not self.pack():
            raise ApiError(404, "no team plugin has been published yet")
        return self._path("bundle.zip")

    # --- the owner's side ---
    def installs(self) -> list[dict]:
        rows = [self._public(k, v) for k, v in self._installs().items()]
        return sorted(rows, key=lambda r: -(r.get("last_seen") or r.get("requested") or r.get("created") or 0))

    def update(self, iid: str, body: dict) -> dict:
        with self.lock:
            installs = self._installs()
            i = installs.get(iid)
            if i is None:
                raise ApiError(404, "no such install")
            if "status" in body:
                if body["status"] not in ("approved", "revoked"):
                    raise ApiError(400, "status is approved or revoked")
                i["status"] = body["status"]
                i[body["status"]] = time.time()
            if "name" in body:
                i["name"] = " ".join(str(body["name"] or "").split())[:80]
            _write_json(self._path("installs.json"), installs)
            return self._public(iid, i)

    def forget(self, iid: str) -> dict:
        with self.lock:
            installs = self._installs()
            if installs.pop(iid, None) is None:
                raise ApiError(404, "no such install")
            _write_json(self._path("installs.json"), installs)
            return {"forgotten": iid}

    def invite(self, body: dict, base: str) -> dict:
        name = " ".join(str(body.get("name") or "").split())[:80]
        try:
            days = min(90.0, max(0.01, float(body.get("days") or 7)))
        except (TypeError, ValueError):
            raise ApiError(400, "days must be a number") from None
        code = _invite_code()
        now = time.time()
        with self.lock:
            invites = self._invites()
            for k in [k for k, v in invites.items() if v["expires"] < now - 30 * 86400]:
                del invites[k]          # long-expired ones are just clutter
            inv_id = os.urandom(4).hex()
            invites[inv_id] = {"code_sha256": _code_hash(code), "name": name, "created": now,
                               "expires": now + days * 86400, "used_by": "", "used": 0}
            _write_json(self._path("invites.json"), invites)
        return {"id": inv_id, "code": code, "link": f"{base}/invite/{code}", "name": name,
                "expires": now + days * 86400}

    def invites(self) -> list[dict]:
        return sorted(({"id": k, **{f: v for f, v in inv.items() if f != "code_sha256"}}
                       for k, inv in self._invites().items()), key=lambda r: -r["created"])

    def uninvite(self, inv_id: str) -> dict:
        with self.lock:
            invites = self._invites()
            if not INVITE_ID_RE.match(inv_id) or invites.pop(inv_id, None) is None:
                raise ApiError(404, "no such invite")
            _write_json(self._path("invites.json"), invites)
            return {"deleted": inv_id}

    def publish(self, data: bytes, version: str, who: str) -> dict:
        import io
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
        except zipfile.BadZipFile:
            raise ApiError(400, "the team plugin must be a zip file") from None
        if not any(n == "pyproject.toml" or (n.count("/") == 1 and n.endswith("/pyproject.toml")) for n in names):
            raise ApiError(400, "the zip has no pyproject.toml (at the top, or in one top folder)")
        version = " ".join(version.split())[:80] or time.strftime("%Y-%m-%d.%H%M")
        with self.lock:
            tmp = self._path("bundle.zip.tmp")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, self._path("bundle.zip"))
            pack = {"version": version, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                    "published": time.time(), "by": who}
            _write_json(self._path("pack.json"), pack)
            return pack


class TeamDenied(ApiError):
    """An install that isn't (or is no longer) let in: the app removes the team plugin."""

    def __init__(self, status: int, text: str, state: str):
        super().__init__(status, text)
        self.state = state


INVITE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>CANopener invite</title><style>body{font:16px/1.5 system-ui,sans-serif;max-width:34em;margin:2em auto;
padding:0 1em}code{background:#eee;padding:.1em .3em;border-radius:3px;word-break:break-all}</style></head><body>
<h1>CANopener team access</h1>
<p>This is an invite for the CANopener app. To use it:</p>
<ol><li>Copy this page's address from the address bar.</li>
<li>Open CANopener and choose <b>Help &rarr; Team access&hellip;</b></li>
<li>Paste the address into <b>Invite link</b> and click <b>Join</b>.</li></ol>
<p>An invite works once, on one computer, and expires after a few days.</p>
</body></html>
"""


# --- the admin page (GET /admin): team access from a browser or a phone, with the admin token -----------
# The token is pasted in, kept in the tab's sessionStorage only and sent as Authorization: Bearer to
# the /api/team calls above; it's never in a URL, a cookie or this file. Everything the page needs is
# served from here (no CDNs, fonts or inline script), under a strict Content-Security-Policy.

ADMIN_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

ADMIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="robots" content="noindex">
<title>Team access</title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="/admin/app.css">
<script src="/admin/app.js" defer></script>
</head><body>
<header>
  <h1>CANopener team access</h1>
  <div id="bar" hidden><button type="button" id="refresh">Refresh</button>
  <button type="button" id="signout">Sign out</button></div>
</header>
<p id="msg" role="status" aria-live="polite" hidden></p>

<main id="login" hidden>
  <form id="loginform" autocomplete="off">
    <h2>Sign in</h2>
    <p>Paste this server's admin token (one of <code>ADMIN_TOKENS</code> in its <code>.env</code>).
    It stays in this browser tab only and is forgotten when you close the tab or sign out.</p>
    <p id="nothttps" class="warn" hidden>This page isn't on https: don't paste the token here.</p>
    <label>Admin token <input type="password" id="token" required spellcheck="false"></label>
    <button type="submit" class="primary">Sign in</button>
  </form>
</main>

<main id="app" hidden>
  <section>
    <h2>Waiting for approval</h2>
    <p class="hint">Computers that asked for access from the app (<b>Ask for access</b>). Check the
    serial with the person before approving.</p>
    <div id="requests" class="cards"></div>
  </section>

  <section>
    <h2>Computers</h2>
    <div id="installs" class="cards"></div>
  </section>

  <section>
    <h2>Invite someone</h2>
    <form id="inviteform" class="row">
      <label>Name <input id="invname" maxlength="80" placeholder="Dave, van 3"></label>
      <label>Valid for (days) <input id="invdays" type="number" min="1" max="90" step="1" value="7"></label>
      <button type="submit" class="primary">Make invite link</button>
    </form>
    <div id="newinvite" class="card" hidden>
      <p>Send this link to <b id="newname"></b>. It works once, on one computer, until
      <span id="newexpires"></span>. It's only shown now.</p>
      <div class="row"><input id="newlink" readonly aria-label="Invite link">
      <button type="button" id="copy">Copy</button></div>
    </div>
    <h3>Invites</h3>
    <div id="invites" class="cards"></div>
  </section>

  <section>
    <h2>Team plugin</h2>
    <p id="pack"></p>
    <form id="packform" class="row">
      <label>Plugin zip <input id="packfile" type="file" accept=".zip,application/zip" required></label>
      <label>Version <input id="packversion" maxlength="80" placeholder="blank: today's date"></label>
      <button type="submit" class="primary">Publish</button>
    </form>
    <p class="hint">A zip of the plugin's repository, with <code>pyproject.toml</code> at the top or in
    one top folder (<code>canopener-cli team-admin publish FOLDER</code> makes one from a git checkout).
    Approved computers are offered it at their next start, and install it only after a yes.</p>
  </section>
</main>

<dialog id="ask">
  <p id="asktext"></p>
  <input id="askinput" maxlength="80" hidden aria-label="Name">
  <div class="row end"><button type="button" id="askno">No</button>
  <button type="button" id="askyes">Yes</button></div>
</dialog>
</body></html>
"""

ADMIN_CSS = """:root {
  --bg: #f6f7f9; --card: #ffffff; --text: #1b1f24; --dim: #5b6470; --line: #d8dde3;
  --accent: #1f6feb; --accent-text: #ffffff; --danger: #c0362c; --ok: #1a7f37; --warn: #9a6700;
  --pending: #8250df;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1216; --card: #181c22; --text: #e6e9ed; --dim: #9aa4b1; --line: #2c333c;
    --accent: #4c8dff; --accent-text: #0b0d10; --danger: #ff6b5e; --ok: #4ac26b; --warn: #d4a72c;
    --pending: #b393ff;
  }
}
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 60rem; padding: 0 16px 3rem; background: var(--bg); color: var(--text);
  font: 16px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { display: flex; flex-wrap: wrap; gap: .5rem 1rem; align-items: center; justify-content: space-between;
  padding: 1rem 0; border-bottom: 1px solid var(--line); }
h1 { font-size: 1.3rem; margin: 0; }
h2 { font-size: 1.1rem; margin: 2rem 0 .4rem; }
h3 { font-size: 1rem; margin: 1.4rem 0 .4rem; }
code { font-size: .9em; background: var(--line); padding: .05em .3em; border-radius: 3px; }
.hint { color: var(--dim); font-size: .92rem; margin: .2rem 0 .8rem; }
.warn { color: var(--danger); font-weight: 600; }
#msg { padding: .6rem .8rem; border-radius: 6px; background: var(--card); border: 1px solid var(--line); }
#msg.error { border-color: var(--danger); color: var(--danger); }
label { display: flex; flex-direction: column; gap: .2rem; font-size: .9rem; color: var(--dim); }
input { font: inherit; color: var(--text); background: var(--card); border: 1px solid var(--line);
  border-radius: 6px; padding: .45rem .6rem; min-width: 0; }
input[type=file] { padding: .3rem; }
button { font: inherit; color: var(--text); background: var(--card); border: 1px solid var(--line);
  border-radius: 6px; padding: .45rem .9rem; cursor: pointer; min-height: 2.5rem; }
button:hover { border-color: var(--accent); }
button:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
button.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-text); }
button.danger { color: var(--danger); }
button:disabled { opacity: .5; cursor: default; }
.row { display: flex; flex-wrap: wrap; gap: .6rem; align-items: flex-end; }
.row > input { flex: 1 1 12rem; }
.row label input { width: 100%; }
.row label { flex: 1 1 12rem; }
.row.end { justify-content: flex-end; }
#loginform { max-width: 30rem; display: flex; flex-direction: column; gap: .8rem; }
.cards { display: grid; gap: .6rem; grid-template-columns: repeat(auto-fill, minmax(17rem, 1fr)); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: .8rem; }
.card .top { display: flex; justify-content: space-between; gap: .5rem; align-items: baseline; }
.serial { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-weight: 600; }
.name { margin: .15rem 0 .5rem; overflow-wrap: anywhere; }
.badge { font-size: .8rem; font-weight: 600; text-transform: uppercase; letter-spacing: .03em; }
.approved { color: var(--ok); } .revoked { color: var(--danger); } .pending { color: var(--pending); }
.open { color: var(--ok); } .used, .expired { color: var(--dim); }
dl { display: grid; grid-template-columns: auto 1fr; gap: .1rem .8rem; margin: 0 0 .7rem; font-size: .9rem; }
dt { color: var(--dim); } dd { margin: 0; overflow-wrap: anywhere; }
.card .row { gap: .4rem; }
.card button { padding: .35rem .7rem; }
.empty { color: var(--dim); font-style: italic; margin: .2rem 0; }
#newinvite { margin-top: .8rem; }
#newlink { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: .9rem; }
dialog { background: var(--card); color: var(--text); border: 1px solid var(--line); border-radius: 10px;
  max-width: min(28rem, calc(100vw - 32px)); padding: 1rem; }
dialog::backdrop { background: rgba(0, 0, 0, .45); }
dialog input { width: 100%; margin-bottom: .8rem; }
"""

ADMIN_JS = r""""use strict";
// CANopener team access admin page. Talks to /api/team/* with the admin token as a Bearer header.
(function () {
  const KEY = "canopener-team-admin";
  const $ = (id) => document.getElementById(id);
  let token = "";
  let installsById = {};

  function store(t) {
    token = t || "";
    try {
      if (token) sessionStorage.setItem(KEY, token); else sessionStorage.removeItem(KEY);
    } catch (e) { /* private mode: keep it in memory for this page only */ }
  }
  try { token = sessionStorage.getItem(KEY) || ""; } catch (e) { token = ""; }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function button(text, onClick, cls) {
    const b = el("button", cls || "", text);
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  }

  function say(text, isError) {
    const m = $("msg");
    m.textContent = text || "";
    m.className = isError ? "error" : "";
    m.hidden = !text;
  }

  function ago(t) {
    if (!t) return "never";
    const s = Math.max(0, Date.now() / 1000 - t);
    if (s < 90) return "just now";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 129600) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " days ago";
  }
  function when(t) { return t ? new Date(t * 1000).toLocaleString() : ""; }
  function size(n) {
    return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " kB";
  }

  class Refused extends Error {
    constructor(status, text) { super(text); this.status = status; }
  }

  async function api(method, path, body, headers) {
    const h = Object.assign({ "Authorization": "Bearer " + token }, headers || {});
    let data;
    if (body instanceof Blob) data = body;
    else if (body !== undefined) { h["Content-Type"] = "application/json"; data = JSON.stringify(body); }
    let r;
    try {
      r = await fetch(path, { method: method, headers: h, body: data, cache: "no-store",
                              credentials: "omit", referrerPolicy: "no-referrer" });
    } catch (e) {
      throw new Refused(0, "Couldn't reach the server. Check the connection and try again.");
    }
    let out = {};
    try { out = await r.json(); } catch (e) { out = {}; }
    if (r.status === 401 || r.status === 403) {
      signOut("That token isn't an admin token for this server (or it was changed). Sign in again.");
      throw new Refused(r.status, "");
    }
    if (!r.ok) throw new Refused(r.status, out.error || ("The server answered " + r.status + "."));
    return out;
  }

  // A question with No as the default (Escape, or Enter straight away, answers No).
  function ask(text, yes, opts) {
    opts = opts || {};
    const d = $("ask"), inp = $("askinput"), no = $("askno"), ok = $("askyes");
    $("asktext").textContent = text;
    ok.textContent = yes || "Yes";
    ok.className = opts.danger ? "danger" : "primary";
    no.textContent = opts.no || "No";
    inp.hidden = opts.input === undefined;
    inp.value = opts.input || "";
    return new Promise((resolve) => {
      function done(v) {
        ok.removeEventListener("click", onYes);
        no.removeEventListener("click", onNo);
        d.removeEventListener("cancel", onCancel);
        inp.removeEventListener("keydown", onKey);
        if (d.open) d.close();
        resolve(v);
      }
      function onYes() { done(opts.input === undefined ? true : inp.value); }
      function onNo() { done(null); }
      function onCancel(e) { e.preventDefault(); done(null); }
      function onKey(e) { if (e.key === "Enter") { e.preventDefault(); onYes(); } }
      ok.addEventListener("click", onYes);
      no.addEventListener("click", onNo);
      d.addEventListener("cancel", onCancel);
      inp.addEventListener("keydown", onKey);
      d.showModal();
      (opts.input === undefined ? no : inp).focus();
    });
  }

  async function act(fn, done) {
    try {
      await fn();
      if (done) say(done);
      await load();
    } catch (e) {
      if (e.message) say(e.message, true);
    }
  }

  function installCard(i) {
    const c = el("div", "card");
    const top = el("div", "top");
    top.append(el("span", "serial", i.serial || i.install.slice(0, 8)));
    top.append(el("span", "badge " + i.status, i.status === "pending" ? "waiting" : i.status));
    c.append(top);
    c.append(el("div", "name", i.name || "(no name)"));
    const dl = el("dl");
    function row(k, v) { dl.append(el("dt", "", k)); const dd = el("dd", "", v); dl.append(dd); return dd; }
    if (i.status === "pending") row("Asked", ago(i.requested)).title = when(i.requested);
    row("Plugin", i.pack_version || "none yet");
    row("Last check", ago(i.last_seen)).title = when(i.last_seen);
    row("App", i.app_version || "unknown");
    row("Joined by", i.via ? i.via.replace(/^invite .*/, "invite") : "unknown");
    c.append(dl);
    const who = (i.serial || "this computer") + (i.name ? " (" + i.name + ")" : "");
    const path = "/api/team/installs/" + encodeURIComponent(i.install);
    const btns = el("div", "row");
    if (i.status !== "approved") {
      btns.append(button("Approve", () => act(() => api("POST", path, { status: "approved" }),
                                               who + " is approved. It gets the plugin at its next check."),
                         "primary"));
    } else {
      btns.append(button("Revoke", async () => {
        if (await ask("Revoke " + who + "? The team plugin is removed from it the next time it starts online, " +
                      "without asking. Approve undoes this.", "Revoke", { danger: true })) {
          act(() => api("POST", path, { status: "revoked" }), who + " is revoked.");
        }
      }, "danger"));
    }
    btns.append(button("Rename", async () => {
      const name = await ask("New name for " + (i.serial || "this computer") + ":", "Save",
                             { input: i.name || "", no: "Cancel" });
      if (name !== null) act(() => api("POST", path, { name: name }), "Renamed.");
    }));
    btns.append(button("Forget", async () => {
      if (await ask("Forget " + who + "? It's removed from this list and loses the team plugin at its next " +
                    "check. To let it back in it needs a new invite or request.", "Forget", { danger: true })) {
        act(() => api("DELETE", path), who + " is forgotten.");
      }
    }, "danger"));
    c.append(btns);
    return c;
  }

  function inviteCard(v) {
    const now = Date.now() / 1000;
    const state = v.used_by ? "used" : (v.expires < now ? "expired" : "open");
    const c = el("div", "card");
    const top = el("div", "top");
    top.append(el("span", "", v.name || "(no name)"));
    top.append(el("span", "badge " + state, state));
    c.append(top);
    const dl = el("dl");
    function row(k, val) { dl.append(el("dt", "", k)); dl.append(el("dd", "", val)); }
    row("Made", when(v.created));
    if (state === "used") {
      const by = installsById[v.used_by];
      row("Used by", (by && by.serial ? by.serial : "a computer since forgotten") + ", " + ago(v.used));
    } else {
      row(state === "open" ? "Expires" : "Expired", when(v.expires));
    }
    c.append(dl);
    if (state === "open") {
      const btns = el("div", "row");
      btns.append(button("Cancel invite", () => act(
        () => api("DELETE", "/api/team/invites/" + encodeURIComponent(v.id)), "Invite cancelled."), "danger"));
      c.append(btns);
    }
    return c;
  }

  function fill(box, items, make, emptyText) {
    box.replaceChildren();
    if (!items.length) box.append(el("p", "empty", emptyText));
    for (const it of items) box.append(make(it));
  }

  async function load() {
    const [ins, inv, pack] = await Promise.all([
      api("GET", "/api/team/installs"), api("GET", "/api/team/invites"), api("GET", "/api/team/pack")]);
    const all = ins.installs || [];
    installsById = {};
    for (const i of all) installsById[i.install] = i;
    fill($("requests"), all.filter((i) => i.status === "pending"), installCard, "Nobody is waiting.");
    fill($("installs"), all.filter((i) => i.status !== "pending"), installCard,
         "No computers yet: invite someone below.");
    fill($("invites"), inv.invites || [], inviteCard, "No invites.");
    $("pack").textContent = pack && pack.version
      ? "Published: " + pack.version + ", " + size(pack.size || 0) + ", " + when(pack.published) + "."
      : "No team plugin has been published yet.";
  }

  function showLogin() {
    $("app").hidden = true;
    $("bar").hidden = true;
    $("login").hidden = false;
    $("nothttps").hidden = location.protocol === "https:" ||
      ["localhost", "127.0.0.1", "[::1]"].indexOf(location.hostname) >= 0;
    $("token").value = "";
    $("token").focus();
  }

  async function showApp() {
    $("login").hidden = true;
    $("app").hidden = false;
    $("bar").hidden = false;
    try { await load(); } catch (e) { if (e.message) say(e.message, true); }
  }

  function signOut(why) {
    store("");
    $("newinvite").hidden = true;
    $("newlink").value = "";
    for (const id of ["requests", "installs", "invites"]) $(id).replaceChildren();
    say(why || "Signed out.", !!why);
    showLogin();
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("loginform").addEventListener("submit", async (e) => {
      e.preventDefault();
      const t = $("token").value.trim();
      $("token").value = "";
      if (!t) return;
      store(t);
      say("");
      try {
        const me = await api("GET", "/api/whoami");
        if (!me.can_delete) { signOut("That token works here but isn't an admin token."); return; }
      } catch (err) {
        if (err.message) { store(""); say(err.message, true); }
        return;
      }
      showApp();
    });
    $("signout").addEventListener("click", () => signOut(""));
    $("refresh").addEventListener("click", () => { say(""); act(() => Promise.resolve()); });

    $("inviteform").addEventListener("submit", (e) => {
      e.preventDefault();
      const name = $("invname").value.trim();
      const days = Math.min(90, Math.max(1, parseFloat($("invdays").value) || 7));
      act(async () => {
        const r = await api("POST", "/api/team/invites", { name: name, days: days });
        $("newname").textContent = r.name || "them";
        $("newexpires").textContent = when(r.expires);
        $("newlink").value = r.link;
        $("newinvite").hidden = false;
        $("invname").value = "";
      }, "Invite made.");
    });
    $("copy").addEventListener("click", async () => {
      const box = $("newlink");
      try { await navigator.clipboard.writeText(box.value); say("Link copied."); }
      catch (e) { box.focus(); box.select(); say("Select the link and copy it (the browser didn't allow copying)."); }
    });

    $("packform").addEventListener("submit", (e) => {
      e.preventDefault();
      const f = $("packfile").files[0];
      if (!f) return;
      // Header values must be plain text: keep printable ASCII only.
      const version = $("packversion").value.replace(/[^\x20-\x7e]/g, "").trim().slice(0, 80);
      const btn = e.submitter || null;
      if (btn) btn.disabled = true;
      say("Uploading " + f.name + " (" + size(f.size) + ")...");
      act(async () => {
        try {
          const headers = { "Content-Type": "application/zip" };
          if (version) headers["X-Pack-Version"] = version;
          const r = await api("PUT", "/api/team/pack", f, headers);
          $("packfile").value = "";
          $("packversion").value = "";
          say("Published " + r.version + ".");
        } finally { if (btn) btn.disabled = false; }
      });
    });

    if (token) showApp(); else showLogin();
  });
})();
"""

ADMIN_FILES = {
    "": (ADMIN_PAGE, "text/html; charset=utf-8"),
    "app.js": (ADMIN_JS, "text/javascript; charset=utf-8"),
    "app.css": (ADMIN_CSS, "text/css; charset=utf-8"),
}


PUBKEY_RE = re.compile(r"^ssh-ed25519 [A-Za-z0-9+/]{60,100}={0,2}$")


class Tunnels:
    """Which units may open an SSH tunnel, and on which port each one's far end listens.
    ``data/tunnel/authorized_keys`` is read by the tunnel container's sshd: one line per unit, its
    key allowed to do exactly one thing, listen on its own loopback port there (no shell, no
    other forwarding)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = os.path.join(cfg.data, "tunnel")
        self.lock = threading.Lock()
        os.makedirs(self.root, exist_ok=True)

    def enroll(self, body: dict, who: str) -> dict:
        cfg = self.cfg
        if not cfg.tunnel_host:
            raise ApiError(404, "this server has no SSH tunnel set up")
        device = str(body.get("device") or "")
        key = " ".join(str(body.get("public_key") or "").split()[:2])
        if not re.match(r"^[0-9a-f]{8,64}$", device) or not PUBKEY_RE.match(key):
            raise ApiError(400, "needs the unit's id and an ssh-ed25519 public key")
        with self.lock:
            path = os.path.join(self.root, "units.json")
            units = _read_json(path, {})
            unit = units.get(device)
            if unit is None:
                used = {u["listen_port"] for u in units.values()}
                port = next((p for p in range(cfg.tunnel_first, cfg.tunnel_first + cfg.tunnel_units)
                             if p not in used), None)
                if port is None:
                    raise ApiError(429, "no tunnel ports left (TUNNEL_MAX_UNITS)")
                unit = units[device] = {"listen_port": port}
            unit.update(public_key=key, by=who, enrolled=time.time())
            _write_json(path, units)
            lines = [f'restrict,port-forwarding,permitlisten="127.0.0.1:{u["listen_port"]}" {u["public_key"]} '
                     f'unit-{d[:6]}' for d, u in sorted(units.items())]
            tmp = os.path.join(self.root, "authorized_keys.tmp")
            with open(tmp, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.chmod(tmp, 0o644)
            os.replace(tmp, os.path.join(self.root, "authorized_keys"))
            return {"host": cfg.tunnel_host, "port": cfg.tunnel_port, "user": cfg.tunnel_user,
                    "listen_port": unit["listen_port"]}


class Relay:
    """Remote help: a unit in the field opens a session and asks for work; someone with a remote
    token queues commands for it and collects the results. Nothing is stored on disk, a session
    ends when its time is up or either side closes it, and the unit decides what it will do with
    each command (the server only carries them)."""

    MAX_SESSIONS = 8
    MAX_QUEUE = 64
    MAX_RESULT = 12 * 1024 * 1024       # a screenshot or a small file, base64 in JSON

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cond = threading.Condition()
        self.sessions: dict[str, dict] = {}

    def _expire(self):
        now = time.time()
        for sid in [k for k, v in self.sessions.items() if v["expires"] < now]:
            del self.sessions[sid]

    def _get(self, sid: str) -> dict:
        self._expire()
        s = self.sessions.get(sid)
        if s is None:
            raise ApiError(404, "no such session (it may have ended)")
        return s

    @staticmethod
    def _public(s: dict) -> dict:
        return {k: s[k] for k in ("id", "device", "label", "info", "created", "expires", "last_poll", "done")} | {
            "queued": len(s["queue"])}

    def open(self, body: dict, who: str) -> dict:
        minutes = min(float(body.get("minutes") or 30), self.cfg.remote_minutes)
        with self.cond:
            self._expire()
            if len(self.sessions) >= self.MAX_SESSIONS:
                raise ApiError(429, "too many remote sessions are open")
            sid = uuid.uuid4().hex
            now = time.time()
            self.sessions[sid] = {"id": sid, "device": str(body.get("device") or "")[:64], "by": who,
                                  "label": str(body.get("label") or "")[:120], "info": body.get("info") or {},
                                  "created": now, "expires": now + max(1.0, minutes) * 60, "last_poll": 0.0,
                                  "queue": [], "results": {}, "done": 0}
            return self._public(self.sessions[sid])

    def close(self, sid: str) -> dict:
        with self.cond:
            self._get(sid)
            del self.sessions[sid]
            self.cond.notify_all()
            return {"closed": sid}

    def list(self) -> list[dict]:
        with self.cond:
            self._expire()
            return [self._public(s) for s in sorted(self.sessions.values(), key=lambda s: -s["created"])]

    def next(self, sid: str, wait: float) -> dict | None:
        """The unit asks for its next command (waits up to ``wait`` seconds for one)."""
        end = time.time() + max(0.0, min(wait, 25.0))
        with self.cond:
            while True:
                s = self._get(sid)
                s["last_poll"] = time.time()
                if s["queue"]:
                    return s["queue"].pop(0)
                left = end - time.time()
                if left <= 0:
                    return None
                self.cond.wait(left)

    def command(self, sid: str, body: dict, who: str) -> dict:
        action = str(body.get("action") or "")
        if not action or len(action) > 64:
            raise ApiError(400, "a command needs an 'action'")
        with self.cond:
            s = self._get(sid)
            if len(s["queue"]) >= self.MAX_QUEUE:
                raise ApiError(429, "the unit has too many commands waiting")
            cmd = {"id": uuid.uuid4().hex, "action": action, "args": body.get("args") or {}, "by": who,
                   "queued": time.time()}
            s["queue"].append(cmd)
            self.cond.notify_all()
            return {"id": cmd["id"], "session": sid}

    def result(self, sid: str, cid: str, body: dict) -> dict:
        with self.cond:
            s = self._get(sid)
            s["results"][cid] = {"id": cid, "finished": time.time(), "result": body}
            s["done"] += 1
            while len(s["results"]) > self.MAX_QUEUE:
                s["results"].pop(next(iter(s["results"])))
            self.cond.notify_all()
            return {"ok": True}

    def fetch(self, sid: str, cid: str, wait: float) -> dict | None:
        end = time.time() + max(0.0, min(wait, 25.0))
        with self.cond:
            while True:
                s = self._get(sid)
                if cid in s["results"]:
                    return s["results"][cid]
                left = end - time.time()
                if left <= 0:
                    return None
                self.cond.wait(left)


def _summary(m: dict) -> dict:
    keep = ("key", "revision", "uploaded", "size", "name", "machine", "machine_summary", "concern", "question",
            "started", "stopped", "frames", "samples", "timestamps", "undecoded_streams", "streams",
            "device", "app_version", "folder", "connection")
    return {k: m[k] for k in keep if k in m}


def _matches(e: dict, q: str) -> bool:
    hay = " ".join(str(v) for v in [e.get("key"), e.get("name"), e.get("concern"), e.get("question"),
                                    e.get("folder"), e.get("machine_summary"), *(e.get("machine") or {}).values()])
    return all(w in hay.lower() for w in q.lower().split())


class Handler(BaseHTTPRequestHandler):
    server_version = f"CANopenerUpload/{VERSION}"
    store: Store = None         # set by make_server
    profiles: "Profiles" = None
    relay: "Relay" = None
    tunnels: "Tunnels" = None
    team: "Team" = None
    cfg: Config = None
    timeout = 120               # a connection that goes quiet is dropped (make_server sets REQUEST_TIMEOUT)

    # --- plumbing ---
    def log_message(self, fmt, *args):        # path only: never the Authorization header
        status = args[1] if len(args) > 1 else ""
        who = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip() if self.headers else ""
        sys.stderr.write(f"{who or self.address_string()} {self.command} {urlsplit(self.path).path} {status}\n")

    def _send(self, status: int, data=None, ctype: str = "application/json", body: bytes | None = None,
              headers: dict | None = None):
        if body is None:
            body = (json.dumps(data, indent=1) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, path: str, max_bytes: int | None):
        ext = os.path.splitext(path)[1].lower()
        size = os.path.getsize(path)
        n = size if max_bytes is None else min(size, max_bytes)
        self.send_response(200)
        self.send_header("Content-Type", TEXT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(n))
        self.send_header("X-File-Size", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            left = n
            while left > 0:
                chunk = f.read(min(left, 1 << 20))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _body(self, limit: int) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(411, "Content-Length needed") from None
        if n > limit:
            raise ApiError(413, f"request too big (limit {limit // 1048576} MB)")
        data = bytearray()
        while len(data) < n:
            chunk = self.rfile.read(min(n - len(data), 1 << 20))
            if not chunk:
                raise ApiError(400, "the request body was cut off")
            data += chunk
        return bytes(data)

    def _json(self) -> dict:
        try:
            d = json.loads(self._body(MAX_JSON).decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "body must be JSON") from None
        if not isinstance(d, dict):
            raise ApiError(400, "body must be a JSON object")
        return d

    def _rights(self) -> tuple[bool, bool, bool, str]:
        """(may upload, may read, may delete, a short fingerprint of the token)."""
        auth = self.headers.get("Authorization") or ""
        tok = auth[7:].strip().encode("utf-8", "replace") if auth.lower().startswith("bearer ") else b""
        if not tok:
            return False, False, False, ""

        def known(tokens):      # bytes: compare_digest refuses non-ASCII text
            return any(hmac.compare_digest(tok, t.encode()) for t in tokens)
        return (known(self.cfg.upload_tokens), known(self.cfg.read_tokens), known(self.cfg.admin_tokens),
                hashlib.sha256(tok).hexdigest()[:8])

    def _token(self) -> bytes:
        auth = self.headers.get("Authorization") or ""
        return auth[7:].strip().encode("utf-8", "replace") if auth.lower().startswith("bearer ") else b""

    def _need_remote(self) -> str:
        """A remote token: may list remote sessions and send commands to units."""
        tok = self._token()
        if not tok:
            raise ApiError(401, "a valid token is needed (Authorization: Bearer ...)")
        if not any(hmac.compare_digest(tok, t.encode()) for t in self.cfg.remote_tokens):
            raise ApiError(403, "this token can't use remote help")
        return hashlib.sha256(tok).hexdigest()[:8]

    def _need(self, upload: bool = False, read: bool = False, admin: bool = False) -> str:
        up, rd, ad, who = self._rights()
        if not (up or rd or ad):
            raise ApiError(401, "a valid token is needed (Authorization: Bearer ...)")
        if (upload and not up) or (read and not rd) or (admin and not ad):
            raise ApiError(403, "this token can't " + ("upload" if upload else "read" if read else "delete"))
        return who

    def _route(self):
        url = urlsplit(self.path)
        parts = [unquote(p) for p in url.path.split("/") if p]
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        m, s = self.command, self.store
        if parts[:1] == ["admin"] and len(parts) <= 2 and m in ("GET", "HEAD"):
            page = ADMIN_FILES.get(parts[1] if len(parts) == 2 else "")
            if page is None:
                raise ApiError(404, "not found")
            return self._send(200, body=page[0].encode(), ctype=page[1], headers=ADMIN_HEADERS)
        if len(parts) == 2 and parts[0] == "invite" and m in ("GET", "HEAD"):
            return self._send(200, body=INVITE_PAGE.encode(), ctype="text/html; charset=utf-8")
        if parts[:1] != ["api"]:
            if parts == [] and m in ("GET", "HEAD"):
                return self._send(200, body=b"CANopener upload server. The API is under /api (see README); "
                                       b"team access is at /admin.\n",
                                  ctype="text/plain; charset=utf-8")
            raise ApiError(404, "not found")
        p = parts[1:]
        if p == ["health"] and m in ("GET", "HEAD"):
            return self._send(200, {"ok": True, "version": VERSION})
        if p == ["whoami"] and m == "GET":
            up, rd, ad, _ = self._rights()
            remote = any(hmac.compare_digest(self._token(), t.encode()) for t in self.cfg.remote_tokens)
            if not (up or rd or ad or remote):
                raise ApiError(401, "a valid token is needed (Authorization: Bearer ...)")
            tok = self._token()
            return self._send(200, {"can_upload": up, "can_read": rd, "can_delete": ad, "server": self.cfg.name,
                                    "can_remote": any(hmac.compare_digest(tok, t.encode())
                                                      for t in self.cfg.remote_tokens),
                                    "version": VERSION})
        if p == ["uploads"] and m == "POST":
            who = self._need(upload=True)
            return self._send(201, s.create(self._json(), who))
        if len(p) == 2 and p[0] == "uploads" and m == "GET":
            self._need(upload=True)
            return self._send(200, s.describe(p[1]))
        if len(p) == 4 and p[0] == "uploads" and p[2] == "parts" and m == "PUT":
            self._need(upload=True)
            if not p[3].isdigit():
                raise ApiError(400, "part number must be a number")
            data = self._body(self.cfg.max_part)
            return self._send(200, s.put_part(p[1], int(p[3]), data, self.headers.get("X-Content-SHA256")))
        if len(p) == 3 and p[0] == "uploads" and p[2] == "complete" and m == "POST":
            self._need(upload=True)
            self._body(MAX_JSON)
            return self._send(200, s.complete(p[1]))
        if p == ["captures"] and m == "GET":
            self._need(read=True)
            entries = s.index()
            if q.get("q"):
                entries = [e for e in entries if _matches(e, q["q"])]
            if q.get("since"):
                try:
                    since = float(q["since"])
                except ValueError:
                    raise ApiError(400, "since must be a Unix time") from None
                entries = [e for e in entries if (e.get("uploaded") or 0) >= since]
            limit = int(q["limit"]) if q.get("limit", "").isdigit() else 50
            return self._send(200, {"count": len(entries), "captures": entries[:limit]})
        if len(p) == 2 and p[0] == "captures" and m == "GET":
            self._need(read=True)
            return self._send(200, s.capture(p[1]))
        if len(p) == 2 and p[0] == "captures" and m == "DELETE":
            self._need(admin=True)
            return self._send(200, s.delete(p[1]))
        if len(p) == 3 and p[0] == "captures" and p[2] == "bundle.zip" and m in ("GET", "HEAD"):
            self._need(read=True)
            return self._send_file(s.file_path(p[1], "bundle.zip"), None)
        if len(p) == 4 and p[0] == "captures" and p[2] == "files" and m in ("GET", "HEAD"):
            self._need(read=True)
            mb = q.get("max_bytes")
            return self._send_file(s.file_path(p[1], p[3]), int(mb) if mb and mb.isdigit() else None)
        # --- machine profiles ---
        if p == ["profiles"] and m == "GET":
            self._need(read=True)
            entries = self.profiles.index()
            if q.get("q"):
                words = q["q"].lower().split()
                entries = [e for e in entries if all(w in json.dumps(e).lower() for w in words)]
            return self._send(200, {"count": len(entries), "profiles": entries})
        if len(p) == 2 and p[0] == "profiles" and m == "PUT":
            who = self._need(upload=True)
            return self._send(200, self.profiles.put(p[1], self._json(), who, self.headers.get("X-Device") or ""))
        if len(p) == 2 and p[0] == "profiles" and m in ("GET", "HEAD"):
            self._need(read=True)
            rev = int(q["rev"]) if q.get("rev", "").isdigit() else None
            return self._send_file(self.profiles.get(p[1], rev), None)
        # --- remote help ---
        r = self.relay
        if p == ["remote", "tunnel"] and m == "POST":
            who = self._need(upload=True)
            return self._send(200, self.tunnels.enroll(self._json(), who))
        if p == ["remote", "sessions"] and m == "POST":
            who = self._need(upload=True)
            return self._send(201, r.open(self._json(), who))
        if p == ["remote", "sessions"] and m == "GET":
            self._need_remote()
            return self._send(200, {"sessions": r.list()})
        if len(p) >= 3 and p[:2] == ["remote", "sessions"]:
            sid, rest = p[2], p[3:]
            if not ID_RE.match(sid):
                raise ApiError(400, "bad session id")
            wait = float(q["wait"]) if q.get("wait", "").replace(".", "", 1).isdigit() else 0.0
            if rest == [] and m == "DELETE":
                if not self._rights()[0]:
                    self._need_remote()
                return self._send(200, r.close(sid))
            if rest == ["next"] and m == "GET":
                self._need(upload=True)
                cmd = r.next(sid, wait)
                return self._send(200, {"command": cmd})
            if len(rest) == 2 and rest[0] == "results" and m == "POST":
                self._need(upload=True)
                raw = self._body(Relay.MAX_RESULT)
                try:
                    body = json.loads(raw.decode() or "{}")
                except (ValueError, UnicodeDecodeError):
                    raise ApiError(400, "the result must be JSON") from None
                return self._send(200, r.result(sid, rest[1], body))
            if rest == ["commands"] and m == "POST":
                who = self._need_remote()
                return self._send(201, r.command(sid, self._json(), who))
            if len(rest) == 2 and rest[0] == "results" and m == "GET":
                self._need_remote()
                return self._send(200, {"result": r.fetch(sid, rest[1], wait)})
        # --- team access ---
        if p[:1] == ["team"]:
            return self._team(p[1:], m)
        raise ApiError(404, "not found")

    def _base(self) -> str:
        return self.cfg.public_url or f"https://{self.headers.get('Host') or 'localhost'}"

    def _install(self) -> tuple[str, bytes, str]:
        return (self.headers.get("X-Install") or "", self._token(), self.headers.get("X-Machine") or "")

    def _team(self, p: list, m: str):
        t = self.team
        if p == ["redeem"] and m == "POST":
            return self._send(200, t.redeem(self._json()))
        if p == ["requests"] and m == "POST":
            if self.cfg.team_pending <= 0:
                raise ApiError(404, "this server only takes invites; ask for an invite link")
            return self._send(201, t.request(self._json()))
        if p == ["me"] and m == "GET":
            iid, key, machine = self._install()
            return self._send(200, t.me(iid, key, machine, self.headers.get("X-App-Version") or "",
                                        self.headers.get("X-Pack-Version") or ""))
        if p == ["pack", "bundle.zip"] and m in ("GET", "HEAD"):
            return self._send_file(t.bundle(*self._install()), None)
        # the owner's side: an admin token
        if p == ["installs"] and m == "GET":
            self._need(admin=True)
            return self._send(200, {"installs": t.installs()})
        if len(p) == 2 and p[0] == "installs" and m == "POST":
            self._need(admin=True)
            return self._send(200, t.update(p[1], self._json()))
        if len(p) == 2 and p[0] == "installs" and m == "DELETE":
            self._need(admin=True)
            return self._send(200, t.forget(p[1]))
        if p == ["invites"] and m == "POST":
            self._need(admin=True)
            return self._send(201, t.invite(self._json(), self._base()))
        if p == ["invites"] and m == "GET":
            self._need(admin=True)
            return self._send(200, {"invites": t.invites()})
        if len(p) == 2 and p[0] == "invites" and m == "DELETE":
            self._need(admin=True)
            return self._send(200, t.uninvite(p[1]))
        if p == ["pack"] and m == "GET":
            self._need(admin=True)
            return self._send(200, t.pack() or {})
        if p == ["pack"] and m == "PUT":
            who = self._need(admin=True)
            return self._send(200, t.publish(self._body(self.cfg.max_part), self.headers.get("X-Pack-Version") or "",
                                             who))
        raise ApiError(404, "not found")

    def _handle(self):
        try:
            self._route()
        except TeamDenied as e:
            self._send(e.status, {"error": str(e), "status": e.state})
        except ApiError as e:
            self._send(e.status, {"error": str(e)})
        except Exception as e:          # keep serving; say what broke
            sys.stderr.write(f"error: {e!r}\n")
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "server error (see its log)"})

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _handle


def make_server(cfg: Config) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"store": Store(cfg), "cfg": cfg, "timeout": cfg.timeout,
                                                "profiles": Profiles(cfg), "relay": Relay(cfg),
                                                "tunnels": Tunnels(cfg), "team": Team(cfg)})
    return ThreadingHTTPServer((cfg.host, cfg.port), handler)


def _cleanup_forever(store: Store, every: float = CLEANUP_EVERY):
    while True:
        time.sleep(every)
        try:
            store.cleanup()
        except Exception as e:          # never let housekeeping stop the server
            sys.stderr.write(f"cleanup error: {e!r}\n")


def main() -> int:
    cfg = Config()
    if not cfg.upload_tokens:
        print("UPLOAD_TOKENS is empty: nothing can upload. Set it (see README).", file=sys.stderr)
    if not cfg.read_tokens:
        print("READ_TOKENS is empty: nothing can read the captures back.", file=sys.stderr)
    srv = make_server(cfg)
    threading.Thread(target=_cleanup_forever, args=(srv.RequestHandlerClass.store,), daemon=True).start()
    print(f"{cfg.name}: listening on {cfg.host}:{cfg.port}, data in {cfg.data}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
