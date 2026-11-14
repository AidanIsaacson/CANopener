"""Remote help: let someone you trust work a unit in the field through the upload server.

A unit on a phone hotspot can't be reached from outside, but it can call out. ``RemoteLink`` opens
a session on the upload server (deploy/upload-server, ``/api/remote``) and keeps asking it for
work; whoever holds one of the server's *remote* tokens queues commands for that session and
collects the results (``Operator``, ``canopener-cli remote``). The server only carries them.

**The unit decides what it will do.** A command is an action name and arguments; only actions
registered here (``register``) exist. Each action says whether it only reads. One that could
change a vehicle is not run until the person at the unit says yes (``confirm`` is called with a
plain description; the front-end shows it, default No); with nobody to ask, it is refused.
Everything run is kept in ``RemoteLink.log`` for the screen to show. The session ends when the
person stops it, when its time is up, or when the link is lost for too long.

The front-ends add their own actions (a screenshot, the adapter, a scan); the ones here need only
the files on the unit: ``status``, ``logs``, ``file``, ``profiles``.

No Qt, no Pygame. HTTP goes through ``core.upload`` (the same server address and token).
"""

import base64
import json
import os
import threading
import time
from dataclasses import dataclass, field

from .. import __version__
from . import config, profiles, upload

POLL_WAIT = 25              # seconds the server holds a request open waiting for a command
MAX_FILE = 6 * 1024 * 1024  # the biggest file sent back whole
LOST_AFTER = 180.0          # seconds without reaching the server: the session is given up


@dataclass
class Action:
    name: str
    fn: object              # fn(args: dict, link: RemoteLink) -> JSON-friendly result
    reads_only: object = True   # True / False, or fn(args) -> bool when it depends on what's asked
    describe: object = None  # describe(args) -> what it will do, in words, for the person asked to allow it
    help: str = ""


_actions: dict[str, Action] = {}


def register(name: str, fn, reads_only: bool = True, describe=None, help: str = ""):
    _actions[name] = Action(name, fn, reads_only, describe, help)


def actions() -> dict[str, Action]:
    return dict(_actions)


class Refused(Exception):
    """The unit won't do this (unknown action, not allowed by the person at it, bad arguments)."""


@dataclass
class LogEntry:
    t: float
    action: str
    text: str
    ok: bool = True


@dataclass
class RemoteLink:
    """One session. ``confirm(text) -> bool`` asks the person at the unit (None: nobody to ask)."""

    settings: object
    label: str = ""
    minutes: float = 30.0
    confirm: object = None
    info: object = None                 # info() -> dict sent when the session opens (what the unit is)
    session: dict = field(default_factory=dict)
    log: list = field(default_factory=list)
    state: str = "off"                  # off / connecting / open / ended: <why>
    context: dict = field(default_factory=dict)     # what the front-end hands its actions (ctx, app ...)

    def __post_init__(self):
        self._stop = threading.Event()
        self._thread = None
        self._http = upload.HttpUploader()

    # --- plumbing ---
    def _call(self, method: str, path: str, body=None, timeout: float | None = None) -> dict:
        raw = json.dumps(body).encode() if body is not None else None
        return self._http._call(self.settings, method, path, raw, timeout=timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def ends_in(self) -> float:
        return max(0.0, (self.session.get("expires") or 0) - time.time()) if self.session else 0.0

    def note(self, action: str, text: str, ok: bool = True):
        self.log.append(LogEntry(time.time(), action, text, ok))
        del self.log[:-200]

    # --- lifecycle ---
    def start(self):
        if self.running:
            return
        self._stop.clear()
        self.state = "connecting"
        self._thread = threading.Thread(target=self._run, name="remote-help", daemon=True)
        self._thread.start()

    def stop(self, why: str = "stopped here"):
        self._stop.set()
        sid = self.session.get("id")
        if sid:
            try:
                self._call("DELETE", f"/api/remote/sessions/{sid}", timeout=8)
            except upload.UploadError:
                pass
        if self.state in ("open", "connecting"):
            self.state = f"ended: {why}"
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3.0)

    def _run(self):
        try:
            body = {"device": upload.device_id(), "label": self.label, "minutes": self.minutes,
                    "info": {"version": __version__, **(self.info() if self.info else {})}}
            self.session = self._call("POST", "/api/remote/sessions", body, timeout=20)
        except upload.UploadError as e:
            self.state = f"ended: {e}"
            return
        self.state = "open"
        self.note("session", f"open for {self.minutes:g} min")
        sid, last_ok = self.session["id"], time.time()
        while not self._stop.is_set():
            if time.time() >= self.session.get("expires", 0):
                self.state = "ended: time is up"
                break
            try:
                got = self._call("GET", f"/api/remote/sessions/{sid}/next?wait={POLL_WAIT}", timeout=POLL_WAIT + 15)
                last_ok = time.time()
            except upload.UploadError as e:
                if not e.retry or time.time() - last_ok > LOST_AFTER:
                    self.state = f"ended: {e}"
                    break
                self._stop.wait(5.0)
                continue
            cmd = got.get("command")
            if not cmd:
                continue
            result = self.run(cmd.get("action", ""), cmd.get("args") or {})
            try:
                self._call("POST", f"/api/remote/sessions/{sid}/results/{cmd['id']}", result, timeout=60)
            except upload.UploadError as e:
                self.note(cmd.get("action", "?"), f"couldn't send the result back: {e}", False)
        if self.state == "open":
            self.state = "ended: stopped here"

    # --- one command ---
    def run(self, name: str, args: dict) -> dict:
        """Run one action here. Always returns {"ok": ..., ...}: a refusal or an error is a result too."""
        act = _actions.get(name)
        try:
            if act is None:
                raise Refused(f"this unit has no action called {name!r} (it has: {', '.join(sorted(_actions))})")
            reads_only = act.reads_only(args) if callable(act.reads_only) else act.reads_only
            if not reads_only:
                what = act.describe(args) if act.describe else f"{name} {args}"
                if self.confirm is None:
                    raise Refused("this would change something, and there is nobody at the unit to allow it")
                self.note(name, f"asking here: {what}")
                if not self.confirm(what):
                    raise Refused("the person at the unit said no")
            value = act.fn(args, self)
            self.note(name, _short(args))
            return {"ok": True, "value": value}
        except Refused as e:
            self.note(name, f"refused: {e}", False)
            return {"ok": False, "refused": True, "error": str(e)}
        except Exception as e:              # the remote side gets the reason; the unit keeps going
            self.note(name, f"failed: {e}", False)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _short(args: dict) -> str:
    text = json.dumps(args, ensure_ascii=False) if args else ""
    return text if len(text) <= 80 else text[:77] + "..."


