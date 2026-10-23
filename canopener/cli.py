"""Headless command-line front-end (handy over SSH on a Pi).

    canopener-cli scan --iface can0 --full
    canopener-cli scan --sim
    canopener-cli decode capture.log
    canopener-cli sim --iface vcan0         # run the simulator on a real/virtual SocketCAN bus
    canopener-cli interfaces
    canopener-cli setup --iface can0 --bitrate auto   # configure + bring up (listen-only probe)
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


def cmd_obd(a) -> int:
    from .core.obd import ObdClient
    kind = connection.OBD_SIM if a.sim else connection.SOCKETCAN
    try:
        conn = connection.open_connection(kind, a.iface)
    except OSError as e:
        print(f"Could not open {a.iface}: {e}", file=sys.stderr)
        return 2
    client = ObdClient(conn.session)
    try:
        if not client.available:
            print("OBD-II needs to send requests, so it can't run in listen-only mode.", file=sys.stderr)
            return 2
        time.sleep(0.3)
        from . import obd as obdlib
        info = client.read_info()
        if info:
            for name, value in info:
                print(f"{name}: {value}")
        else:
            print("VIN: (not reported)")
        dtcs = client.read_dtcs()
        for label in ("stored", "pending", "permanent"):
            codes = dtcs.get(label) or []
            if codes:
                print(f"{label.capitalize()} DTCs:")
                for c in codes:
                    print(f"  {c}  {obdlib.describe_dtc(c)}")
            else:
                print(f"{label.capitalize()} DTCs: none")
        print("\nLive data:")
        live = client.read_live()
        if not live:
            print("  (no live PIDs answered)")
        for r in live:
            value = units.fmt(r.value, r.unit, system=a.units) if r.unit else str(r.value)
            print(f"  {r.name}: {value}")
    finally:
        client.close()
        conn.close()
    return 0


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


def cmd_capture(a) -> int:
    from .core.capture import Capture
    try:
        c = Capture.load(a.folder)
    except (OSError, ValueError) as e:
        print(f"Not a capture folder: {e}", file=sys.stderr)
        return 2
    if c.state != "saved":
        print("Capture was never saved (recording interrupted); recovering it.", file=sys.stderr)
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
    sub.add_parser("profiles", help="list machine profiles").set_defaults(fn=cmd_profiles)

    o = sub.add_parser("obd", help="read OBD-II (automotive J1979): DTCs, live PIDs and VIN")
    o.add_argument("--iface", default="can0")
    o.add_argument("--sim", action="store_true", help="read the built-in OBD-II simulator instead")
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

    c = sub.add_parser("capture", help="re-generate and print a capture folder's summary")
    c.add_argument("folder")
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
