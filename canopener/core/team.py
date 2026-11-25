"""Team access: the team plugin, handed out by your own upload server to the installs you approve.

The private plugin normally comes from GitHub (``core.plugin_repos``), which needs a GitHub account
with access to its repository. Team access is the simpler way for colleagues: the server in
``deploy/upload-server`` keeps one build of the plugin (a zip of its repository, sent with
``canopener-cli team-admin publish``) and a list of installs, each known by its serial
(``core.install``). The owner approves installs; approved ones download the build and install it.

How an install gets in, either way:

* **Invite.** ``team-admin invite "Dave's laptop"`` prints a link (``https://<server>/invite/<code>``,
  single use, expires). Pasted into **Help > Team access**, it brings the server's address with it
  and approves this install in one step.
* **Request.** The install sends its serial to a server address you give it; the owner approves it
  (``team-admin approve CO-XXXX-XXXX``) and its next check finds it approved.

Either way the install makes its own secret key and sends it once; the server keeps only its
hash. The key, the server's address and what's installed are kept in
``~/.config/canopener/team.json`` (readable only by this user), never in ``settings.json``.

A **check** (``check()``) asks the server what this install may have. Approved and there's a
newer build: ``Status.update`` says so, and ``install_update()`` downloads it, checks its SHA-256,
unpacks it to ``~/.local/share/canopener/team-plugin`` and ``pip install -e``'s it into the app's
environment (no root), as GitHub plugins are. The first install after joining happens straight
away; later ones wait for the user to say yes (the startup check asks). **Withdrawn** (revoked or
forgotten on the server, or the server says this is another computer): the plugin is uninstalled
and its folder removed, and the user is told. Offline, nothing changes: what's installed keeps
working, however long.

This is access control, not copy protection: anyone who can read a computer's files can copy a
plugin installed on it. Standard library only.
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass

from .. import __version__
from . import config, install

STATE_FILE = "team.json"
TIMEOUT = 30
DOWNLOAD_TIMEOUT = 300
MAX_BUNDLE = 200 * 1024 * 1024
MAX_UNPACKED = 1024 * 1024 * 1024
MAX_MEMBERS = 20000
PIP_TIMEOUT = 600
CHECK_EVERY = 20 * 3600                 # seconds: the "once a day" check runs when the last is this old
USER_AGENT = f"CANopener/{__version__}"
INVITE_RE = re.compile(r"^(?P<base>.+?)/invite/(?P<code>[A-Za-z0-9-]{6,40})/?$")

NONE, PENDING, APPROVED, WITHDRAWN = "none", "pending", "approved", "withdrawn"


class TeamError(Exception):
    """Something went wrong, worded for the user. ``offline``: no connection (try later)."""

    def __init__(self, text: str, offline: bool = False):
        super().__init__(text)
        self.offline = offline


@dataclass
class Status:
    state: str                  # NONE / PENDING / APPROVED / WITHDRAWN
    message: str
    ok: bool = True             # the check itself worked (False: offline, server error)
    changed: bool = False       # the plugin was installed, updated or removed: restart to apply
    update: str = ""            # a newer build is there (its version), not installed yet
    version: str = ""           # the build installed here
    name: str = ""              # this install's name on the server
    server: str = ""


# ------------------------------------------------------------------------------------- state

def state_path() -> str:
    return os.path.join(config.config_dir(), STATE_FILE)


def plugin_dir() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "canopener", "team-plugin")


def load_state() -> dict:
    """This install's team state, or {} (none, or made for another install: a copied folder)."""
    try:
        with open(state_path()) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(st, dict) or st.get("install") != install.install_id():
        return {}
    return st


