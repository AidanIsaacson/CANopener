"""Headless command-line front-end (handy over SSH on a Pi).

    canopener-cli scan --iface can0 --full
    canopener-cli scan --sim
    canopener-cli decode capture.log
    canopener-cli sim --iface vcan0         # run the simulator on a real/virtual SocketCAN bus
    canopener-cli interfaces
    canopener-cli setup --iface can0 --bitrate auto   # configure + bring up (listen-only probe)
    canopener-cli find --iface can0 --sound           # keep listening while probing; chime on lock
"""

import argparse
import os
import sys
import time

from . import __version__
from .core import connection, interfaces, profiles, report, scan, units


def _apply_profile(pid: str | None):
    if not pid:
        profiles.apply(None)
        return
    p = profiles.find(pid)
    if p is None:
        sys.exit(f"Unknown profile {pid!r}. Available: "
                 + ", ".join(x.id for x in profiles.available_profiles()[0]))
    profiles.apply(p)


def cmd_scan(a) -> int:
    kind = connection.SIMULATOR if a.sim else connection.SOCKETCAN
    try:
        conn = connection.open_connection(kind, a.iface, listen_only=a.listen_only)
    except OSError as e:
        print(f"Could not open {a.iface}: {e}", file=sys.stderr)
        return 2
    try:
        time.sleep(1.2)            # let broadcast traffic (DM1, live data) arrive first
        preset = "full" if a.full else "stored" if a.stored else "emissions" if a.emissions else "active"
        result = scan.run_preset(conn.session, preset)
        print(report.build_report(result, conn.session, full=a.full, system=a.units))
    finally:
        conn.close()
    return 0


def cmd_decode(a) -> int:
    session, n, markers = connection.load_log_session(a.log)
    result = scan.offline_result(session)
    result.source = f"{a.log} ({n} frames)"
    print(report.build_report(result, session, full=True, system=a.units))
    if markers:
        t0 = session.raw[0].timestamp if session.raw else (markers[0][0])
        print(f"\nMarkers from log ({len(markers)}):")
        for t, label in markers:
            print(f"  +{t - t0:7.2f}s  {label}")
    if a.csv:
        from .core.storage import write_csv
        with session.lock:
            frames = list(session.raw)
        print(f"Wrote {write_csv(a.csv, frames)} frames to {a.csv}", file=sys.stderr)
    return 0


def _confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print(f"{question} -- not confirmed (pass --yes to run without asking).", file=sys.stderr)
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def cmd_obd(a) -> int:
    from . import obd as obdlib
    from .core.obd import CLEAR_WARNING, ObdClient
    kind = connection.OBD_SIM if a.sim else connection.SOCKETCAN
    try:
        conn = connection.open_connection(kind, a.iface)
    except OSError as e:
        print(f"Could not open {a.iface}: {e}", file=sys.stderr)
        return 2
    c = ObdClient(conn.session)
    try:
        if not c.available:
            print("OBD-II needs to send requests, so it can't run in listen-only mode.", file=sys.stderr)
            return 2
        time.sleep(0.3)
        if c.detect_addressing() is None:
            print("No OBD-II module answered (key on? 500k bus?).", file=sys.stderr)
            return 1
        if a.clear:
            if not _confirm(CLEAR_WARNING, a.yes):
                return 1
            for ecu, (_, msg) in c.clear_dtcs().items():
                print(f"{c.ecu_label(ecu)}: {msg}")
            return 0
        if a.evap_test:
            if not _confirm("Start the EVAP leak test (Mode 08, TID 01)?", a.yes):
                return 1
            result = c.run_control_test(0x01)
            for ecu, (_, msg) in result.items():
                print(f"{c.ecu_label(ecu)}: {msg}")
            return 0 if any(ok for ok, _ in result.values()) else 1

        info = c.read_info_by_ecu()
        print(f"{len(info) or 'No'} module(s) answered, {c.addressing.name} CAN")
        for ecu, rows in info.items():
            print(f"\n{c.ecu_label(ecu)}")
            for name, value in rows:
                print(f"  {name}: {value}")
        print("\nTrouble codes:")
        any_code = False
        for ecu, kinds in c.read_dtcs_by_ecu().items():
            for label, codes in kinds.items():
                for code in codes:
                    any_code = True
                    print(f"  {c.ecu_label(ecu):18} {label:9} {code}  {obdlib.describe_dtc(code)}")
        if not any_code:
            print("  none")
        print("\nReadiness:")
        for ecu, st in c.read_readiness().items():
            print(f"  {c.ecu_label(ecu)}: MIL {'ON' if st.mil else 'off'}, {st.dtc_count} confirmed code(s)")
            for m in st.monitors:
                if m.supported:
                    print(f"    {m.name:28} {m.state}")
        if a.freeze or a.all:
            print("\nFreeze frame:")
            for ecu, rows in c.read_freeze_frame().items():
                for r in rows:
                    for name, v, u in (r.values or [(r.name, r.text, "")]):
                        print(f"  {c.ecu_label(ecu):18} {name}: {_obd_value(v, u, a.units)}")
        if a.tests or a.all:
            print("\nOn-board test results (Mode 06):")
            for ecu, t in c.read_test_results():
                print(f"  {c.ecu_label(ecu):18} {t.monitor:24} {t.test:34} {t.value:g} {t.unit} "
                      f"[{t.minimum:g}..{t.maximum:g}] {'PASS' if t.passed else 'FAIL'}")
        print("\nLive data:")
        pids = c.live_pids()
        if not pids:
            print("  (no live PIDs answered)")
        end = time.time() + (a.watch or 0)
        while True:
            for ecu, pid in pids:
                r = c.read_pid(ecu, pid)
                for name, v, u in (r.values if r else []):
                    print(f"  {name}: {_obd_value(v, u, a.units)}")
            if time.time() >= end:
                break
            print(f"-- {time.strftime('%H:%M:%S')}", flush=True)
    except KeyboardInterrupt:
        return 130
    finally:
        c.close()
        conn.close()
    return 0


