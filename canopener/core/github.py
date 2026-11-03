"""Sign in to GitHub without a keyboard, and the few API calls plugin installs need.

Sign-in is GitHub's *device flow* (OAuth 2.0 device authorization grant): the app asks GitHub for
a short code, shows it with the address ``github.com/login/device``, and the user approves it on
their phone or laptop. Nothing is typed on the handheld, and no password ever reaches it.

The client ID belongs to a GitHub App with "Enable Device Flow" ticked (docs/DEVELOPING.md,
"Private plugins"). It is public by design (GitHub shows it on the approval page) and is not a
secret. A GitHub App token can only read what the App was granted (Contents: read-only) on the
repositories it was installed on, so a lost handheld exposes those repositories read-only and
nothing else. Untick "Expire user authorization tokens" in the App's settings: refreshing an
expiring token needs the App's client secret, which has no place on a device.

The token is kept in ``~/.config/canopener/github-token.json``, readable only by the user.
Standard library only (urllib).
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import config

# The CANopener GitHub App's client ID ("Iv23..."). Empty: set "github.client_id" in settings.json
# or CANOPENER_GITHUB_CLIENT_ID instead.
DEFAULT_CLIENT_ID = ""
DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
API = "https://api.github.com"
TOKEN_FILE = "github-token.json"
TIMEOUT = 20
USER_AGENT = "CANopener"


class GitHubError(Exception):
    """Anything that went wrong talking to GitHub, worded for the user."""


class SignInNeeded(GitHubError):
    """No token, or GitHub no longer accepts it (revoked, expired)."""


def client_id(settings=None) -> str:
    env = os.environ.get("CANOPENER_GITHUB_CLIENT_ID")
    if env:
        return env.strip()
    if settings is not None:
        v = settings.get("github.client_id")
        if v:
            return str(v).strip()
    return DEFAULT_CLIENT_ID


# ---------------------------------------------------------------------------------------- HTTP

def _urllib_http(method: str, url: str, data: dict | None = None, headers: dict | None = None):
    """(status, parsed JSON or None). Tests replace ``http`` with a fake."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise GitHubError(f"Couldn't reach GitHub (no internet?): {reason}") from e
    try:
        return status, json.loads(raw.decode() or "null")
    except ValueError:
        return status, None


http = _urllib_http


# ---------------------------------------------------------------------------------------- device flow

@dataclass
class DeviceCode:
    device_code: str
    user_code: str              # what the user types on github.com/login/device, e.g. "WDJB-MJHT"
    verification_uri: str
    interval: float             # seconds between polls (GitHub raises it with "slow_down")
    expires_at: float           # time.monotonic() deadline


def start_sign_in(cid: str, clock=time.monotonic) -> DeviceCode:
    if not cid:
        raise GitHubError("No GitHub App client ID is set, so sign-in can't start "
                          "(see docs/DEVELOPING.md, \"Private plugins\").")
    status, js = http("POST", DEVICE_CODE_URL, {"client_id": cid})
    if status != 200 or not isinstance(js, dict) or "device_code" not in js:
        why = (js or {}).get("error_description") or (js or {}).get("error") or f"HTTP {status}"
        if (js or {}).get("error") == "device_flow_disabled":
            why = "device flow isn't enabled on the GitHub App (its settings: Enable Device Flow)"
        raise GitHubError(f"GitHub didn't start the sign-in: {why}")
    return DeviceCode(js["device_code"], js["user_code"], js.get("verification_uri", "https://github.com/login/device"),
                      float(js.get("interval", 5)), clock() + float(js.get("expires_in", 900)))


def poll_once(cid: str, dc: DeviceCode) -> str | None:
    """One poll: the token once the user has approved, None while still waiting."""
    status, js = http("POST", TOKEN_URL, {"client_id": cid, "device_code": dc.device_code,
                                          "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    js = js if isinstance(js, dict) else {}
    if js.get("access_token"):
        return js["access_token"]
    err = js.get("error", f"HTTP {status}")
    if err == "authorization_pending":
        return None
    if err == "slow_down":
        dc.interval = float(js.get("interval", dc.interval + 5))
        return None
    if err == "expired_token":
        raise GitHubError("The code expired before it was approved. Start the sign-in again.")
    if err == "access_denied":
        raise GitHubError("Sign-in was cancelled on GitHub.")
    raise GitHubError(f"Sign-in failed: {js.get('error_description') or err}")


def wait_for_token(cid: str, dc: DeviceCode, cancelled=lambda: False, sleep=time.sleep,
                   clock=time.monotonic) -> str | None:
    """Poll until approved (the token), cancelled (None), or the code expires (GitHubError)."""
    while not cancelled():
        if clock() >= dc.expires_at:
            raise GitHubError("The code expired before it was approved. Start the sign-in again.")
        waited = 0.0
        while waited < dc.interval:             # sleep in small steps so Cancel is quick
            if cancelled():
                return None
            sleep(0.25)
            waited += 0.25
        token = poll_once(cid, dc)
        if token:
            return token
    return None


# ---------------------------------------------------------------------------------------- token store

def token_path() -> str:
    return os.path.join(config.config_dir(), TOKEN_FILE)


@dataclass
class Account:
    token: str
    login: str = ""
    saved: float = field(default_factory=time.time)


def load_account() -> Account | None:
    try:
        with open(token_path()) as f:
            js = json.load(f)
        return Account(js["token"], js.get("login", ""), js.get("saved", 0)) if js.get("token") else None
    except (OSError, ValueError, KeyError):
        return None


def save_account(acct: Account):
    path = token_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"token": acct.token, "login": acct.login, "saved": acct.saved}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def sign_out() -> bool:
    try:
        os.unlink(token_path())
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------------------- API

def api_get(path: str, token: str | None):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, js = http("GET", API + path, None, headers)
    if status == 401:
        raise SignInNeeded("GitHub didn't accept the saved sign-in (expired or revoked). Sign in again.")
    if status >= 400:
        msg = (js or {}).get("message") if isinstance(js, dict) else None
        raise GitHubError(f"GitHub said: {msg or f'HTTP {status}'}")
    return js


def whoami(token: str) -> str:
    return api_get("/user", token).get("login", "")


def repositories(token: str) -> list[str]:
    """``owner/name`` of every repository this sign-in can read. For a GitHub App token that's
    the repositories the App is installed on; for anything else, the user's own list."""
    names = []
    insts = api_get("/user/installations?per_page=100", token)
    for inst in (insts or {}).get("installations", []):
        page = 1
        while True:
            js = api_get(f"/user/installations/{inst['id']}/repositories?per_page=100&page={page}", token)
            repos = (js or {}).get("repositories", [])
            names += [r["full_name"] for r in repos]
            if len(repos) < 100:
                break
            page += 1
    if not insts or not insts.get("installations"):
        names = [r["full_name"] for r in api_get("/user/repos?per_page=100&sort=pushed", token) or []]
    return sorted(set(names), key=str.lower)


def git_env(token: str | None) -> dict:
    """Environment that lets git (and only git started with it) read github.com with the token.
    Passed through the environment, never the command line or .git/config, so it isn't saved in
    the checkout or visible in the process list."""
    if not token:
        return {}
    import base64
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}