def save_state(st: dict | None):
    """Write it readable by this user only (None / {} removes it)."""
    path = state_path()
    if not st:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    config.ensure_dir(config.config_dir())
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(st, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def enrolled() -> bool:
    return bool(load_state().get("key"))


def due(now: float | None = None) -> bool:
    """Joined, and the last check is old enough for the daily one."""
    st = load_state()
    return bool(st.get("key")) and (now or time.time()) - float(st.get("checked") or 0) >= CHECK_EVERY


def describe() -> Status:
    """What this install has, from what's saved (no network)."""
    st = load_state()
    if not st.get("key"):
        return Status(NONE, "This computer hasn't joined team access.")
    s = _status_from(st)
    return s


def _status_from(st: dict, message: str = "", **kw) -> Status:
    state = st.get("status") or PENDING
    if not message:
        if state == PENDING:
            message = "Waiting for approval. Read out this computer's serial to whoever looks after team access."
        elif state == APPROVED:
            message = (f"Approved. Team plugin {st['version']} is installed." if st.get("version")
                       else "Approved. The team plugin isn't installed yet.")
        else:
            message = "Team access was withdrawn for this computer."
    return Status(state, message, version=st.get("version", ""), name=st.get("name", ""),
                  server=st.get("server", ""), **kw)


# -------------------------------------------------------------------------------------- HTTP

def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    return url


def _safe_base(base: str):
    u = urllib.parse.urlsplit(base)
    if u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1"):
        raise TeamError("The server's address must start with https:// (the key would travel in the clear).")


def _urllib_http(method: str, url: str, body: bytes | None = None, headers: dict | None = None,
                 timeout: float = TIMEOUT, dest: str | None = None, limit: int = MAX_BUNDLE) -> tuple[int, bytes]:
    """(status, body). With ``dest``, a 200 body goes to that file instead (up to ``limit``)."""
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if dest is None:
                return r.status, r.read()
            got = 0
            with open(dest, "wb") as f:
                for chunk in iter(lambda: r.read(1 << 20), b""):
                    got += len(chunk)
                    if got > limit:
                        raise TeamError("The download is bigger than a plugin should be; stopped.")
                    f.write(chunk)
            return r.status, b""
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise TeamError(f"Couldn't reach the server (no internet?): {getattr(e, 'reason', e)}", offline=True) from e


http = _urllib_http


def _call(base: str, method: str, path: str, data: dict | None = None, headers: dict | None = None,
          **kw) -> tuple[int, dict]:
    _safe_base(base)
    h = dict(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    status, raw = http(method, base + path, body, h, **kw)
    try:
        js = json.loads(raw.decode() or "null") if raw else None
    except (ValueError, UnicodeDecodeError):
        js = None
    if not isinstance(js, dict):
        js = {"error": raw[:200].decode(errors="replace").strip() if raw else ""} if status >= 300 else {}
    if status >= 500:
        raise TeamError(f"Server error: {js.get('error') or f'HTTP {status}'}", offline=True)
    return status, js


def _install_headers(st: dict) -> dict:
    return {"Authorization": f"Bearer {st['key']}", "X-Install": st["install"], "X-Machine": install.machine(),
            "X-App-Version": __version__, "X-Pack-Version": st.get("version", "")}


def parse_invite(text: str) -> tuple[str, str]:
    """``(server address, code)`` from an invite link."""
    m = INVITE_RE.match((text or "").strip())
    if not m:
        raise TeamError("That isn't an invite link. It looks like https://<server>/invite/XXXX-XXXX-XXXX: "
                        "paste the whole link you were sent.")
    return normalize_url(m.group("base")), m.group("code").upper()


def default_name() -> str:
    """A name to suggest for this computer: user on host."""
    import getpass
    import socket
    try:
        return f"{getpass.getuser()} on {socket.gethostname()}"
    except Exception:           # noqa: BLE001 - only a suggestion
        return socket.gethostname()


def _new_state(base: str, name: str) -> dict:
    return {"server": base, "install": install.install_id(), "key": secrets.token_urlsafe(32),
            "name": name.strip()[:80], "status": PENDING, "joined": time.time()}


# ---------------------------------------------------------------------------- joining, checking

def join(link: str, name: str = "", runner=subprocess.run, python: str = sys.executable, progress=None) -> Status:
    """Use an invite link: approved straight away, then the plugin is installed."""
    base, code = parse_invite(link)
    st = _new_state(base, name)
    status, js = _call(base, "POST", "/api/team/redeem",
                       {"code": code, "install": st["install"], "serial": install.serial(),
                        "machine": install.machine(), "key": st["key"], "name": st["name"]})
    if status >= 300:
        raise TeamError(js.get("error") or f"The server refused the invite (HTTP {status}).")
    st.update(status=js.get("status") or APPROVED, name=js.get("name") or st["name"])
    old = load_state()
    if old.get("dist"):                 # joining again: keep track of what's installed
        st.update({k: old[k] for k in ("dist", "version", "sha256") if k in old})
    save_state(st)
    return check(runner=runner, python=python, progress=progress, install_updates=True)


def request(server: str, name: str = "") -> Status:
    """Ask a server for access; it waits for the owner's approval."""
    base = normalize_url(server)
    if not base:
        raise TeamError("Which server? Enter the address you were given.")
    st = _new_state(base, name)
    status, js = _call(base, "POST", "/api/team/requests",
                       {"install": st["install"], "serial": install.serial(), "machine": install.machine(),
                        "key": st["key"], "name": st["name"]})
    if status >= 300:
        raise TeamError(js.get("error") or f"The server refused the request (HTTP {status}).")
    st["status"] = js.get("status") or PENDING
    save_state(st)
    return _status_from(st, f"Request sent. Read out this computer's serial, {install.serial()}, to whoever "
                            "looks after team access; once they approve it, check again.")


def check(runner=subprocess.run, python: str = sys.executable, progress=None,
          install_updates: bool | None = None) -> Status:
    """Ask the server. Withdrawn: the plugin is removed. A newer build: installed when
    ``install_updates`` is True, or when there's none here yet and it isn't False; otherwise
    reported in ``Status.update``. Offline: nothing changes (``ok`` False)."""
    st = load_state()
    if not st.get("key"):
        return describe()
    try:
        status, js = _call(st["server"], "GET", "/api/team/me", headers=_install_headers(st))
    except TeamError as e:
        return _status_from(st, str(e), ok=False)
    if status in (401, 403, 404) and js.get("status") in ("unknown", "other_computer", "revoked"):
        return _withdraw(st, js.get("error") or "", runner, python)
    if status >= 300:
        return _status_from(st, js.get("error") or f"The server answered HTTP {status}.", ok=False)
    if js.get("status") == "revoked":
        return _withdraw(st, "", runner, python)
    st.update(status=js.get("status") or PENDING, name=js.get("name") or st.get("name", ""), checked=time.time())
    save_state(st)
    pack = js.get("pack") if st["status"] == APPROVED else None
    if not pack or not pack.get("sha256"):
        msg = "" if st["status"] != APPROVED or st.get("version") else \
            "Approved, but there's no team plugin on the server yet."
        return _status_from(st, msg)
    if pack["sha256"] == st.get("sha256"):
        return _status_from(st)
    first = not st.get("dist")
    if install_updates or (install_updates is None and first):
        return install_update(pack, runner=runner, python=python, progress=progress)
    return _status_from(st, f"Team plugin {pack.get('version') or 'update'} is available.",
                        update=pack.get("version") or "new")


def install_update(pack: dict | None = None, runner=subprocess.run, python: str = sys.executable,
                   progress=None) -> Status:
    """Download the server's build and install it (``pack`` from a check, else asked for)."""
    st = load_state()
    if st.get("status") != APPROVED:
        return describe()
    if pack is None:
        status, js = _call(st["server"], "GET", "/api/team/me", headers=_install_headers(st))
        pack = js.get("pack") if status < 300 else None
        if not pack:
            return _status_from(st, js.get("error") or "There's nothing to install.", ok=status < 300)
    if progress:
        progress(f"Downloading the team plugin {pack.get('version', '')}…")
    with tempfile.TemporaryDirectory(prefix="canopener-team-") as tmp:
        zpath = os.path.join(tmp, "bundle.zip")
        try:
            status, raw = http("GET", st["server"] + "/api/team/pack/bundle.zip", None, _install_headers(st),
                               timeout=DOWNLOAD_TIMEOUT, dest=zpath)
        except TeamError as e:
            return _status_from(st, str(e), ok=False)
        if status != 200:
            return _status_from(st, f"The download was refused (HTTP {status}).", ok=False)
        if _sha256(zpath) != pack["sha256"]:
            return _status_from(st, "The download didn't match its checksum; nothing was changed. Try again.",
                                ok=False)
        if progress:
            progress("Installing it into CANopener's Python…")
        try:
            dist = _install_bundle(zpath, st, runner, python)
        except TeamError as e:
            return _status_from(st, str(e), ok=False)
    st.update(dist=dist, version=pack.get("version", ""), sha256=pack["sha256"], installed=time.time())
    save_state(st)
    return _status_from(st, f"Team plugin {st['version']} installed. Restart CANopener to use it.", changed=True)


def leave(runner=subprocess.run, python: str = sys.executable) -> Status:
    """Stop using team access here: the plugin is removed and the key forgotten."""
    st = load_state()
    removed = bool(st.get("dist")) or os.path.exists(plugin_dir())
    if st:
        _remove(st, runner, python)
    save_state(None)
    return Status(NONE, "Left team access" + (": the team plugin is removed. Restart CANopener." if removed
                                              else "."), changed=removed)


def _withdraw(st: dict, why: str, runner, python) -> Status:
    """No asking: the plugin goes. If pip couldn't uninstall it, the next check tries again."""
    had = bool(st.get("dist")) or os.path.exists(plugin_dir())
    gone = _remove(st, runner, python)
    for k in ("version", "sha256") + (("dist",) if gone else ()):
        st.pop(k, None)
    st.update(status=WITHDRAWN, checked=time.time())
    save_state(st)
    msg = "Team access was withdrawn for this computer" + (f" ({why})" if why else "") + "."
    if had:
        msg += " The team plugin is removed: restart CANopener." if gone else \
            " Its files are deleted, but pip couldn't uninstall it; the next check tries again."
    return _status_from(st, msg, changed=had)


# ----------------------------------------------------------------------- installing the bundle

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract(zpath: str, dest: str) -> str:
    """Unpack safely; returns the folder holding pyproject.toml (the zip's top folder, if any)."""
    try:
        with zipfile.ZipFile(zpath) as z:
            infos = z.infolist()
            if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_UNPACKED:
                raise TeamError("The team plugin's zip is far bigger than a plugin should be; not installed.")
            for i in infos:
                name = i.filename
                if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/") or ":" in name:
                    raise TeamError(f"The team plugin's zip holds an unsafe path ({name!r}); not installed.")
            z.extractall(dest)
    except zipfile.BadZipFile as e:
        raise TeamError("The download isn't a zip file; not installed.") from e
    root = dest
    entries = [e for e in os.listdir(dest) if not e.startswith(".")]
    if not os.path.exists(os.path.join(dest, "pyproject.toml")) and len(entries) == 1 \
            and os.path.isdir(os.path.join(dest, entries[0])):
        root = os.path.join(dest, entries[0])
    if not os.path.exists(os.path.join(root, "pyproject.toml")):
        raise TeamError("The team plugin's zip has no pyproject.toml; not installed.")
    return root


def dist_name(folder: str) -> str:
    """The package name in a pyproject.toml ([project] name)."""
    with open(os.path.join(folder, "pyproject.toml"), encoding="utf-8") as f:
        text = f.read()
    try:
        import tomllib
        return str(tomllib.loads(text).get("project", {}).get("name") or "")
    except ImportError:             # Python 3.10
        section = ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("["):
                section = line
            elif section == "[project]":
                m = re.match(r"""name\s*=\s*["']([^"']+)["']""", line)
                if m:
                    return m.group(1)
    return ""


def _installed_elsewhere(name: str) -> bool:
    """A package of this name is installed, and not from our folder (from GitHub, say)."""
    from importlib import metadata
    try:
        d = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return False
    url = ""
    try:
        url = json.loads(d.read_text("direct_url.json") or "{}").get("url", "")
    except ValueError:
        pass
    return os.path.realpath(plugin_dir()) not in urllib.parse.unquote(url)


def _pip(args: list[str], python: str, runner) -> tuple[bool, str]:
    try:
        r = runner([python, "-m", "pip", *args], capture_output=True, text=True, timeout=PIP_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    tail = "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-6:])
    return r.returncode == 0, tail


def _install_bundle(zpath: str, st: dict, runner, python) -> str:
    target = plugin_dir()
    parent = config.ensure_dir(os.path.dirname(target))
    new, old = target + ".new", target + ".old"
    for d in (new, old):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(new)
    try:
        root = _extract(zpath, new)
        name = dist_name(root)
        if not name:
            raise TeamError("The team plugin's pyproject.toml has no [project] name; not installed.")
        if not st.get("dist") and _installed_elsewhere(name):
            raise TeamError(f"{name} is already installed on this computer another way (from GitHub?), so "
                            "it's left as it is. Remove that copy first to use team access.")
        staged = os.path.join(parent, ".team-plugin.staged")
        shutil.rmtree(staged, ignore_errors=True)
        os.rename(root, staged)
        if os.path.exists(target):
            os.rename(target, old)
        os.rename(staged, target)
        ok, why = _pip(["install", "--quiet", "-e", target], python, runner)
        if not ok:
            shutil.rmtree(target, ignore_errors=True)
            if os.path.exists(old):
                os.rename(old, target)
            raise TeamError(f"Downloaded, but pip couldn't install it:\n{why}")
        return name
    finally:
        shutil.rmtree(new, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)


def _remove(st: dict, runner, python) -> bool:
    """Uninstall the plugin and delete its folder. False when pip couldn't uninstall it."""
    ok = True
    if st.get("dist"):
        ok, _why = _pip(["uninstall", "--yes", st["dist"]], python, runner)
    shutil.rmtree(plugin_dir(), ignore_errors=True)
    return ok


# ------------------------------------------------------------------------------- the owner's side

def admin_token() -> str:
    """CANOPENER_ADMIN_TOKEN, or the file ~/.config/canopener/admin-token."""
    env = os.environ.get("CANOPENER_ADMIN_TOKEN")
    if env:
        return env.strip()
    try:
        with open(os.path.join(config.config_dir(), "admin-token")) as f:
            return f.read().strip()
    except OSError:
        return ""


class Admin:
    """The owner's calls (an admin token): installs, invites, publishing the plugin."""

    def __init__(self, server: str, token: str):
        self.base, self.token = normalize_url(server), token

    def _do(self, method: str, path: str, data: dict | None = None, raw: bytes | None = None,
            headers: dict | None = None) -> dict:
        h = {"Authorization": f"Bearer {self.token}", **(headers or {})}
        if raw is not None:
            _safe_base(self.base)
            status, body = http(method, self.base + path, raw, {**h, "Content-Type": "application/zip"},
                                timeout=DOWNLOAD_TIMEOUT)
            try:
                js = json.loads(body.decode() or "{}")
            except (ValueError, UnicodeDecodeError):
                js = {"error": body[:200].decode(errors="replace")}
        else:
            status, js = _call(self.base, method, path, data, h)
        if status >= 300:
            raise TeamError(js.get("error") or f"HTTP {status}")
        return js

    def installs(self) -> list[dict]:
        return self._do("GET", "/api/team/installs").get("installs", [])

    def find(self, who: str) -> dict:
        """An install by serial (CO-XXXX-XXXX), ID (or its start) or exact name."""
        rows = self.installs()
        s = install.normalize_serial(who)
        hits = [r for r in rows if s and r.get("serial") == s] or \
               [r for r in rows if len(who) >= 4 and r["install"].startswith(who.lower())] or \
               [r for r in rows if r.get("name", "").lower() == who.strip().lower()]
        if len(hits) != 1:
            raise TeamError(f"No install matches {who!r}." if not hits else
                            f"{who!r} matches {len(hits)} installs; use the serial.")
        return hits[0]

    def set(self, who: str, **fields) -> dict:
        return self._do("POST", f"/api/team/installs/{self.find(who)['install']}", fields)

    def forget(self, who: str) -> dict:
        return self._do("DELETE", f"/api/team/installs/{self.find(who)['install']}")

    def invite(self, name: str, days: float = 7) -> dict:
        return self._do("POST", "/api/team/invites", {"name": name, "days": days})

    def invites(self) -> list[dict]:
        return self._do("GET", "/api/team/invites").get("invites", [])

    def uninvite(self, invite_id: str) -> dict:
        return self._do("DELETE", f"/api/team/invites/{invite_id}")

    def pack(self) -> dict:
        return self._do("GET", "/api/team/pack")

    def publish(self, zip_path: str, version: str) -> dict:
        with open(zip_path, "rb") as f:
            data = f.read()
        return self._do("PUT", "/api/team/pack", raw=data, headers={"X-Pack-Version": version})


SKIP_PARTS = {".git", "__pycache__", ".venv", "venv", "build", "dist", ".pytest_cache", ".ruff_cache"}


def make_bundle(path: str, out_dir: str, version: str = "", runner=subprocess.run) -> tuple[str, str, str]:
    """``(zip path, version, note)`` for publishing: a zip as it is; a git checkout as ``git
    archive HEAD`` (committed files only; version from ``git describe``); any other folder zipped
    without caches and build output."""
    path = os.path.abspath(os.path.expanduser(path))
    stamp = time.strftime("%Y-%m-%d.%H%M")
    if os.path.isfile(path):
        if not zipfile.is_zipfile(path):
            raise TeamError(f"{path} isn't a zip file or a folder.")
        return path, version or stamp, ""
    if not os.path.exists(os.path.join(path, "pyproject.toml")):
        raise TeamError(f"{path} has no pyproject.toml: point at the plugin's folder.")
    out = os.path.join(out_dir, "team-plugin.zip")
    note = ""
    if os.path.exists(os.path.join(path, ".git")):
        r = runner(["git", "-C", path, "archive", "--format=zip", "-o", out, "HEAD"], capture_output=True, text=True)
        if r.returncode != 0:
            raise TeamError(f"git archive failed: {(r.stderr or '').strip()}")
        if not version:
            d = runner(["git", "-C", path, "describe", "--tags", "--always"], capture_output=True, text=True)
            version = d.stdout.strip() or stamp
        dirty = runner(["git", "-C", path, "status", "--porcelain", "--untracked-files=no"],
                       capture_output=True, text=True).stdout.strip()
        if dirty:
            note = "Uncommitted changes aren't included (only what's committed is published)."
        return out, version, note
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_PARTS and not d.endswith(".egg-info"))
            for name in sorted(files):
                if name.endswith((".pyc", ".tmp")):
                    continue
                full = os.path.join(dirpath, name)
                z.write(full, os.path.relpath(full, path))
    return out, version or stamp, ""
