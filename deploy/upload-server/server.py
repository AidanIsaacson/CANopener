#!/usr/bin/env python3
"""CANopener upload server: receives capture folders from the app and serves them back to readers.

Standard library only (Python 3.10+), one file, no database: everything lives under DATA_DIR.
See README.md next to this file for the API, the layout on disk and how to run it (Docker, behind
nginx and a Cloudflare tunnel).

Configuration (environment variables):

  UPLOAD_TOKENS      comma-separated tokens that may upload (the app / handheld units)
  READ_TOKENS        comma-separated tokens that may list and download (you, an AI assistant)
  DATA_DIR           where captures are kept (default /data)
  HOST, PORT         where to listen (default 0.0.0.0:8080)
  PUBLIC_URL         the address readers use (https://logs.example.com), for the links it returns
  SERVER_NAME        a name shown to the app's Test button (default "CANopener upload server")
  MAX_UPLOAD_MB      largest capture bundle accepted (default 4096)
  MAX_PART_MB        largest single part (default 64; keep under Cloudflare's 100 MB request limit)
  KEEP_REVISIONS     earlier uploads of the same capture kept (default 3)
  INCOMING_DAYS      unfinished uploads are deleted after this many days (default 7)
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

VERSION = "1"
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
        self.max_upload = int(float(env.get("MAX_UPLOAD_MB") or 4096) * 1024 * 1024)
        self.max_part = int(float(env.get("MAX_PART_MB") or 64) * 1024 * 1024)
        self.keep = int(env.get("KEEP_REVISIONS") or 3)
        self.incoming_days = float(env.get("INCOMING_DAYS") or 7)


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
        self.purge_incoming()

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

    # --- uploading ---
    def purge_incoming(self):
        root = os.path.join(self.cfg.data, "incoming")
        cutoff = time.time() - self.cfg.incoming_days * 86400
        for uid in os.listdir(root):
            p = os.path.join(root, uid)
            try:
                if os.path.getmtime(p) < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass

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
    cfg: Config = None

    # --- plumbing ---
    def log_message(self, fmt, *args):        # path only: never the Authorization header
        status = args[1] if len(args) > 1 else ""
        sys.stderr.write(f"{self.address_string()} {self.command} {urlsplit(self.path).path} {status}\n")

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

    def _rights(self) -> tuple[bool, bool, str]:
        auth = self.headers.get("Authorization") or ""
        tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not tok:
            return False, False, ""
        up = any(hmac.compare_digest(tok, t) for t in self.cfg.upload_tokens)
        rd = any(hmac.compare_digest(tok, t) for t in self.cfg.read_tokens)
        return up, rd, hashlib.sha256(tok.encode()).hexdigest()[:8]

    def _need(self, upload: bool = False, read: bool = False) -> str:
        up, rd, who = self._rights()
        if not (up or rd):
            raise ApiError(401, "a valid token is needed (Authorization: Bearer ...)")
        if (upload and not up) or (read and not rd):
            raise ApiError(403, "this token can't " + ("upload" if upload else "read"))
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
            up, rd, _ = self._rights()
            if not (up or rd):
                raise ApiError(401, "a valid token is needed (Authorization: Bearer ...)")
            return self._send(200, {"can_upload": up, "can_read": rd, "server": self.cfg.name, "version": VERSION})
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
        if len(p) == 3 and p[0] == "captures" and p[2] == "bundle.zip" and m in ("GET", "HEAD"):
            self._need(read=True)
            return self._send_file(s.file_path(p[1], "bundle.zip"), None)
        if len(p) == 4 and p[0] == "captures" and p[2] == "files" and m in ("GET", "HEAD"):
            self._need(read=True)
            mb = q.get("max_bytes")
            return self._send_file(s.file_path(p[1], p[3]), int(mb) if mb and mb.isdigit() else None)
        raise ApiError(404, "not found")

    def _handle(self):
        try:
            self._route()
        except ApiError as e:
            self._send(e.status, {"error": str(e)})
        except Exception as e:          # keep serving; say what broke
            sys.stderr.write(f"error: {e!r}\n")
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "server error (see its log)"})

    do_GET = do_HEAD = do_POST = do_PUT = _handle


def make_server(cfg: Config) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"store": Store(cfg), "cfg": cfg})
    return ThreadingHTTPServer((cfg.host, cfg.port), handler)


def main() -> int:
    cfg = Config()
    if not cfg.upload_tokens:
        print("UPLOAD_TOKENS is empty: nothing can upload. Set it (see README).", file=sys.stderr)
    if not cfg.read_tokens:
        print("READ_TOKENS is empty: nothing can read the captures back.", file=sys.stderr)
    srv = make_server(cfg)
    print(f"{cfg.name}: listening on {cfg.host}:{cfg.port}, data in {cfg.data}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
