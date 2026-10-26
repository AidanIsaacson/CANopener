"""Optional plugins: separately installed packages that add to CANopener.

This keeps the public app generic while letting a private or OEM-specific package add tools.
A plugin is a Python module named by an entry point in the ``canopener.plugins`` group::

    # in the plugin's pyproject.toml
    [project.entry-points."canopener.plugins"]
    mytool = "mytool.canopener_plugin"

(For development, ``CANOPENER_PLUGINS=mod1,mod2`` loads modules by name without installing.)

The module may define any of these; all are optional:

* ``PLUGIN_NAME`` / ``PLUGIN_VERSION``: strings shown in Help > About
* ``profile_dirs() -> list[str]``: extra folders of profile JSON files
* ``register_cli(subparsers)``: add ``canopener-cli`` sub-commands (argparse; use
  ``set_defaults(fn=...)``, the function gets the parsed args and returns an exit code)
* ``register_gui(api)``: add tabs / menu actions to the desktop app; ``api`` is a
  ``canopener.gui.main_window.PluginApi``
* ``register_device(api)``: add a mode (a home-screen tile and its screens) to the handheld UI
  with ``api.register_mode(canopener.device.modes.Mode(...))``; ``api`` is a
  ``canopener.device.modes.DeviceApi``. Keep the logic in Qt- and Pygame-free modules so both
  UIs can share it.

A plugin that transmits anything beyond J1939 requests must ask the user first: the main app is
read-only, and ``Session.send_frame`` refuses in listen-only mode.
"""

import importlib
import os
from dataclasses import dataclass, field
from importlib import metadata

GROUP = "canopener.plugins"


@dataclass
class Plugin:
    id: str
    module: object = None
    error: str | None = None
    name: str = ""
    version: str = ""
    profile_dirs: list[str] = field(default_factory=list)

    def hook(self, name: str):
        return getattr(self.module, name, None) if self.module is not None else None


_cache: list[Plugin] | None = None


def _targets() -> list[tuple[str, str]]:
    out = []
    try:
        eps = metadata.entry_points(group=GROUP)
    except TypeError:                       # Python < 3.10 style API
        eps = metadata.entry_points().get(GROUP, [])
    for ep in eps:
        out.append((ep.name, ep.value))
    for name in filter(None, (os.environ.get("CANOPENER_PLUGINS") or "").split(",")):
        out.append((name.strip(), name.strip()))
    return out


def discover(refresh: bool = False) -> list[Plugin]:
    """Load every plugin once. A plugin that fails to import is listed with its error."""
    global _cache
    if _cache is not None and not refresh:
        return _cache
    found = []
    seen = set()
    for pid, target in _targets():
        if pid in seen:
            continue
        seen.add(pid)
        p = Plugin(pid)
        try:
            p.module = importlib.import_module(target.split(":")[0])
            p.name = str(getattr(p.module, "PLUGIN_NAME", pid))
            p.version = str(getattr(p.module, "PLUGIN_VERSION", ""))
            dirs = p.hook("profile_dirs")
            p.profile_dirs = [d for d in (dirs() if dirs else []) if os.path.isdir(d)]
        except Exception as e:      # a broken plugin must not stop the app
            p.module, p.error = None, f"{type(e).__name__}: {e}"
        found.append(p)
    _cache = found
    return found


def loaded() -> list[Plugin]:
    return [p for p in discover() if p.module is not None]


def extra_profile_dirs() -> list[str]:
    return [d for p in loaded() for d in p.profile_dirs]
