"""The handheld's top-level functions ("modes"): the tiles on the home screen.

CAN diagnostics is one mode; the meter, the data log and system settings are others, and more
will follow (logic analyzer, scope, ...). A mode is a tile plus a function that builds its first
screen. Plugins add their own from ``register_device(api)`` with ``api.register_mode(...)``.
"""

from dataclasses import dataclass


@dataclass
class Mode:
    id: str
    title: str
    icon: object                # ui icon function: icon(ui, rect, color)
    factory: object             # factory(app) -> Screen
    status: object = None       # status(ctx) -> short text under the tile
    order: int = 50
    visible: object = None      # visible(ctx) -> bool: hidden from the home screen when False


_modes: dict[str, Mode] = {}


def register_mode(mode: Mode):
    _modes[mode.id] = mode


def modes() -> list[Mode]:
    return sorted(_modes.values(), key=lambda m: (m.order, m.title))


ORDER = "device.tile_order"              # the home screen's tiles, as the user arranged them


def shown(ctx) -> list[Mode]:
    """The modes on the home screen right now (Games stays hidden until unlocked), in the order
    the user dragged them into; tiles they haven't placed (new ones) follow in the usual order."""
    ms = [m for m in modes() if m.visible is None or m.visible(ctx)]
    saved = list((ctx.settings.get(ORDER) if ctx is not None else None) or [])
    place = {mode_id: i for i, mode_id in enumerate(saved)}
    return sorted(ms, key=lambda m: (0, place[m.id]) if m.id in place else (1, 0))


def save_order(ctx, ids: list[str]):
    """Keep the tiles in this order. Ids of modes that aren't shown now keep their place after
    them, so hiding Games (or a plugin) and showing it again doesn't lose where it was."""
    old = [i for i in (ctx.settings.get(ORDER) or []) if i not in ids]
    ctx.settings.set(ORDER, list(ids) + old)


def get(mode_id: str) -> Mode | None:
    return _modes.get(mode_id)


def register_builtin():
    from . import ui
    from .screens import can, controls, games, guide, log, meter, settings
    register_mode(Mode("can", "CAN", ui.icon_can, can.CanScreen, can.status, 10))
    register_mode(Mode("meter", "Meter", ui.icon_meter, meter.MeterScreen, meter.status, 20))
    register_mode(Mode("log", "Data log", ui.icon_log, log.LogScreen, log.status, 30))
    register_mode(Mode("settings", "System", ui.icon_settings, settings.SettingsScreen, settings.status, 90))
    # after System, so the four soft keys stay CAN / Meter / Data log / System
    register_mode(Mode("builder", "Profile builder", ui.icon_builder, guide.BuilderHome, guide.status, 92))
    register_mode(Mode("controls", "Control mapping", ui.icon_controls, controls.ControlMapScreen, controls.status,
                       93))
    register_mode(Mode("games", "Games", ui.icon_games, games.GamesScreen, games.status, 95,
                       visible=lambda ctx: bool(ctx.settings.get("device.games"))))


class DeviceApi:
    """What a plugin's ``register_device(api)`` gets."""

    def __init__(self, app):
        self.app = app
        self.ctx = app.ctx

    def register_mode(self, mode: Mode):
        register_mode(mode)

    def toast(self, text: str):
        self.ctx.toast(text)
