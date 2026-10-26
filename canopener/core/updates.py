"""Check for and install updates from the app's own git checkout.

Both installers (``deploy/linux/install.sh``, ``deploy/raspberry-pi/install.sh``) clone the repo
and ``pip install -e`` it into a private environment, so the running code *is* the checkout. An
update is therefore:

1. ``git fetch`` and compare with the branch's upstream (``check``);
2. ``git pull --ff-only`` (never a merge: local edits are left alone, or the update stops);
3. re-run ``pip install -e`` in this environment only if ``pyproject.toml`` changed (new
   dependencies or commands);
4. restart the app.

None of that needs root. When an update changes ``deploy/`` (drivers, udev rules, sudoers, the
installers themselves) the full installer should run once more; ``installer_command`` says how.
That one asks for a password, so it's left to a terminal.
"""

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

from .. import __version__

GIT_TIMEOUT = 30
# Never wait on a prompt: no terminal to type a password or accept a host key into.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes", "LC_ALL": "C"}
RESTART_EXIT_CODE = 75      # exit status that asks systemd (Restart=on-failure) to start us again


def repo_root() -> str | None:
    """The git checkout the app runs from, or None (installed from a wheel, say)."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return root if os.path.exists(os.path.join(root, ".git")) else None


@dataclass
class UpdateStatus:
    ok: bool                        # the check itself worked
    message: str
    current: str = __version__
    latest: str | None = None       # version on the upstream branch
    behind: int = 0                 # commits to download
    ahead: int = 0                  # local commits not upstream (the update can't fast-forward)
    commits: list[str] = field(default_factory=list)     # subjects of the incoming commits
    dirty: list[str] = field(default_factory=list)       # locally modified files
    branch: str = ""
    changed: list[str] = field(default_factory=list)     # files the update changes

    @property
    def available(self) -> bool:
        return self.ok and self.behind > 0

    @property
    def installer_changed(self) -> bool:
        return any(f.startswith("deploy/") for f in self.changed)

    @property
    def deps_changed(self) -> bool:
        return "pyproject.toml" in self.changed


def _git(args: list[str], root: str, timeout: float = GIT_TIMEOUT, runner=subprocess.run):
    return runner(["git", "-C", root, *args], capture_output=True, text=True, timeout=timeout,
                  env={**os.environ, **GIT_ENV})


def _version_in(text: str) -> str | None:
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text or "")
    return m.group(1) if m else None


def check(fetch: bool = True, root: str | None = None, runner=subprocess.run) -> UpdateStatus:
    """Is there a newer version upstream? Never changes the checkout (only fetches)."""
    root = root or repo_root()
    if root is None:
        return UpdateStatus(False, "This copy of CANopener wasn't installed from git, so it can't update "
                                   "itself. Reinstall it with the installer (see the README).")
    try:
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], root, runner=runner).stdout.strip()
        upstream = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], root, runner=runner)
        if upstream.returncode != 0:
            return UpdateStatus(False, f"The branch '{branch}' doesn't follow a branch on GitHub, so there's "
                                       "nothing to update from.", branch=branch)
        if fetch:
            r = _git(["fetch", "--quiet"], root, runner=runner)
            if r.returncode != 0:
                why = (r.stderr or r.stdout).strip().splitlines()
                return UpdateStatus(False, "Couldn't reach GitHub to check for updates (no internet?)"
                                           + (f": {why[-1]}" if why else "."), branch=branch)
        counts = _git(["rev-list", "--left-right", "--count", "HEAD...@{u}"], root, runner=runner).stdout.split()
        ahead, behind = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (0, 0)
        log = _git(["log", "--format=%s", "HEAD..@{u}"], root, runner=runner).stdout
        changed = _git(["diff", "--name-only", "HEAD", "@{u}"], root, runner=runner).stdout.split()
        dirty = [line[3:] for line in _git(["status", "--porcelain", "--untracked-files=no"], root,
                                           runner=runner).stdout.splitlines() if len(line) > 3]
        latest = _version_in(_git(["show", "@{u}:canopener/__init__.py"], root, runner=runner).stdout)
        current = _version_in(_git(["show", "HEAD:canopener/__init__.py"], root, runner=runner).stdout) or __version__
    except (OSError, subprocess.SubprocessError) as e:
        return UpdateStatus(False, f"Couldn't run git: {e}")
    s = UpdateStatus(True, "", current=current, latest=latest, behind=behind, ahead=ahead,
                     commits=[c for c in log.splitlines() if c.strip()], dirty=dirty, branch=branch,
                     changed=changed if behind else [])
    if behind and ahead:
        s.message = (f"An update is available, but this copy has {ahead} change(s) of its own that aren't on "
                     "GitHub, so it can't update automatically.")
    elif behind:
        new = f" (version {latest})" if latest and latest != current else ""
        s.message = f"An update is available{new}: {behind} change(s)."
    else:
        s.message = f"CANopener {current} is up to date."
    return s


def apply(root: str | None = None, runner=subprocess.run, python: str = sys.executable, progress=None
          ) -> tuple[bool, str]:
    """Download and install the update. Returns (ok, message). Fast-forward only: a local change in
    the way stops it with git's explanation, and nothing is half-applied."""
    root = root or repo_root()
    if root is None:
        return False, "Not a git install: can't update in place."
    try:
        old = _git(["rev-parse", "HEAD"], root, runner=runner).stdout.strip()
        if progress:
            progress("Downloading the update…")
        r = _git(["pull", "--ff-only", "--quiet"], root, timeout=120, runner=runner)
        if r.returncode != 0:
            return False, "The update didn't download:\n" + (r.stderr or r.stdout).strip()
        changed = _git(["diff", "--name-only", old, "HEAD"], root, runner=runner).stdout.split() if old else []
        done = UpdateStatus(True, "", changed=changed)
        if done.deps_changed:
            if progress:
                progress("Updating the app's Python packages…")
            r = runner([python, "-m", "pip", "install", "--quiet", "-e", f"{root}[gui,device]"],
                       capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                return False, ("The code updated, but its Python packages didn't:\n" + (r.stderr or r.stdout).strip()
                               + f"\n\nRun the installer to finish: {installer_command(root)}")
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Update failed: {e}"
    msg = "Updated. Restart CANopener to use the new version."
    if done.installer_changed:
        msg += ("\n\nThis update also changes the system setup (adapter drivers, rules or permissions). "
                f"Run the installer once in a terminal (it asks for your password):\n  {installer_command(root)}")
    return True, msg


def installer_command(root: str | None = None) -> str:
    root = root or repo_root() or "~/CANopener"
    # The laptop installer keeps the environment in the repo (.venv); the Pi one in ~/canopener-venv.
    if os.path.abspath(sys.prefix) == os.path.join(os.path.abspath(root), ".venv") or not os.path.exists(
            "/etc/rpi-issue"):
        return f"{root}/deploy/linux/install.sh"
    return f"{root}/deploy/raspberry-pi/install.sh --sudoers --autostart"


def under_systemd() -> bool:
    """Started by the Pi's systemd user service (which restarts us when we exit with
    RESTART_EXIT_CODE)."""
    return bool(os.environ.get("INVOCATION_ID")) and os.environ.get("CANOPENER_SERVICE", "1") != "0"


def restart_command() -> list[str]:
    """How to start this same app again (same Python environment, same options)."""
    return [sys.executable, "-m", "canopener", *sys.argv[1:]]