def _obd_value(v, unit: str, system: str) -> str:
    if isinstance(v, (int, float)) and unit:
        return units.fmt(v, unit, 3 if unit == "V" else 1, system=system)
    return f"{v:g}" if isinstance(v, float) else str(v)


def cmd_uds(a) -> int:
    from . import uds as udslib
    from .core.uds import UdsClient
    kind = connection.OBD_SIM if a.sim else connection.SOCKETCAN
    try:
        conn = connection.open_connection(kind, a.iface)
    except OSError as e:
        print(f"Could not open {a.iface}: {e}", file=sys.stderr)
        return 2
    req_id = int(a.module, 16)
    client = UdsClient(conn.session, request_id=req_id)
    try:
        if not client.available:
            print("UDS needs to send requests, so it can't run in listen-only mode.", file=sys.stderr)
            return 2
        print(f"Module 0x{req_id:X} (responses on 0x{client.response_id:X}):")
        try:
            dtcs = client.read_dtcs()
            if dtcs:
                print("  DTCs:")
                for code, status, flags in dtcs:
                    print(f"    {code}  status=0x{status:02X}  {', '.join(flags) or '-'}")
            else:
                print("  DTCs: none")
        except udslib.UdsError as e:
            print(f"  ReadDTCInformation: {e}")
        if a.did is not None:
            did = int(a.did, 16)
            try:
                data = client.read_data_by_identifier(did)
                text = bytes(b for b in data if 0x20 <= b < 0x7F).decode("ascii", "replace")
                print(f"  DID {did:04X}: {data.hex(' ').upper()}" + (f"  ({text})" if text.strip() else ""))
            except udslib.UdsError as e:
                print(f"  DID {did:04X}: {e}")
    finally:
        client.close()
        conn.close()
    return 0


