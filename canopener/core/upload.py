"""Sending saved captures to a server (or wherever a plugin sends them), from either UI.

The flow: a saved capture folder is zipped (``make_bundle``), queued (``UploadQueue.enqueue``) and
sent on a background thread when there's a connection. Each capture folder keeps its own state in
``upload.json`` (queued / uploading / done / failed, the server's ID for it, and a fingerprint of
the files so "changed since it was uploaded" can be shown). The queue survives a restart: the
folders still to send are listed in ``~/.config/canopener/upload-queue.json``.

Where captures go is an ``Uploader``. Built in: ``HttpUploader`` (id ``"server"``), for the
receiving server in ``deploy/upload-server/`` (any HTTPS host speaking the same small API: see its
README). Also built in: ``upload_github.GitHubUploader`` (id ``"github"``), which commits captures
to a private GitHub repository with the device's GitHub sign-in (no server needed). A plugin can
add another destination with ``register_uploader`` (the GUI's ``PluginApi.register_uploader``
does the same).

Settings (``core/settings.py``): ``upload.target`` (which uploader), ``upload.url`` (the server),
``upload.on_save`` (queue every capture when it's saved). The server's token is not a setting: it
is kept in ``~/.config/canopener/upload-token.json``, readable only by the user (like the GitHub
token), or given as ``CANOPENER_UPLOAD_TOKEN``.

The HTTP protocol, in short (``deploy/upload-server/README.md`` has it in full)::

    POST /api/uploads                      {key, size, sha256, part_size, manifest} -> {upload_id, part_size, received}
    PUT  /api/uploads/<id>/parts/<n>       raw bytes (X-Content-SHA256)             -> {received}
    POST /api/uploads/<id>/complete                                                  -> {key, revision, url}

Uploads go in parts (8 MB by default) so a Cloudflare tunnel's 100 MB request limit never bites
and a dropped connection only costs the part in flight: creating the same upload again (same key
and checksum) returns the parts the server already has. Standard library only (urllib, zipfile).
"""

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field

from .. import __version__
from . import config
from .capture import META_JSON, Capture, machine_summary, safe_name

UPLOAD_JSON = "upload.json"
QUEUE_FILE = "upload-queue.json"
TOKEN_FILE = "upload-token.json"
DEVICE_ID_FILE = "device-id"
PART_SIZE = 8 * 1024 * 1024
TIMEOUT = 60
USER_AGENT = f"CANopener/{__version__}"
RETRY_FIRST, RETRY_MAX = 30.0, 1800.0           # seconds: back-off between automatic retries
PART_TRIES = 3                                  # tries per part before the attempt fails

QUEUED, UPLOADING, DONE, FAILED = "queued", "uploading", "done", "failed"


class UploadError(Exception):
    """Something went wrong sending a capture, worded for the user. ``retry``: worth trying again
    by itself later (no connection, server down) rather than needing the user (bad token)."""

    def __init__(self, text: str, retry: bool = True):
        super().__init__(text)
        self.retry = retry


# ------------------------------------------------------------------------------------ helpers

def cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "canopener")


def device_id() -> str:
    """A random ID for this install, made once: tells the server which unit sent a capture."""
    path = os.path.join(config.config_dir(), DEVICE_ID_FILE)
    try:
        with open(path) as f:
            v = f.read().strip()
        if v:
            return v
    except OSError:
        pass
    v = uuid.uuid4().hex
    config.ensure_dir(config.config_dir())
    with open(path, "w") as f:
        f.write(v + "\n")
    return v


def _bundle_files(folder: str) -> list[str]:
    """The files that go in a bundle: everything but our own upload state and temporary files."""
    out = []
    for name in sorted(os.listdir(folder)):
        p = os.path.join(folder, name)
        if name == UPLOAD_JSON or name.startswith(".") or name.endswith(".tmp") or not os.path.isfile(p):
            continue
        out.append(name)
    return out


def fingerprint(folder: str) -> str:
    """Changes whenever a file in the capture changes (relabel, trim, new machine details)."""
    h = hashlib.sha256()
    for name in _bundle_files(folder):
        st = os.stat(os.path.join(folder, name))
        h.update(f"{name}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:16]


def capture_key(c: Capture) -> str:
    """The name the capture has on the server: start time, name, and this unit. Stays the same
    when the capture is relabelled or re-saved, so a re-upload replaces the copy there."""
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(c.started or 0))
    label = safe_name(c.name) or safe_name(machine_summary(c.machine)) or "capture"
    return f"{stamp}_{label[:40]}_{device_id()[:6]}"


