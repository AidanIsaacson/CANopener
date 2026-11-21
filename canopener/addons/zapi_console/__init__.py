"""Zapi console: a mode for Zapi controllers' serial console port (a bundled plugin).

Groundwork. It listens to the link between a Zapi console and its controller (one tap per
direction, docs/SERIAL-SNIFFING.md), shows each message with what we make of it, records it with
the Log tool, and runs the protocol analysis on what it heard. What we work out goes in
``protocol.py`` with its evidence in docs/ZAPI-CONSOLE.md. Later, once the protocol is known, the
same mode can ask the controller itself, behind the usual confirmation for anything that changes it.

Switch it on in System > Software > Plugins (handheld), Settings > Built-in plugins (desktop) or
``canopener-cli plugins enable zapi_console``; it's a home tile on the handheld and a tab on the
desktop. ``console.py`` holds the logic both share.
"""

PLUGIN_NAME = "Zapi console"
PLUGIN_VERSION = "0.1"


def shared_console(ctx):
    """One ``Console`` per front-end, kept on its context (the screens come and go). Its settings
    are the shared ``core.settings`` ones (the handheld's ``ctx.settings``; the desktop's own
    QSettings are something else)."""
    con = getattr(ctx, "zapi_console", None)
    if con is None:
        from ...core.settings import Settings
        from .console import Console
        settings = getattr(ctx, "settings", None)
        con = ctx.zapi_console = Console(settings if hasattr(settings, "on_change") else Settings())
    return con


def register_device(api):
    from ...device.modes import Mode
    from . import device
    api.register_mode(Mode("zapi", "Zapi console", device.icon, device.ZapiScreen, device.status, 60))


def register_gui(api):
    from .gui import ZapiConsoleTab
    api.add_tab(ZapiConsoleTab(api), "Zapi console")
