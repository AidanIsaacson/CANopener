"""What a remote helper can do on the handheld (``core.remote`` carries the commands).

Reads run straight away and are listed on the Remote help page. Anything that could change the
vehicle asks the person holding the unit first (default No). Pygame is only touched for the
screenshot, and only when one is asked for.
"""

import base64
import io
import os
import threading

from ..can import elm327
from ..core import capture as cap
from ..core import config, profiles, remote, scan

ASK_TIMEOUT = 60.0          # seconds the person at the unit has to answer
# Services that only read, on any of the protocols a request can go out on (SAE J1979 modes 01-0A
# except clear / control, and the ISO 14230 / 14229 reads and tester-present).
READ_SERVICES = {0x01, 0x02, 0x03, 0x06, 0x07, 0x09, 0x0A, 0x12, 0x19, 0x1A, 0x21, 0x22, 0x3E}


def _ctx(link):
    return link.context["ctx"]


def _backend(link):
    ctx = _ctx(link)
    if not ctx.live or ctx.session is None:
        raise remote.Refused("the unit isn't connected to a vehicle")
    return ctx.session.backend


def _state(args, link):
    ctx = _ctx(link)
    c = ctx.capture
    battery = ctx.hub.latest("obd.battery")
    return {"connection": ctx.describe_connection(), "live": ctx.live, "protocol": ctx.protocol,
            "profile": ctx.profile_name(), "can_transmit": ctx.can_transmit, "busy": ctx.busy or "",
            "bus": getattr(ctx.bus, "text", ""), "adapter": getattr(ctx.adapter, "text", ""),
            "battery": battery.value if battery else None,
            "log": {"state": c.state, "folder": os.path.basename(c.folder or ""), "frames": c.recorded_frames},
            "toast": ctx.current_toast() or ""}


def _screenshot(args, link):
    import pygame
    app = link.context.get("app")
    if app is None:
        raise remote.Refused("no screen to copy")
    buf = io.BytesIO()
    pygame.image.save(app.screen.copy(), buf, "screen.png")
    return {"png_base64": base64.b64encode(buf.getvalue()).decode(), "size": list(app.screen.get_size())}


def _codes(args, link):
    ctx = _ctx(link)
    if not ctx.live:
        raise remote.Refused("the unit isn't connected to a vehicle")
    if ctx.protocol == profiles.OBD2:
        client = ctx.obd_client
        if client is None or not client.available:
            raise remote.Refused("the unit is listen-only")
        by_ecu = client.read_dtcs_by_ecu()
        return {"obd": {client.ecu_label(e): kinds for e, kinds in by_ecu.items()},
                "thin": [client.ecu_label(e) for e in client.thin_ecus()] if by_ecu else []}
    from ..core import report
    preset = str(args.get("preset") or "active")
    if preset not in scan.PRESETS:
        raise remote.Refused(f"preset must be one of {', '.join(scan.PRESETS)}")
    result = scan.run_preset(ctx.session, preset)
    return {"title": result.title, "codes": result.code_count, "tx_error": result.tx_error,
            "text": report.text_report(result) if hasattr(report, "text_report") else ""}


def _adapter(args, link):
    """Adapter commands (AT / ST): the console's rules apply, nothing reaches the vehicle."""
    b = _backend(link)
    if not isinstance(b, elm327.ElmBackend):
        raise remote.Refused("this connection isn't a Bluetooth OBD adapter")
    out = []
    for cmd in list(args.get("commands") or [])[:40]:
        why = elm327.console_refusal(str(cmd))
        if why:
            out.append({"command": cmd, "refused": why})
            continue
        out.append({"command": cmd, "lines": b.command(str(cmd), 5.0, keep_progress=True)})
    return out


def _request_hex(args) -> list[str]:
    reqs = [str(r).replace(" ", "").upper() for r in (args.get("requests") or [])][:60]
    if not reqs or any(len(r) % 2 or not r or any(c not in "0123456789ABCDEF" for c in r) for r in reqs):
        raise remote.Refused("requests must be hex strings, e.g. ['0100', '22F190']")
    return reqs


def _request_reads_only(args) -> bool:
    try:
        return all(int(r[:2], 16) in READ_SERVICES for r in _request_hex(args))
    except remote.Refused:
        return True                         # refused when it runs, with the reason


def _request(args, link):
    """Requests to the vehicle through the adapter, as typed (hex). Read services go straight out;
    any other service asked the person at the unit first."""
    b = _backend(link)
    if not isinstance(b, elm327.ElmBackend) or b.j1939:
        raise remote.Refused("raw requests need the Bluetooth OBD adapter on a car / K-line connection")
    if not _ctx(link).can_transmit:
        raise remote.Refused("the unit is listen-only")
    out = []
    with b._lock:
        for r in _request_hex(args):
            lines = b.command(r, float(args.get("timeout") or 6.0), keep_progress=True)
            out.append({"request": r, "lines": lines})
    return out


def _describe_request(args) -> str:
    reqs = _request_hex(args)
    other = [r for r in reqs if int(r[:2], 16) not in READ_SERVICES]
    return (f"send {len(reqs)} request(s) to the vehicle, including {', '.join(other[:4])}: "
            "not plain reads, they may change something on it")


def _log(args, link):
    ctx = _ctx(link)
    what = str(args.get("do") or "")
    if what == "start":
        return {"started": ctx.start_log()}
    if what == "stop":
        ctx.stop_log()
    elif what == "tag":
        ctx.tag()
    elif what == "save":
        ctx.save_log()
    elif what == "upload":
        ctx.upload_log()
    else:
        raise remote.Refused("do must be start, stop, tag, save or upload")
    return {"state": ctx.capture.state, "toast": ctx.current_toast() or ""}


def _changes(args, link):
    from ..core import changes
    name = str(args.get("log") or "")
    folder = remote._safe_path(config.logs_dir(), name) if name else (_ctx(link).capture.folder or "")
    if not folder or not os.path.isdir(folder):
        raise remote.Refused("no such saved log")
    c = cap.Capture.load(folder)
    r = changes.analyze_capture(folder, parts=0 if c.tagged() else int(args.get("parts") or 4))
    return r.to_dict() if args.get("full") else {"lines": r.lines(int(args.get("top") or 20))}


def register_all():
    remote.register("state", _state, help="connection, protocol, profile, battery, the log's state")
    remote.register("screenshot", _screenshot, help="the screen as a PNG (base64)")
    remote.register("codes", _codes, help="read fault codes (J1939 args: preset active / stored / full)")
    remote.register("adapter", _adapter, help="Bluetooth OBD adapter commands, AT / ST only (args: commands)")
    remote.register("request", _request, reads_only=_request_reads_only, describe=_describe_request,
                    help="hex requests to the vehicle through the adapter (args: requests, timeout); a service "
                         "that isn't a plain read asks at the unit first")
    remote.register("log", _log, help="the data log (args: do = start / stop / tag / save / upload)")
    remote.register("changes", _changes, help="what changed between a saved log's timestamps (args: log, top, full)")


class Asker:
    """``confirm`` for a RemoteLink: shows the question on the unit and waits for the answer.
    ``show(text, on_yes, on_no)`` must put up a default-No dialog from the UI thread; ``post(fn)``
    runs ``fn`` there. No answer within ASK_TIMEOUT counts as No."""

    def __init__(self, post, show):
        self.post, self.show = post, show

    def __call__(self, text: str) -> bool:
        answered = threading.Event()
        yes = []

        def said_yes():
            yes.append(True)
            answered.set()
        self.post(lambda: self.show(text, said_yes, answered.set))
        answered.wait(ASK_TIMEOUT)
        return bool(yes)