def manifest(c: Capture, key: str) -> dict:
    """What the server indexes a capture by (it also gets the whole folder, README included)."""
    meta = c.meta or {}
    bus = meta.get("bus") or {}
    return {
        "key": key, "name": c.name, "machine": dict(c.machine), "machine_summary": machine_summary(c.machine),
        "concern": c.notes, "question": c.question,
        "started": c.started, "stopped": c.stopped, "frames": c.frames, "samples": c.samples,
        "timestamps": len(c.tagged()),
        "connection": {k: meta[k] for k in ("source", "interface", "bitrate", "vehicle_protocol", "profile",
                                             "listen_only", "front_end") if k in meta},
        "undecoded_streams": bus.get("undecoded_count"), "streams": bus.get("traffic_count"),
        "folder": os.path.basename(c.folder or ""), "device": device_id(), "app_version": __version__,
    }


@dataclass
class Bundle:
    path: str
    size: int
    sha256: str
    key: str
    manifest: dict
    fingerprint: str
    folder: str = ""            # the capture folder it was made from


def make_bundle(folder: str, out_dir: str | None = None, key: str | None = None) -> Bundle:
    """Zip a saved capture folder (flat: the files at the top of the zip)."""
    c = Capture.load(folder)
    if c.state != "saved":
        raise UploadError("Save the capture first: only saved captures are uploaded.", retry=False)
    key = key or capture_key(c)
    out_dir = config.ensure_dir(out_dir or os.path.join(cache_dir(), "uploads"))
    path = os.path.join(out_dir, f"{key}.zip")
    fp = fingerprint(folder)
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name in _bundle_files(folder):
            z.write(os.path.join(folder, name), name)
    os.replace(tmp, path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return Bundle(path, os.path.getsize(path), h.hexdigest(), key, manifest(c, key), fp, folder)


# ---------------------------------------------------------------------- per-capture state

def read_state(folder: str) -> dict:
    try:
        with open(os.path.join(folder, UPLOAD_JSON)) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(folder: str, state: dict):
    tmp = os.path.join(folder, UPLOAD_JSON + ".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, os.path.join(folder, UPLOAD_JSON))


@dataclass
class Status:
    """One capture's upload state, ready to show."""
    state: str = ""                 # "" (never queued), queued, uploading, done, failed
    text: str = ""
    progress: float | None = None   # 0..1 while uploading
    key: str = ""
    url: str = ""
    stale: bool = False             # done, but the capture changed after it was sent
    error: str = ""


def _ago(t: float | None, now: float) -> str:
    if not t:
        return ""
    return time.strftime("%H:%M" if now - t < 20 * 3600 else "%Y-%m-%d %H:%M", time.localtime(t))


def status(folder: str | None, progress: dict | None = None, now: float | None = None) -> Status:
    if not folder or not os.path.isdir(folder):
        return Status()
    now = time.time() if now is None else now
    st = read_state(folder)
    state = st.get("state", "")
    s = Status(state, key=st.get("key", ""), url=st.get("url", ""), error=st.get("error", ""))
    if state == DONE:
        s.stale = st.get("fingerprint") != fingerprint(folder)
        s.text = (f"Uploaded {_ago(st.get('done'), now)}" + (f" as {s.key}" if s.key else "")
                  + (" (changed since: upload again to update it)" if s.stale else ""))
    elif state == UPLOADING:
        sent, total = (progress or {}).get(folder, (0, 0))
        s.progress = sent / total if total else None
        s.text = f"Uploading {s.progress:.0%}" if s.progress is not None else "Uploading"
    elif state == QUEUED:
        s.text = "Waiting to upload"
    elif state == FAILED:
        nxt = st.get("retry_at")
        s.text = f"Upload failed: {s.error}" + (f" (trying again at {_ago(nxt, now)})" if nxt else "")
    return s


# ------------------------------------------------------------------------------ uploaders

class Uploader:
    """A place captures can go. Subclass it (a plugin can) and ``register_uploader`` an instance."""
    id = ""
    name = ""

    def configured(self, settings) -> bool:
        return True

    def describe(self, settings) -> str:
        """Where it sends, for the UI ("https://logs.example.com")."""
        return self.name

    def upload(self, bundle: Bundle, settings, progress=None) -> dict:
        """Send ``bundle``; ``progress(sent, total)`` as it goes. Return ``{"key", "url", ...}``
        (stored in upload.json). Raise ``UploadError`` (``retry=False`` when the user must act)."""
        raise NotImplementedError

    def check(self, settings) -> str:
        """Test the connection and the credentials: a line for the user, or raise UploadError."""
        return "OK"


_uploaders: dict[str, Uploader] = {}


def register_uploader(u: Uploader):
    """Add (or replace) a destination. A plugin calls this at import or from ``register_gui`` /
    ``register_device``; it then shows in Upload settings on both UIs."""
    if not u.id:
        raise ValueError("an uploader needs an id")
    _uploaders[u.id] = u


def uploaders() -> list[Uploader]:
    return list(_uploaders.values())


def get_uploader(uid: str | None) -> Uploader | None:
    return _uploaders.get(uid or "")


def target(settings) -> Uploader | None:
    """The chosen destination if it's set up, else the first one that is."""
    u = get_uploader(settings.get("upload.target"))
    if u is not None and u.configured(settings):
        return u
    return next((x for x in uploaders() if x.configured(settings)), None)


# -------------------------------------------------------------------------- the server token

def token_path() -> str:
    return os.path.join(config.config_dir(), TOKEN_FILE)


def load_token() -> str:
    env = os.environ.get("CANOPENER_UPLOAD_TOKEN")
    if env:
        return env.strip()
    try:
        with open(token_path()) as f:
            return str(json.load(f).get("token") or "").strip()
    except (OSError, ValueError, AttributeError):
        return ""


def save_token(token: str):
    """Store the token readable by this user only ("" removes it)."""
    token = (token or "").strip()
    if not token:
        try:
            os.remove(token_path())
        except OSError:
            pass
        return
    config.ensure_dir(config.config_dir())
    tmp = token_path() + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"token": token}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, token_path())


