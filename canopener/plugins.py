"""Optional plugins: separately installed packages that add to CANopener.

This keeps the public app generic while letting a private or OEM-specific package add tools.
A plugin is a Python module named by an entry point in the ``canopener.plugins`` group::

    # in the plugin's pyproject.toml
    [project.entry-points."canopener.plugins"]
    mytool = "mytool.canopener_plugin"

**Bundled plugins** ship inside CANopener (``canopener/addons/``) and use the same hooks, so one
could move to a repository of its own unchanged. They're off until switched on
(``plugins.bundled`` in settings.json: System > Software > Plugins on the handheld, **Settings >
Built-in plugins** on the desktop, ``canopener-cli plugins enable ID``). ``BUNDLED`` lists them.

(For development, ``CANOPENER_PLUGINS=mod1,mod2`` loads modules by name without installing.
``CANOPENER_NO_INSTALLED_PLUGINS=1`` leaves the installed ones out: CANopener's own tests set it,
so they give the same result on a machine that has a plugin installed.)

The module may define any of these; all are optional:

* ``PLUGIN_NAME`` / ``PLUGIN_VERSION``: strings shown in Help > About
* ``profile_dirs() -> list[str]``: extra folders of profile JSON files
* ``register_cli(subparsers)``: add ``canopener-cli`` sub-commands (argparse; use
  ``set_defaults(fn=...)``, the function gets the parsed args and returns an exit code)
* ``register_gui(api)``: add tabs / menu actions to the desktop app; ``api`` is a
  ``canopener.gui.main_window.PluginApi``
* ``adapter_buses() -> list[dict]``: ways to put a Bluetooth OBD adapter onto another bus
  (``core.adapter_buses``, fields of ``can.elm327.BusSetup``): adapter commands only (AT / ST)
* ``register_device(api)``: add a mode (a home-screen tile and its screens) to the handheld UI
  with ``api.register_mode(canopener.device.modes.Mode(...))``; ``api`` is a
  ``canopener.device.modes.DeviceApi``. Keep the logic in Qt- and Pygame-free modules so both
  UIs can share it.

At import a plugin may also call ``core.upload.register_uploader`` (another place for captures to
go) or ``core.upload.set_default_server`` (the upload server's address and token for every unit
that installs the plugin).

A plugin that transmits anything beyond J1939 requests must ask the user first: the main app is
read-only, and ``Session.send_frame`` refuses in listen-only mode.
"""

import importlib
import os
from dataclasses import dataclass, field
from importlib import metadata

GROUP = "canopener.plugins"
NO_INSTALLED_ENV = "CANOPENER_NO_INSTALLED_PLUGINS"
BUNDLED_SETTING = "plugins.bundled"     # ids of the bundled plugins switched on


@dataclass(frozen=True)
class Bundled:
    id: str
    module: str
    name: str
    about: str


BUNDLED = (
    Bundled("zapi_console", "canopener.addons.zapi_console", "Zapi console",
            "a mode for Zapi controllers' serial console port: listen, record, work out the protocol"),
)


def bundled_enabled(settings=None) -> list[str]:
    """Ids of the bundled plugins switched on."""
    if settings is None:
        from .core.settings import Settings
        settings = Settings()
    on = settings.get(BUNDLED_SETTING) or []
    return [b.id for b in BUNDLED if b.id in on]


def set_bundled(settings, plugin_id: str, on: bool):
    """Switch a bundled plugin on or off (it loads or goes at the next start)."""
    if plugin_id not in {b.id for b in BUNDLED}:
        raise ValueError(f"no bundled plugin {plugin_id!r} (there are: {', '.join(b.id for b in BUNDLED)})")
    now = [i for i in (settings.get(BUNDLED_SETTING) or []) if i != plugin_id]
    settings.set(BUNDLED_SETTING, now + [plugin_id] if on else now)


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
    if os.environ.get(NO_INSTALLED_ENV):
        eps = []
    else:
        try:
            eps = metadata.entry_points(group=GROUP)
        except TypeError:                   # Python < 3.10 style API
            eps = metadata.entry_points().get(GROUP, [])
    for ep in eps:
        out.append((ep.name, ep.value))
    try:
        on = bundled_enabled()
    except Exception:               # noqa: BLE001 - an unreadable settings file must not stop the app
        on = []
    out += [(b.id, b.module) for b in BUNDLED if b.id in on]
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