def cmd_allow_setup(a) -> int:
    if interfaces.sudoers_installed():
        print("Already set up: bringing up a CAN interface won't ask for a password.")
        return 0
    ok, msg = interfaces.install_sudoers()
    print(msg, file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 2


def cmd_sim(a) -> int:
    from .can.backends import SocketCanBackend
    from .sim import MachineSimulator
    if a.profile:
        from .sim.profile_sim import ProfileSimulator
        sim = ProfileSimulator(SocketCanBackend(a.iface), profiles.active())
    else:
        sim = MachineSimulator(SocketCanBackend(a.iface))
    try:
        sim.start()
    except OSError as e:
        print(f"Could not open {a.iface}: {e}\nCreate one with:\n  sudo sh -c "
              f"'{interfaces.setup_command('vcan0', 0)}'", file=sys.stderr)
        return 2
    what = f"profile '{profiles.active().name}'" if a.profile else "engine SA 00, controller SA 27"
    print(f"Simulator running on {a.iface} ({what}). Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
    return 0


def cmd_interfaces(a) -> int:
    found = interfaces.list_interfaces()
    if not found:
        print("No CAN interfaces found.\n" + interfaces.no_adapter_hint()
              + "\nOr create a virtual one to test with:\n  sudo sh -c "
              f"'{interfaces.setup_command('vcan0', 0)}'")
    for i in found:
        print(i.describe())
    for a in interfaces.usb_adapters():
        print(f"USB: {a.product} ({a.kind}{', ' + a.tty if a.tty else ''})")
    return 0


def cmd_setup(a) -> int:
    bitrate = a.bitrate
    if bitrate == "auto":
        found, log = interfaces.detect_bitrate(
            a.iface, progress=lambda br: print(f"listening at {br // 1000}k ...", flush=True))
        for line in log:
            print("  " + line)
        if found is None:
            print("Could not detect the bitrate. Key on? Harness connected? Or pass --bitrate.", file=sys.stderr)
            return 1
        bitrate = found
    ok, msg = interfaces.bring_up(a.iface, int(bitrate), a.listen_only)
    if not ok:
        print(f"Setup failed: {msg}", file=sys.stderr)
        return 1
    print(f"{a.iface} up at {int(bitrate) // 1000}k" + (" (listen-only)" if a.listen_only else ""))
    return 0


def cmd_find(a) -> int:
    from .core import link, sound
    order = link.bitrate_order(interfaces.BITRATES[0]) if a.bitrate == "auto" else [int(a.bitrate)]
    if len(order) > 1 and not a.iface.startswith("vcan") and not interfaces.can_configure_quietly(a.iface):
        print("Searching every bitrate needs passwordless CAN access (run 'canopener-cli allow-setup' "
              "once), or pass --bitrate.", file=sys.stderr)
        return 1
    last = [None]

    def show(ev):
        if ev.text != last[0]:
            print(f"{time.strftime('%H:%M:%S')}  {ev.text}", flush=True)
            last[0] = ev.text

    finder = link.SignalFinder(a.iface, order, show, listen_only=a.listen_only)
    print(f"Listening on {a.iface} (listen-only) until steady traffic is heard. Ctrl+C to stop.", flush=True)
    try:
        finder.run()
    except KeyboardInterrupt:
        return 130
    if finder.state != "locked":
        return 1
    if a.sound or a.buzzer:
        sound.play("found", speaker=a.sound, buzzer=a.buzzer or "off")
        time.sleep(1.0)             # let the cue finish before the process exits
    return 0


def cmd_update(a) -> int:
    from .core import updates
    s = updates.check()
    print(s.message)
    if not s.ok:
        return 1
    for c in s.commits:
        print(f"  * {c}")
    if not s.available or a.check:
        return 0
    if s.ahead:
        return 1
    if not _confirm(f"Install the update{' to ' + s.latest if s.latest else ''}?", a.yes):
        return 1
    ok, msg = updates.apply(progress=print)
    print(msg)
    if ok:
        print("(Running as the Pi's service? systemctl --user restart canopener)")
    return 0 if ok else 1


def cmd_capture(a) -> int:
    from .core.capture import Capture
    try:
        c = Capture.load(a.folder)
    except (OSError, ValueError) as e:
        print(f"Not a capture folder: {e}", file=sys.stderr)
        return 2
    if c.state != "saved":
        print("Capture was never saved (recording interrupted); recovering it.", file=sys.stderr)
    if a.trim:
        try:
            kept, dropped = c.trim(a.trim[0] * 60, a.trim[1] * 60, keep_full=a.keep_full)
        except ValueError as e:
            print(f"Can't trim: {e}", file=sys.stderr)
            return 1
        print(f"Trimmed: kept {kept} frames, cut {dropped}.", file=sys.stderr)
    c.save(system=a.units)
    with open(os.path.join(c.folder, "summary.txt")) as f:
        print(f.read())
    print(f"(rewrote summary.txt and live_data.csv in {c.folder})", file=sys.stderr)
    return 0


def cmd_profiles(a) -> int:
    found, errors = profiles.available_profiles()
    for p in found:
        print(f"{p.id:<20} {p.name}{'' if p.builtin else '  (user)'}")
    for e in errors:
        print(f"!! {e}", file=sys.stderr)
    print(f"\nUser profiles live in {profiles.user_dir()}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="canopener-cli", description="CANopener J1939 diagnostics")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--units", choices=list(units.SYSTEMS), default=units.METRIC)
    for q, (_, options) in units.QUANTITIES.items():
        ap.add_argument(f"--{q}", choices=list(options), help=f"{q} unit (implies --units custom)")
    ap.add_argument("--profile", help="machine profile id (see 'profiles')")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="read fault codes from a live bus")
    s.add_argument("--iface", default="can0")
    s.add_argument("--sim", action="store_true", help="scan the built-in simulator instead")
    s.add_argument("--listen-only", action="store_true", help="send nothing (DM1 + live data only)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--stored", action="store_true", help="DM2 stored codes")
    g.add_argument("--emissions", action="store_true", help="DM6/DM12/DM23/DM5")
    g.add_argument("--full", action="store_true", help="everything, plus machine info and bus survey")
    s.set_defaults(fn=cmd_scan)

    d = sub.add_parser("decode", help="decode a candump log file offline")
    d.add_argument("log")
    d.add_argument("--csv", metavar="OUT", help="also write every frame to a spreadsheet-friendly CSV")
    d.set_defaults(fn=cmd_decode)

    m = sub.add_parser("sim", help="run the simulated machine on a SocketCAN interface "
                                   "(with --profile ID before 'sim': simulate that profile instead)")
    m.add_argument("--iface", default="vcan0")
    m.set_defaults(fn=cmd_sim)

    sub.add_parser("interfaces", help="list CAN interfaces").set_defaults(fn=cmd_interfaces)

    u = sub.add_parser("setup", help="configure a CAN interface and bring it up (needs root, sudo or pkexec)")
    u.add_argument("--iface", default="can0")
    u.add_argument("--bitrate", default="250000", help="bits/s, or 'auto' to detect (listen-only probe)")
    u.add_argument("--listen-only", action="store_true", help="leave the controller in listen-only mode")
    u.set_defaults(fn=cmd_setup)

    f = sub.add_parser("find", help="keep listening (listen-only, cycling bitrates) until steady traffic is "
                                    "heard, then leave the interface up at that bitrate. For placing probes")
    f.add_argument("--iface", default="can0")
    f.add_argument("--bitrate", default="auto", help="bits/s, or 'auto' to try each (default)")
    f.add_argument("--listen-only", action="store_true", help="stay in listen-only once found")
    f.add_argument("--sound", action="store_true", help="chime on the speaker when found")
    f.add_argument("--buzzer", metavar="SPEC", help="also on a buzzer: auto, /dev/input/eventN or pwm:CHIP:CH")
    f.set_defaults(fn=cmd_find)
    sub.add_parser("profiles", help="list machine profiles").set_defaults(fn=cmd_profiles)

    o = sub.add_parser("obd", help="OBD-II (J1979) scan tool: codes from every module, readiness, live data, "
                                   "vehicle info; --clear / --evap-test send commands")
    o.add_argument("--iface", default="can0")
    o.add_argument("--sim", action="store_true", help="use the built-in OBD-II simulator instead")
    o.add_argument("--freeze", action="store_true", help="also read the freeze frame")
    o.add_argument("--tests", action="store_true", help="also read on-board test results (Mode 06)")
    o.add_argument("--all", action="store_true", help="freeze frame and test results too")
    o.add_argument("--watch", type=float, metavar="SECONDS", help="keep printing live data this long")
    g = o.add_mutually_exclusive_group()
    g.add_argument("--clear", action="store_true", help="clear codes on every module (Mode 04; asks first)")
    g.add_argument("--evap-test", action="store_true", help="start the EVAP leak test (Mode 08; asks first)")
    o.add_argument("--yes", action="store_true", help="don't ask before --clear / --evap-test")
    o.set_defaults(fn=cmd_obd)

    ud = sub.add_parser("uds", help="read a module via UDS (ISO 14229): DTCs with status, a data identifier")
    ud.add_argument("--iface", default="can0")
    ud.add_argument("--sim", action="store_true", help="read the built-in simulator instead")
    ud.add_argument("--module", default="7E0", help="module request CAN ID in hex (default 7E0)")
    ud.add_argument("--did", help="also read this data identifier (hex, e.g. F190)")
    ud.set_defaults(fn=cmd_uds)

    sub.add_parser("allow-setup", help="install a sudoers rule so bringing up a CAN interface "
                   "needs no password (only 'ip link set canN ...', no stored password)"
                   ).set_defaults(fn=cmd_allow_setup)

    up = sub.add_parser("update", help="check GitHub for a newer CANopener and install it (git pull)")
    up.add_argument("--check", action="store_true", help="only check, don't install")
    up.add_argument("--yes", action="store_true", help="install without asking")
    up.set_defaults(fn=cmd_update)

    c = sub.add_parser("capture", help="re-generate and print a capture folder's summary (optionally trim it)")
    c.add_argument("folder")
    c.add_argument("--trim", nargs=2, type=float, metavar=("BEFORE", "AFTER"),
                   help="first keep only BEFORE minutes before the first timestamp to AFTER minutes after the last")
    c.add_argument("--keep-full", action="store_true", help="with --trim: keep the full log as capture_full.log")
    c.set_defaults(fn=cmd_capture)

    from . import plugins
    for plugin in plugins.loaded():
        hook = plugin.hook("register_cli")
        if hook:
            hook(sub)

    a = ap.parse_args(argv)
    chosen = {q: getattr(a, q) for q in units.QUANTITIES if getattr(a, q)}
    if chosen:
        units.set_custom({**units.prefs(a.units), **chosen})
        a.units = units.CUSTOM
    _apply_profile(a.profile)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