# -------------------------------------------------------------------------- HTTP uploader

def _urllib_http(method: str, url: str, body: bytes | None = None, headers: dict | None = None,
                 timeout: float = TIMEOUT) -> tuple[int, bytes]:
    """(status, body). Network trouble raises UploadError(retry=True). Tests replace ``http``."""
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise UploadError(f"Couldn't reach the server (no internet?): {getattr(e, 'reason', e)}") from e


http = _urllib_http


def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    return url


class HttpUploader(Uploader):
    """The receiving server in deploy/upload-server (or anything speaking its API)."""
    id = "server"
    name = "Upload server"

    def __init__(self, token_fn=load_token):
        self.token_fn = token_fn

    def configured(self, settings) -> bool:
        return bool(normalize_url(settings.get("upload.url"))) and bool(self.token_fn())

    def describe(self, settings) -> str:
        return normalize_url(settings.get("upload.url")) or "not set up"

    def _call(self, settings, method: str, path: str, body: bytes | None = None, ctype: str = "application/json",
              extra: dict | None = None) -> dict:
        base = normalize_url(settings.get("upload.url"))
        if not base:
            raise UploadError("No upload server is set (Upload settings).", retry=False)
        if not base.startswith("https://") and not settings.get("upload.allow_http") \
                and urllib.parse.urlsplit(base).hostname not in ("localhost", "127.0.0.1"):
            raise UploadError("The upload server address must start with https:// (the token would travel in "
                              "the clear otherwise).", retry=False)
        token = self.token_fn()
        if not token:
            raise UploadError("No upload token is set (Upload settings).", retry=False)
        headers = {"Authorization": f"Bearer {token}", **(extra or {})}
        if body is not None:
            headers["Content-Type"] = ctype
        status, raw = http(method, base + path, body, headers)
        try:
            js = json.loads(raw.decode() or "null") if raw else None
        except (ValueError, UnicodeDecodeError):
            js = None
        if 200 <= status < 300:
            return js if isinstance(js, dict) else {}
        why = (js or {}).get("error") if isinstance(js, dict) else ""
        why = why or (raw[:200].decode(errors="replace").strip() if raw else "") or f"HTTP {status}"
        if status in (401, 403):
            raise UploadError(f"The server refused the token ({why}). Check it in Upload settings.", retry=False)
        if status in (400, 404, 409, 413, 422):
            raise UploadError(f"The server refused the upload: {why}", retry=status in (404, 409, 422))
        raise UploadError(f"Server error: {why}")

    def check(self, settings) -> str:
        js = self._call(settings, "GET", "/api/whoami")
        if not js.get("can_upload"):
            raise UploadError("The server knows this token, but it can't upload with it.", retry=False)
        return f"Connected to {js.get('server') or self.describe(settings)}: this token can upload."

    def upload(self, bundle: Bundle, settings, progress=None) -> dict:
        part_size = int(float(settings.get("upload.part_mb") or 8) * 1024 * 1024) or PART_SIZE
        js = self._call(settings, "POST", "/api/uploads", json.dumps({
            "key": bundle.key, "size": bundle.size, "sha256": bundle.sha256, "part_size": part_size,
            "manifest": bundle.manifest}).encode())
        uid = js.get("upload_id")
        if not uid:
            raise UploadError("The server didn't start the upload (no upload_id).")
        part_size = int(js.get("part_size") or part_size)
        have = {int(n) for n in js.get("received", [])}
        parts = max(1, -(-bundle.size // part_size))
        sent = sum(min(part_size, bundle.size - n * part_size) for n in have if n < parts)
        if progress:
            progress(sent, bundle.size)
        with open(bundle.path, "rb") as f:
            for n in range(parts):
                if n in have:
                    continue
                f.seek(n * part_size)
                chunk = f.read(part_size)
                for attempt in range(PART_TRIES):
                    try:
                        self._call(settings, "PUT", f"/api/uploads/{uid}/parts/{n}", chunk,
                                   "application/octet-stream", {"X-Content-SHA256": hashlib.sha256(chunk).hexdigest()})
                        break
                    except UploadError as e:
                        if not e.retry or attempt == PART_TRIES - 1:
                            raise
                        time.sleep(2 * (attempt + 1))
                sent += len(chunk)
                if progress:
                    progress(sent, bundle.size)
        done = self._call(settings, "POST", f"/api/uploads/{uid}/complete", b"{}")
        return {"key": done.get("key", bundle.key), "url": done.get("url", ""), "revision": done.get("revision")}


register_uploader(HttpUploader())


# ---------------------------------------------------------------------------------- queue

@dataclass
class UploadQueue:
    """Captures waiting to go, sent one at a time on a background thread.

    ``enqueue(folder)`` from either UI; ``status(folder)`` to show it. ``run_once()`` does one
    upload synchronously (tests and the CLI use it); ``start()`` runs it in a loop on a thread,
    retrying failed uploads with a growing back-off while the app runs."""
    settings: object
    clock: object = time.time
    progress: dict = field(default_factory=dict)      # folder -> (bytes sent, total)
    on_change: object = None                          # fn(folder), called on the worker thread

    def __post_init__(self):
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._stop = False
        self.current: str | None = None

    # --- the queue file ---
    def _queue_path(self) -> str:
        return os.path.join(config.config_dir(), QUEUE_FILE)

    def folders(self) -> list[str]:
        with self._lock:
            try:
                with open(self._queue_path()) as f:
                    d = json.load(f)
            except (OSError, ValueError):
                return []
            return [p for p in d if isinstance(p, str)] if isinstance(d, list) else []

    def _set_folders(self, folders: list[str]):
        with self._lock:
            config.ensure_dir(config.config_dir())
            tmp = self._queue_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(folders, f, indent=2)
            os.replace(tmp, self._queue_path())

    # --- front-end calls ---
    def enqueue(self, folder: str, target_id: str | None = None) -> Status:
        """Queue a saved capture (again, if it was sent before). Raises UploadError when nowhere
        to send it is set up, or it isn't saved."""
        folder = os.path.abspath(folder)
        if not os.path.exists(os.path.join(folder, META_JSON)):
            raise UploadError("That isn't a capture folder.", retry=False)
        if Capture.load(folder).state != "saved":
            raise UploadError("Save the capture first: only saved captures are uploaded.", retry=False)
        u = get_uploader(target_id) if target_id else target(self.settings)
        if u is None or not u.configured(self.settings):
            raise UploadError("Nowhere to upload to yet: set up the upload server first (Upload settings).",
                              retry=False)
        with self._lock:
            st = read_state(folder)
            if st.get("state") == UPLOADING and self.current == folder:
                return status(folder, self.progress)
            st.update(state=QUEUED, target=u.id, queued=self.clock(), attempts=0, error="", retry_at=None)
            write_state(folder, st)
            q = self.folders()
            if folder not in q:
                self._set_folders(q + [folder])
        self._wake.set()
        self._changed(folder)
        return status(folder, self.progress)

    def cancel(self, folder: str) -> bool:
        folder = os.path.abspath(folder)
        with self._lock:
            if self.current == folder:
                return False
            q = self.folders()
            if folder in q:
                self._set_folders([p for p in q if p != folder])
            st = read_state(folder)
            if st.get("state") in (QUEUED, FAILED):
                st["state"] = "" if not st.get("done") else DONE
                write_state(folder, st)
        self._changed(folder)
        return True

    def status(self, folder: str | None) -> Status:
        return status(folder, self.progress, self.clock())

    def waiting(self) -> int:
        return sum(1 for p in self.folders() if read_state(p).get("state") in (QUEUED, UPLOADING, FAILED))

    # --- the work ---
    def _changed(self, folder: str):
        if self.on_change is not None:
            try:
                self.on_change(folder)
            except Exception:
                pass

    def _due(self, now: float, force: bool = False) -> str | None:
        for p in self.folders():
            if not os.path.isdir(p):
                continue
            st = read_state(p)
            s = st.get("state")
            if s in (QUEUED, UPLOADING):            # UPLOADING here = the app stopped mid-upload
                return p
            if s == FAILED and st.get("retry_at") and (force or now >= st["retry_at"]):
                return p
        return None

    def _drop_gone(self):
        q = self.folders()
        keep = [p for p in q if os.path.isdir(p) and read_state(p).get("state") in (QUEUED, UPLOADING, FAILED)]
        if keep != q:
            self._set_folders(keep)

    def run_once(self, force: bool = False) -> str | None:
        """Send the next due capture. Returns its folder (None when nothing was due)."""
        now = self.clock()
        with self._lock:
            self._drop_gone()
            folder = self._due(now, force)
            if folder is None:
                return None
            st = read_state(folder)
            st.update(state=UPLOADING, started=now)
            write_state(folder, st)
            self.current = folder
        self._changed(folder)
        u = get_uploader(st.get("target")) or target(self.settings)
        try:
            if u is None:
                raise UploadError("Nowhere to upload to: the upload server isn't set up.", retry=False)
            b = make_bundle(folder, key=st.get("key"))
            self.progress[folder] = (0, b.size)

            def prog(sent, total):
                self.progress[folder] = (sent, total)
                self._changed(folder)
            result = u.upload(b, self.settings, prog)
            try:
                os.remove(b.path)
            except OSError:
                pass
            with self._lock:
                st = read_state(folder)
                st.update(state=DONE, done=self.clock(), key=result.get("key") or b.key, url=result.get("url", ""),
                          revision=result.get("revision"), fingerprint=b.fingerprint, size=b.size,
                          sha256=b.sha256, target=u.id, error="", retry_at=None)
                write_state(folder, st)
                self._set_folders([p for p in self.folders() if p != folder])
        except UploadError as e:
            self._failed(folder, str(e), e.retry)
        except (OSError, ValueError) as e:
            self._failed(folder, str(e), True)
        finally:
            self.current = None
            self.progress.pop(folder, None)
        self._changed(folder)
        return folder

    def _failed(self, folder: str, text: str, retry: bool):
        with self._lock:
            st = read_state(folder)
            attempts = int(st.get("attempts") or 0) + 1
            wait = min(RETRY_MAX, RETRY_FIRST * 2 ** (attempts - 1))
            st.update(state=FAILED, error=text, attempts=attempts, retry_at=self.clock() + wait if retry else None)
            write_state(folder, st)

    # --- the thread ---
    def start(self):
        if self._thread is not None:
            return
        self._stop = False

        def loop():
            while not self._stop:
                try:
                    did = self.run_once()
                except Exception:           # never let the worker die: try again later
                    did = None
                if did is None and not self._stop:
                    self._wake.wait(15.0)
                    self._wake.clear()
        self._thread = threading.Thread(target=loop, name="uploads", daemon=True)
        self._thread.start()

    def retry_now(self):
        """Try failed uploads straight away (the user pressed Upload, or the connection came back)."""
        with self._lock:
            for p in self.folders():
                st = read_state(p)
                if st.get("state") == FAILED:
                    st.update(state=QUEUED, retry_at=None)
                    write_state(p, st)
        self._wake.set()

    def stop(self):
        self._stop = True
        self._wake.set()
        t, self._thread = self._thread, None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)


def after_save(queue: UploadQueue | None, settings, folder: str) -> Status | None:
    """Front-ends call this after saving: queues the capture when "Upload when saved" is on."""
    if queue is None or not settings.get("upload.on_save") or target(settings) is None:
        return None
    try:
        return queue.enqueue(folder)
    except UploadError:
        return None


from . import upload_github  # noqa: E402,F401  (registers the GitHub uploader; it imports this module)
