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

VERSION = "3"
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
    cfg: Config = None
    timeout = 120               # a connection that goes quiet is dropped (make_server sets REQUEST_TIMEOUT)

    # --- plumbing ---
    def log_message(self, fmt, *args):        # path only: never the Authorization header
        status = args[1] if len(args) > 1 else ""
        who = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip() if self.headers else ""
        sys.stderr.write(f"{who or self.address_string()} {self.command} {urlsplit(self.path).path} {status}\n")

    def _send(self, status: int, data=None, ctype: str = "application/json", body: bytes | None = None):
        if body is None:
            body = (json.dumps(data, indent=1) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        if parts[:1] != ["api"]:
            if parts == [] and m in ("GET", "HEAD"):
                return self._send(200, body=b"CANopener upload server. The API is under /api (see README).\n",
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
        raise ApiError(404, "not found")

    def _handle(self):
        try:
            self._route()
        except ApiError as e:
            self._send(e.status, {"error": str(e)})
        except Exception as e:          # keep serving; say what broke
            sys.stderr.write(f"error: {e!r}\n")
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "server error (see its log)"})

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _handle


def make_server(cfg: Config) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"store": Store(cfg), "cfg": cfg, "timeout": cfg.timeout,
                                                "profiles": Profiles(cfg), "relay": Relay(cfg),
                                                "tunnels": Tunnels(cfg)})
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
