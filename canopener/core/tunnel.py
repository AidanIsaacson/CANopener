"""A reverse SSH tunnel from this unit to the upload server's tunnel container, for hands-on help.

Remote help (``core.remote``) covers looking and asking. When a helper needs a real login (to try
code on the unit, look at the system, run a long session), this opens ``ssh -N -R`` to the server:
the unit's own SSH port appears on the server's loopback, and a helper who can already log in to
the server reaches the unit through it. The unit still decides who gets in: the helper needs a
key or password the unit's own SSH accepts.

The unit's tunnel key is made on first use and enrolled with the server through the upload
token (``POST /api/remote/tunnel``); on the server that key can do one thing, hold this tunnel.
Nothing here needs root. No Qt, no Pygame.
"""

import os
import shutil
import subprocess
import threading
import time

from . import config, upload

LOCAL_PORT = 22             # the unit's own sshd


class TunnelError(Exception):
    pass


def key_path() -> str:
    return os.path.join(config.config_dir(), "tunnel_key")


def ensure_key(run=subprocess.run) -> str:
    """The unit's tunnel public key (made the first time)."""
    path = key_path()
    if not os.path.exists(path):
        if not shutil.which("ssh-keygen"):
            raise TunnelError("ssh isn't installed on this unit (sudo apt install openssh-client).")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        r = run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "canopener-tunnel", "-f", path],
                capture_output=True, text=True)
        if r.returncode:
            raise TunnelError(f"couldn't make the tunnel key: {r.stderr.strip()}")
    with open(path + ".pub") as f:
        return " ".join(f.read().split()[:2])


def command(grant: dict) -> list[str]:
    """The ssh command for a server's answer ({host, port, user, listen_port})."""
    known = os.path.join(config.config_dir(), "tunnel_known_hosts")
    return ["ssh", "-N", "-T", "-i", key_path(), "-p", str(int(grant["port"])),
            "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known}", "-o", "IdentitiesOnly=yes",
            "-R", f"127.0.0.1:{int(grant['listen_port'])}:127.0.0.1:{LOCAL_PORT}",
            f"{grant['user']}@{grant['host']}"]


class Tunnel:
    """``start()`` enrols and connects in the background; ``state`` is off / connecting / open /
    ended: <why>. ``grant`` says where the unit can be reached once open."""

    def __init__(self, settings, popen=subprocess.Popen):
        self.settings, self._popen = settings, popen
        self.state = "off"
        self.grant: dict = {}
        self._proc = None
        self._thread = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def where(self) -> str:
        """For the helper: how to reach this unit while the tunnel is open."""
        return f"port {self.grant.get('listen_port')} on the server's own 127.0.0.1" if self.grant else ""

    def start(self):
        if self.running or self.state == "connecting":
            return
        self.state = "connecting"
        self._thread = threading.Thread(target=self._run, name="ssh-tunnel", daemon=True)
        self._thread.start()

    def _run(self):
        try:
            import json
            body = json.dumps({"device": upload.device_id(), "public_key": ensure_key()}).encode()
            self.grant = upload.HttpUploader()._call(self.settings, "POST", "/api/remote/tunnel", body, timeout=20)
            self._proc = self._popen(command(self.grant), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True)
        except (upload.UploadError, TunnelError, OSError) as e:
            self.state = f"ended: {e}"
            return
        time.sleep(2.5)                                 # a refused tunnel exits at once
        if self._proc.poll() is None:
            self.state = "open"
        code = self._proc.wait()
        err = (self._proc.stderr.read() or "").strip().splitlines() if self._proc.stderr else []
        if self.state != "ended: stopped here":
            self.state = "ended: " + (err[-1] if err else f"ssh stopped (code {code})")

    def stop(self):
        self.state = "ended: stopped here" if self.state in ("open", "connecting") else self.state
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