# ----------------------------------------------------------------------
# actions every unit has (files only)
# ----------------------------------------------------------------------
def _safe_path(root: str, rel: str) -> str:
    path = os.path.realpath(os.path.join(root, rel))
    if not (path == os.path.realpath(root) or path.startswith(os.path.realpath(root) + os.sep)):
        raise Refused("that path is outside the logs folder")
    return path


def _status(args, link):
    p = profiles.active()
    return {"version": __version__, "device": upload.device_id(), "profile": p.id if p else "", "time": time.time(),
            "logs_dir": config.logs_dir(), "actions": {n: a.help for n, a in sorted(_actions.items())}}


def _logs(args, link):
    root = config.logs_dir()
    out = []
    for name in sorted(os.listdir(root), reverse=True)[:int(args.get("limit") or 30)] if os.path.isdir(root) else []:
        folder = os.path.join(root, name)
        if os.path.isdir(folder):
            out.append({"name": name, "files": {f: os.path.getsize(os.path.join(folder, f))
                                                for f in sorted(os.listdir(folder))}})
    return out


def _file(args, link):
    path = _safe_path(config.logs_dir(), str(args.get("path") or ""))
    if not os.path.isfile(path):
        raise Refused("no such file in the logs folder")
    size = os.path.getsize(path)
    offset, limit = int(args.get("offset") or 0), min(int(args.get("max_bytes") or MAX_FILE), MAX_FILE)
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(limit)
    return {"path": args.get("path"), "size": size, "offset": offset, "base64": base64.b64encode(data).decode()}


def _profiles(args, link):
    found, errors = profiles.available_profiles()
    name = args.get("id")
    if name:
        p = next((x for x in found if x.id == name), None)
        if p is None or not p.path:
            raise Refused("no such profile on this unit")
        with open(p.path, encoding="utf-8") as f:
            return json.load(f)
    return {"profiles": [{"id": p.id, "name": p.name, "builtin": p.builtin, "signals": len(p.signals)} for p in found],
            "errors": errors}


register("status", _status, help="what this unit is: version, profile, and the actions it has")
register("logs", _logs, help="the saved logs and their files (args: limit)")
register("file", _file, help="one file from the logs folder, base64 (args: path, offset, max_bytes)")
register("profiles", _profiles, help="the profiles on this unit, or one of them in full (args: id)")


# ----------------------------------------------------------------------
# the other end
# ----------------------------------------------------------------------
class Operator:
    """The far side: list sessions, send a command, wait for its result. ``token`` is one of the
    server's remote tokens."""

    def __init__(self, url: str, token: str):
        self.url, self.token = upload.normalize_url(url), token

    def _call(self, method: str, path: str, body=None, timeout: float = 40.0) -> dict:
        raw = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}"}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        status, data = upload.http(method, self.url + path, raw, headers, timeout)
        try:
            js = json.loads(data.decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            js = {}
        if not 200 <= status < 300:
            raise upload.UploadError(js.get("error") or f"HTTP {status}", retry=False)
        return js

    def sessions(self) -> list[dict]:
        return self._call("GET", "/api/remote/sessions")["sessions"]

    def send(self, session: str, action: str, args: dict | None = None, wait: float = 120.0) -> dict:
        """Queue a command and wait for the unit's answer ({"ok": ..., "value" / "error": ...})."""
        cid = self._call("POST", f"/api/remote/sessions/{session}/commands",
                         {"action": action, "args": args or {}})["id"]
        end = time.time() + wait
        while time.time() < end:
            got = self._call("GET", f"/api/remote/sessions/{session}/results/{cid}?wait=25")["result"]
            if got:
                return got["result"]
        raise upload.UploadError("the unit didn't answer in time (is the session still open?)", retry=True)

    def close(self, session: str):
        self._call("DELETE", f"/api/remote/sessions/{session}")
