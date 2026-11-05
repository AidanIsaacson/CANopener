"""Install and update plugins from GitHub repositories (private ones too) from inside the app.

A plugin source is a GitHub repository holding a pip-installable plugin package
(``plugins.py``). Installing one is what you'd otherwise type in a terminal:

1. ``git clone`` it (by default to ``~/<repo name>``). Private repositories are read with the
   GitHub sign-in (``core.github``), passed to git through its environment only: the token is
   never written into the checkout;
2. ``pip install -e`` it into the app's own Python environment (no root);
3. restart the app, which loads plugins at start.

Updating is ``git pull --ff-only`` (local edits are left alone, or the update stops), with pip
again only when ``pyproject.toml`` changed. A folder already at the target path that isn't a git
checkout (a plugin unzipped from a ZIP download) is moved aside to ``<folder>.old-<date>``, never
deleted. The list of sources is the ``plugins.sources`` setting (``core.settings``).
"""

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

from . import github, updates

SETTING = "plugins.sources"
GIT_TIMEOUT = 60
CLONE_TIMEOUT = 300
PIP_TIMEOUT = 600
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass
class Source:
    repo: str                   # "owner/name" on GitHub
    dir: str = ""               # where the checkout lives ("" = ~/<name>)
    url: str = ""               # clone URL ("" = https://github.com/<repo>.git); tests use local paths

    @property
    def name(self) -> str:
        return self.repo.split("/")[-1]

    @property
    def path(self) -> str:
        return os.path.expanduser(self.dir or os.path.join("~", self.name))

    @property
    def clone_url(self) -> str:
        return self.url or f"https://github.com/{self.repo}.git"

    def as_dict(self) -> dict:
        return {k: v for k, v in (("repo", self.repo), ("dir", self.dir), ("url", self.url)) if v}


def sources(settings) -> list[Source]:
    out = []
    for d in settings.get(SETTING) or []:
        if isinstance(d, dict) and d.get("repo"):
            out.append(Source(d["repo"], d.get("dir", ""), d.get("url", "")))
    return out


def add_source(settings, repo: str, dir: str = "", url: str = "") -> Source:
    repo = repo.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not REPO_RE.match(repo):
        raise ValueError(f"{repo!r} isn't a GitHub repository name (owner/name)")
    keep = [s for s in sources(settings) if s.repo.lower() != repo.lower()]
    src = Source(repo, dir, url)
    settings.set(SETTING, [s.as_dict() for s in keep + [src]])
    return src


def remove_source(settings, repo: str):
    """Forget a source. Its folder and the installed package stay (nothing is deleted)."""
    settings.set(SETTING, [s.as_dict() for s in sources(settings) if s.repo.lower() != repo.lower()])


# ---------------------------------------------------------------------------------------- git

def _git(args, cwd: str | None, token: str | None, timeout=GIT_TIMEOUT, runner=subprocess.run):
    argv = ["git", *(["-C", cwd] if cwd else []), *args]
    return runner(argv, capture_output=True, text=True, timeout=timeout,
                  env={**os.environ, **updates.GIT_ENV, **github.git_env(token)})


def _why(r) -> str:
    text = (r.stderr or r.stdout or "").strip()
    if re.search(r"could not read Username|Authentication failed|returned error: 40[134]|not found", text, re.I):
        return ("GitHub refused access. Sign in to GitHub (System > Software > Plugins), and make sure the "
                "CANopener GitHub App is installed on this repository.")
    lines = text.splitlines()
    return lines[-1] if lines else f"git exited with {r.returncode}"


def _is_checkout(path: str) -> bool:
    return os.path.exists(os.path.join(path, ".git"))


def _same_repo(remote: str, src: Source) -> bool:
    norm = remote.strip().lower().removesuffix(".git").rstrip("/")
    return norm == src.clone_url.lower().removesuffix(".git").rstrip("/") or norm.endswith("/" + src.repo.lower())


@dataclass
class PluginStatus:
    repo: str
    path: str
    ok: bool
    message: str
    installed: bool = False     # a checkout is there
    behind: int = 0
    ahead: int = 0
    commits: list[str] = field(default_factory=list)
    dirty: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.ok and self.behind > 0 and not self.ahead


def check(src: Source, token: str | None = None, runner=subprocess.run, fetch: bool = True) -> PluginStatus:
    """Is it installed, and is there something newer? Only fetches; changes nothing."""
    path = src.path
    if not _is_checkout(path):
        note = " (a copy that isn't from git is there: installing moves it aside)" if os.path.exists(path) else ""
        return PluginStatus(src.repo, path, True, f"Not installed{note}.")
    try:
        remote = _git(["remote", "get-url", "origin"], path, token, runner=runner)
        if remote.returncode != 0 or not _same_repo(remote.stdout, src):
            return PluginStatus(src.repo, path, False, f"{path} is a checkout of something else "
                                f"({remote.stdout.strip() or 'no origin'}); left alone.", installed=True)
        if fetch:
            r = _git(["fetch", "--quiet"], path, token, runner=runner)
            if r.returncode != 0:
                return PluginStatus(src.repo, path, False, f"Couldn't check for updates: {_why(r)}", installed=True)
        counts = _git(["rev-list", "--left-right", "--count", "HEAD...@{u}"], path, token, runner=runner).stdout.split()
        ahead, behind = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (0, 0)
        log = _git(["log", "--format=%s", "HEAD..@{u}"], path, token, runner=runner).stdout
        dirty = [ln[3:] for ln in _git(["status", "--porcelain", "--untracked-files=no"], path, token,
                                       runner=runner).stdout.splitlines() if len(ln) > 3]
    except (OSError, subprocess.SubprocessError) as e:
        return PluginStatus(src.repo, path, False, f"Couldn't run git: {e}", installed=True)
    s = PluginStatus(src.repo, path, True, "", True, behind, ahead, [c for c in log.splitlines() if c.strip()], dirty)
    if behind and ahead:
        s.message = f"An update is there, but this copy has {ahead} change(s) of its own, so it can't update itself."
    elif behind:
        s.message = f"Update available: {behind} change(s)."
    else:
        s.message = "Up to date."
    return s


def _pip(path: str, python: str, runner) -> tuple[bool, str]:
    r = runner([python, "-m", "pip", "install", "--quiet", "-e", path], capture_output=True, text=True,
               timeout=PIP_TIMEOUT)
    if r.returncode != 0:
        tail = "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-6:])
        return False, f"Downloaded, but pip couldn't install it:\n{tail}"
    return True, ""


def install(src: Source, token: str | None = None, runner=subprocess.run, python: str = sys.executable,
            progress=None, now=time.localtime) -> tuple[bool, str]:
    """Clone (or update, when already there) and pip-install. Returns (ok, message)."""
    path = src.path
    if _is_checkout(path):
        return update(src, token, runner=runner, python=python, progress=progress)
    note = ""
    try:
        if os.path.exists(path):
            aside = f"{path}.old-{time.strftime('%Y%m%d-%H%M%S', now())}"
            os.rename(path, aside)
            note = f"\n\nThe copy that was already at {path} wasn't from git: it's kept as {aside}."
        if progress:
            progress(f"Downloading {src.repo}…")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        r = _git(["clone", "--quiet", src.clone_url, path], None, token, timeout=CLONE_TIMEOUT, runner=runner)
        if r.returncode != 0:
            return False, f"Couldn't download {src.repo}: {_why(r)}{note}"
        if progress:
            progress("Installing it into CANopener's Python…")
        ok, msg = _pip(path, python, runner)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Install failed: {e}{note}"
    if not ok:
        return False, msg + note
    return True, f"Installed {src.repo}. Restart CANopener to load it.{note}"


def update(src: Source, token: str | None = None, runner=subprocess.run, python: str = sys.executable,
           progress=None) -> tuple[bool, str]:
    path = src.path
    st = check(src, token, runner=runner)
    if not st.ok:
        return False, st.message
    if not st.installed:
        return install(src, token, runner=runner, python=python, progress=progress)
    if st.ahead:
        return False, st.message
    if not st.behind:
        return True, f"{src.repo} is up to date."
    try:
        old = _git(["rev-parse", "HEAD"], path, token, runner=runner).stdout.strip()
        if progress:
            progress(f"Downloading the update to {src.repo}…")
        r = _git(["pull", "--ff-only", "--quiet"], path, token, timeout=CLONE_TIMEOUT, runner=runner)
        if r.returncode != 0:
            return False, f"The update didn't download: {_why(r)}"
        changed = _git(["diff", "--name-only", old, "HEAD"], path, token, runner=runner).stdout.split()
        if "pyproject.toml" in changed:
            if progress:
                progress("Updating its Python package…")
            ok, msg = _pip(path, python, runner)
            if not ok:
                return False, msg
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Update failed: {e}"
    return True, f"Updated {src.repo} ({st.behind} change(s)). Restart CANopener to use it."
