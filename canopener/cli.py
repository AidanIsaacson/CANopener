"""Headless command-line front-end (handy over SSH on a Pi).

    canopener-cli scan --iface can0 --full
    canopener-cli scan --sim
    canopener-cli decode capture.log
    canopener-cli clear --iface can0 --stored        # clear J1939 codes (DM11 active, --stored DM3; asks first)
    canopener-cli sim --iface vcan0         # run the simulator on a real/virtual SocketCAN bus
    canopener-cli interfaces --python-can          # Linux interfaces, plus adapters python-can finds
    canopener-cli scan --iface pcan:PCAN_USBBUS1 --bitrate 250000   # any python-can adapter
    canopener-cli setup --iface can0 --bitrate auto   # configure + bring up (listen-only probe)
    canopener-cli find --iface can0 --sound           # keep listening while probing; chime on lock
    canopener-cli hardware --stage 2b --set can_b=auto  # installed options: what's fitted, what's found
    canopener-cli changes CAPTURE_FOLDER              # what changed between a capture's timestamps (no decoder)
    canopener-cli suggest CAPTURE                     # undecoded traffic that other profiles on this unit decode
    canopener-cli bluetooth scan                      # Bluetooth OBD adapters nearby (then: pair, rename, test)
    canopener-cli obd --elm                           # OBD-II through the picked Bluetooth OBD adapter
    canopener-cli bluetooth kline --out probe.txt     # older K-line vehicle: what answers besides generic OBD-II
    canopener-cli obd --elm sim --bench 10            # how fast an adapter answers (here: the simulated one)
    canopener-cli serial sniff --line console=/dev/ttyUSB0 --line controller=/dev/ttyUSB1 --framing "9600 8N1"
    canopener-cli serial analyze CAPTURE_FOLDER       # what the serial bytes look like (kinds, checksums, text)
    canopener-cli --profile zapi-combiacx modules --iface can0 --watch   # which modules are missing (listens only)
    canopener-cli team join https://logs.example.com/invite/XXXX-XXXX-XXXX   # team access (the team plugin)
    canopener-cli team-admin invite "Dave's laptop"   # the owner's side: invite, approve, revoke, publish
"""

import argparse
import os
import sys
import time

from . import __version__
from .core import config, connection, interfaces, profiles, report, scan, units


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
    kind, channel = (connection.SIMULATOR if a.sim else connection.SOCKETCAN), a.iface
    if a.elm is not None:                   # J1939 through a Bluetooth OBD adapter (protocol A)
        channel = _elm_channel(a.elm)
        kind = connection.ELM_SIM if channel == "sim" or channel.startswith("sim:") else connection.ELM
        channel = "sim:j1939" if channel == "sim" else channel
    try:
        conn = connection.open_connection(kind, channel, listen_only=a.listen_only, bitrate=a.bitrate,
                                          elm_protocol="A")
    except OSError as e:
        print(f"Could not open {channel}: {e}", file=sys.stderr)
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


def cmd_clear(a) -> int:
    from .core import j1939_clear as jc
    which = jc.BOTH if a.both else jc.STORED if a.stored else jc.ACTIVE
    try:
        da = int(a.module, 16) if a.module else jc.GLOBAL_SA
    except ValueError:
        print(f"--module is a source address in hex, e.g. 00 or 0x27 (got {a.module!r})", file=sys.stderr)
        return 2
    kind = connection.SIMULATOR if a.sim else connection.SOCKETCAN
    try:
        conn = connection.open_connection(kind, a.iface, bitrate=a.bitrate)
    except OSError as e:
        print(f"Could not open {a.iface}: {e}", file=sys.stderr)
        return 2
    try:
        if conn.session.listen_only:
            print("Clearing codes has to transmit; this adapter is listen-only.", file=sys.stderr)
            return 2
        if not _confirm(jc.warning(which, da), a.yes):
            return 1
        result = jc.clear_codes(conn.session, which, da)
        for line in result.lines():
            print(line)
        time.sleep(1.2)                 # let the next DM1 broadcast arrive
        for preset in (["active"] if which == jc.ACTIVE else ["stored"] if which == jc.STORED
                       else ["active", "stored"]):
            print()
            print("\n".join(report.code_table(scan.run_preset(conn.session, preset))))
        return 0 if result.ok else 1
    finally:
        conn.close()


def _elm_channel(spec: str) -> str:
    """--elm's value: 'sim', an address / tcp: / /dev/ path, a name you gave an adapter, or ''
    for the adapter picked in Settings (or with 'bluetooth use')."""
    from .can import elm327
    from .core import bluetooth
    from .core.settings import Settings
    if spec == "sim" or spec.startswith("sim:"):
        return spec
    settings = Settings()
    if not spec:
        spec = settings.get("elm.adapter") or ""
        if not spec:
            sys.exit("No Bluetooth OBD adapter picked. Find and pair one: canopener-cli bluetooth scan, "
                     "then bluetooth pair ADDRESS (or pass --elm AA:BB:CC:DD:EE:FF).")
    try:
        elm327.parse_channel(spec)
        return spec
    except ValueError:
        pass
    for addr, name in bluetooth.names(settings).items():
        if name.lower() == spec.lower():
            return addr
    sys.exit(f"--elm: {spec!r} is not an adapter name, a Bluetooth address, tcp:HOST:PORT or a /dev/ path.")


def _open_obd(a):
    """The connection for obd / uds: --elm (a Bluetooth OBD adapter), --sim, or --iface."""
    if a.elm is not None:
        channel = _elm_channel(a.elm)
        kind = connection.ELM_SIM if channel == "sim" or channel.startswith("sim:") else connection.ELM
    else:
        channel, kind = a.iface, connection.OBD_SIM if a.sim else connection.SOCKETCAN
    try:
        return connection.open_connection(kind, channel, bitrate=a.bitrate, elm_protocol=a.elm_protocol,
                                          elm_bus=a.elm_bus if kind == connection.ELM else "")
    except OSError as e:
        print(f"Could not open {channel}: {e}", file=sys.stderr)
        return None


def cmd_obd(a) -> int:
    from . import obd as obdlib
    from .core.obd import CLEAR_WARNING, ObdClient
    conn = _open_obd(a)
    if conn is None:
        return 2
    backend = conn.session.backend
    if getattr(backend, "kind", "") == "elm327":
        print(f"{conn.describe()}" + (f", battery {backend.voltage}" if backend.voltage else ""),
              file=sys.stderr)
    c = ObdClient(conn.session)
    try:
        if not c.available:
            print("OBD-II needs to send requests, so it can't run in listen-only mode.", file=sys.stderr)
            return 2
        time.sleep(0.3)
        if c.detect_addressing() is None:
            print("No OBD-II module answered (key on? 500k bus?)."
                  + (f" Adapter: {conn.session.health.tx_error}" if conn.session.health.tx_error else ""),
                  file=sys.stderr)
            return 1
        if a.bench:
            return _obd_bench(c, conn, a.bench)
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
            if c.thin_ecus():
                from .core.obd import THIN_NOTE
                print(f"\n  Note: {THIN_NOTE}")
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


def _obd_bench(c, conn, seconds: float) -> int:
    """How fast does this connection answer? Single-PID live-data requests to the first module
    for ``seconds``, then a few 'every module' requests: round-trip times and rates."""
    pids = c.live_pids()
    if not pids:
        print("No live PIDs answered: nothing to time.", file=sys.stderr)
        return 1
    ecu = pids[0][0]
    todo = [p for e, p in pids if e == ecu][:12]
    print(f"Timing {conn.describe()} for {seconds:g} s: {len(todo)} live value(s) from {c.ecu_label(ecu)}, "
          "one per request...", flush=True)
    times, misses = [], 0
    end = time.time() + seconds
    while time.time() < end:
        for pid in todo:
            t0 = time.perf_counter()
            ok = c.read_pid(ecu, pid) is not None
            dt = time.perf_counter() - t0
            if ok:
                times.append(dt)
            else:
                misses += 1
            if time.time() >= end:
                break
    if not times:
        print("No answers.", file=sys.stderr)
        return 1
    times.sort()
    total = sum(times)

    def ms(x):
        return f"{x * 1000:.0f} ms"

    print(f"  answers: {len(times)} ({misses} missed), {len(times) / seconds:.1f} values/s")
    print(f"  round trip: fastest {ms(times[0])}, typical {ms(times[len(times) // 2])}, "
          f"95% under {ms(times[int(len(times) * 0.95) - 1 if len(times) > 1 else 0])}, slowest {ms(times[-1])}")
    print(f"  each value refreshes: watching 1 -> {1 / (total / len(times)):.0f}/s, "
          f"6 -> {1 / (6 * total / len(times)):.1f}/s, 12 -> {1 / (12 * total / len(times)):.1f}/s")
    funcs = []
    for _ in range(3):
        t0 = time.perf_counter()
        c.query_all(0x01, 0x00)
        funcs.append(time.perf_counter() - t0)
    print(f"  'every module' request (codes, readiness): typical {ms(sorted(funcs)[1])}")
    return 0


def _obd_value(v, unit: str, system: str) -> str:
    if isinstance(v, (int, float)) and unit:
        return units.fmt(v, unit, 3 if unit == "V" else 1, system=system)
    return f"{v:g}" if isinstance(v, float) else str(v)


def cmd_uds(a) -> int:
    from . import uds as udslib
    from .core.uds import UdsClient
    conn = _open_obd(a)
    if conn is None:
        return 2
    if a.find:
        from .core.uds import find_modules
        try:
            print("Asking each diagnostic ID 700-7EF whether a module is there (UDS TesterPresent, changes "
                  "nothing). A minute or two...", file=sys.stderr, flush=True)
            found = find_modules(conn.session, progress=lambda rid: print(f"\r  {rid:03X}", end="", file=sys.stderr,
                                                                         flush=True))
            print("", file=sys.stderr)
            for rid, reply in found:
                print(f"  0x{rid:03X} (answers on 0x{rid + 8:03X}): {reply.hex(' ').upper()}")
            if not found:
                print("  no module answered")
            return 0
        finally:
            conn.close()
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
                    kind = udslib.failure_type(int(code[-2:], 16))
                    print(f"    {code}  status=0x{status:02X}  {', '.join(flags) or '-'}"
                          + (f"  [{kind}]" if kind and kind != "no sub-type" else ""))
            else:
                print("  DTCs: none")
        except udslib.UdsError as e:
            print(f"  ReadDTCInformation: {e}")
        if a.info:
            ident = client.read_identification()
            print("  Identification:" if ident else "  Identification: nothing answered")
            for did, name, value in ident:
                print(f"    {did:04X} {name + ':':<38} {value}")
        if a.supported:
            try:
                known = client.read_supported_dtcs()
                print(f"  Codes this module can set: {len(known)}")
                for code, status, _flags in known:
                    print(f"    {code}  status=0x{status:02X}")
            except udslib.UdsError as e:
                print(f"  Supported codes: {e}")
        if a.did is not None:
            did = int(a.did, 16)
            label = f"DID {did:04X}" + (f" ({udslib.did_name(did)})" if udslib.did_name(did) else "")
            try:
                data = client.read_data_by_identifier(did)
                text = bytes(b for b in data if 0x20 <= b < 0x7F).decode("ascii", "replace")
                print(f"  {label}: {data.hex(' ').upper()}" + (f"  ({text})" if text.strip() else ""))
            except udslib.UdsError as e:
                print(f"  {label}: {e}")
    finally:
        client.close()
        conn.close()
    return 0


def _save_imported(importers, doc: dict, notes: list[str], file_name: str | None, force: bool) -> int:
    for n in notes:
        print(f"  note: {n}", file=sys.stderr)
    if not doc.get("signals"):
        print("Nothing to save.", file=sys.stderr)
        return 1
    try:
        path = importers.save(doc, file_name, force)
    except FileExistsError as e:
        print(f"{e} already exists: pick another --name, or pass --force to replace it.", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Couldn't make a profile from it: {e}", file=sys.stderr)
        return 1
    print(f"Saved profile \"{doc['name']}\" with {len(doc['signals'])} value(s): {path}")
    print(f"Use it: pick it in the app's profile list, or  canopener-cli --profile {os.path.basename(path)[:-5]} ...")
    return 0


def cmd_import(a) -> int:
    """A DBC, EDS or DCF file -> a profile in the user's profiles folder."""
    from .canopen import eds as edslib
    from .core import importers
    ext = os.path.splitext(a.file)[1].lower()
    name = a.name or os.path.splitext(os.path.basename(a.file))[0]
    try:
        if ext == ".dbc":
            with open(a.file, encoding="latin-1") as f:
                doc, notes = importers.dbc_profile(f.read(), name, a.protocol)
        elif ext in (".eds", ".dcf"):
            eds = edslib.load(a.file)
            node = int(a.node, 16) if a.node else eds.node_id
            if not node:
                print("Which node is the device? Pass --node (hex), e.g. --node 05.", file=sys.stderr)
                return 2
            doc, notes = importers.eds_profile(eds, node, a.name)
        else:
            print("Import reads .dbc (signal databases) and .eds / .dcf (CANopen device descriptions).",
                  file=sys.stderr)
            return 2
    except OSError as e:
        print(f"Couldn't read {a.file}: {e}", file=sys.stderr)
        return 2
    if a.show:
        import json
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        for n in notes:
            print(f"  note: {n}", file=sys.stderr)
        return 0
    return _save_imported(importers, doc, notes, a.name, a.force)


def _print_roster(r) -> None:
    from .core import roster
    print(r.summary())
    for m in r.modules:
        mark = {roster.ON: "  ", roster.WAITING: "..", roster.QUIET: "!!", roster.MISSING: "!!"}[m.status]
        heard = f"  ({m.frames} frames, last {m.age:.1f} s ago)" if m.frames else ""
        print(f" {mark} {m.name:<36} {m.text}{heard}")
    if r.hint("--remember"):
        print(r.hint("--remember"))


def cmd_modules(a) -> int:
    """Which modules are on the bus, and which ones the profile says the machine has but aren't.
    Only listens: nothing is sent."""
    from .core import canopen as co
    from .core import roster
    if a.log:
        session = connection.load_log_session(a.log)[0]
        r = roster.check(session)
        _print_roster(r)
        return 1 if r.problems else 0
    kind = connection.SIMULATOR if a.sim else connection.CANOPEN_SIM if a.canopen_sim else connection.SOCKETCAN
    try:
        conn = connection.open_connection(kind, a.iface, listen_only=True, bitrate=a.bitrate)
    except OSError as e:
        print(f"Could not open {a.iface}: {e}", file=sys.stderr)
        return 2
    s = conn.session
    try:
        time.sleep(max(a.listen, roster.SETTLE + 0.5))
        r = roster.check(s)
        if a.remember:
            pid, found = roster.remember(s, profiles.active(), a.remember if a.remember != "-" else "")
            print(f"Saved {len(found)} module(s) as this machine's in profile {pid!r}: "
                  + ", ".join(co.source_name(x) for x in found))
            return 0
        _print_roster(r)
        if not a.watch:
            return 1 if r.problems else 0
        alerts = roster.Alerts()
        alerts.update(r)
        print("Watching (Ctrl+C to stop)...")
        while True:
            time.sleep(1.0)
            for line in alerts.update(roster.check(s))[0]:
                print(f"{time.strftime('%H:%M:%S')}  {line}", flush=True)
    except KeyboardInterrupt:
        return 0
    finally:
        conn.close()


def cmd_canopen(a) -> int:
    from .canopen import nmt as nmtlib
    from .core import canopen as co
    kind = connection.CANOPEN_SIM if a.sim else connection.SOCKETCAN
    try:
        conn = connection.open_connection(kind, a.iface, listen_only=a.listen_only, bitrate=a.bitrate)
    except OSError as e:
        print(f"Could not open {a.iface}: {e}", file=sys.stderr)
        return 2
    s = conn.session
    client = co.CanopenClient(s)
    commands = {"start": nmtlib.START, "stop": nmtlib.STOP, "preop": nmtlib.ENTER_PREOP,
                "reset": nmtlib.RESET_NODE, "reset-comm": nmtlib.RESET_COMM}
    try:
        time.sleep(a.listen)
        if a.nmt or a.clear_errors:
            if s.listen_only:
                print("Commands have to transmit: not in listen-only.", file=sys.stderr)
                return 2
            node = int(a.node, 16) if a.node else nmtlib.ALL_NODES
            if a.nmt:
                if not _confirm(co.warning_nmt(commands[a.nmt], node).replace("\n\n", "\n"), a.yes):
                    return 1
                client.nmt(commands[a.nmt], node)
                print(f"Sent NMT {nmtlib.COMMANDS[commands[a.nmt]][0]}.")
                time.sleep(1.0)
            if a.clear_errors:
                if not node:
                    print("--clear-errors needs --node.", file=sys.stderr)
                    return 2
                if not _confirm(co.warning_clear(node).replace("\n\n", "\n"), a.yes):
                    return 1
                client.clear_error_history(node)
                print(f"Cleared node {node:02X}'s error history.")
        if a.find and not s.listen_only:
            found = client.find_nodes()
            print("Nodes answering: " + (", ".join(f"{n:02X}" for n in found) or "none"))
        nodes = s.canopen_nodes()
        if not nodes:
            print("No CANopen nodes heard (wrong bitrate? try --find, or 125000 / 500000).")
            return 0
        targets = [int(a.node, 16)] if a.node else [n.node for n in nodes]
        if not s.listen_only and (a.info or a.errors):
            for n in targets:
                try:
                    if a.info:
                        client.node_info(n)
                    if a.errors:
                        client.error_history(n)
                except co.SdoError as e:
                    print(f"  node {n:02X}: {e}")
        print("CANopen nodes:")
        for info in s.canopen_nodes():
            print(f"  {co.source_name(co.node_src(info.node))}: {co.node_summary(info)}")
            if a.info:
                for label, text in co.node_details(info)[1:]:
                    print(f"      {label:<34} {text}")
        if a.pdo_map or a.save_profile:
            if s.listen_only:
                print("Reading the PDO mapping has to ask the nodes: not in listen-only.", file=sys.stderr)
                return 2
            from .core import importers
            maps = {}
            for n in targets:
                try:
                    maps[n] = client.read_tpdos(n)
                except co.SdoError as e:
                    print(f"  node {n:02X}: {e}")
            print("Process data (what each node says is in its PDOs):")
            for n, pdos in sorted(maps.items()):
                if not pdos:
                    print(f"  node {n:02X}: no readable PDO mapping")
                for p in pdos:
                    where = f"ID {p.can_id:03X}" if p.can_id is not None else "switched off"
                    what = ", ".join("padding" if m.is_padding else f"{m.index:04X}:{m.sub:02X} ({m.bits} bit)"
                                     for m in p.mapped)
                    print(f"  node {n:02X} {p.label} ({where}): {what or 'nothing mapped'}")
            if a.save_profile:
                names = {i.node: i.name for i in s.canopen_nodes() if getattr(i, "name", "")}
                doc, notes = importers.pdo_profile(maps, a.save_profile, node_names=names)
                rc = _save_imported(importers, doc, notes, a.save_profile, a.force)
                if rc:
                    return rc
        codes = co.codes(s, history=True)
        print("Errors:" if codes else "Errors: none reported")
        for c in codes:
            reg = f"  register {c.register_text}" if c.register is not None else ""
            print(f"  {c.status:<8} {co.source_name(c.src):<34} EMCY {c.code:04X}  {c.text}{reg}")
        with s.lock:
            events = list(s.canopen.events)
        if events and a.events:
            print("Events:")
            for t, text in events:
                print(f"  {time.strftime('%H:%M:%S', time.localtime(t))}  {text}")
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
    for u in interfaces.usb_adapters():
        print(f"USB: {u.product} ({u.kind}{', ' + u.tty if u.tty else ''})")
    if a.python_can:
        from .can import pythoncan
        if not pythoncan.available():
            print(pythoncan.INSTALL_HINT)
            return 1
        found_pc = pythoncan.detect()
        print("python-can adapters (use as --iface): " + (", ".join(found_pc) or "none found"))
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


def cmd_plugins(a) -> int:
    """Sign in to GitHub and install / update plugins from repositories (core.plugin_repos)."""
    from .core import github, plugin_repos
    from .core.settings import Settings
    st = Settings()
    from . import plugins
    if a.action in ("enable", "disable"):
        try:
            plugins.set_bundled(st, a.repo or "", a.action == "enable")
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        print(f"{a.repo} {'on' if a.action == 'enable' else 'off'}: it {'loads' if a.action == 'enable' else 'goes'} "
              "the next time CANopener starts.")
        return 0
    acct = github.load_account()
    token = acct.token if acct else None
    try:
        if a.action == "login":
            cid = github.client_id(st)
            dc = github.start_sign_in(cid)
            print(f"Open {dc.verification_uri} and enter the code:  {dc.user_code}")
            token = github.wait_for_token(cid, dc)
            login = github.whoami(token)
            github.save_account(github.Account(token, login))
            print(f"Signed in as {login}.")
            return 0
        if a.action == "logout":
            print("Signed out." if github.sign_out() else "Wasn't signed in.")
            return 0
        if a.action == "repos":
            if not token:
                print("Not signed in: canopener-cli plugins login", file=sys.stderr)
                return 1
            for r in github.repositories(token):
                print(r)
            return 0
        if a.action == "add":
            if not a.repo:
                print("Which repository? canopener-cli plugins add owner/name", file=sys.stderr)
                return 2
            src = plugin_repos.add_source(st, a.repo, a.dir or "")
            ok, msg = plugin_repos.install(src, token, progress=print)
            print(msg)
            return 0 if ok else 1
        if a.action == "remove":
            plugin_repos.remove_source(st, a.repo or "")
            print("Removed from the list (its folder and package were left in place).")
            return 0
        srcs = plugin_repos.sources(st)
        on = plugins.bundled_enabled(st)
        for b in plugins.BUNDLED:
            print(f"Built in: {b.id} ({b.name}): {'on' if b.id in on else 'off'}, {b.about}")
        print(f"GitHub: signed in as {acct.login}" if acct else "GitHub: not signed in")
        if not srcs:
            print("No plugin repositories yet: canopener-cli plugins add owner/name")
        rc = 0
        for src in srcs:
            if a.action == "update":
                ok, msg = plugin_repos.update(src, token, progress=print)
                rc |= 0 if ok else 1
            else:
                s = plugin_repos.check(src, token)
                msg = s.message + "".join(f"\n    * {c}" for c in s.commits)
            print(f"{src.repo}  ({src.path})\n  {msg}")
        return rc
    except (github.GitHubError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1


def _ago(t) -> str:
    if not t:
        return "never"
    mins = int((time.time() - float(t)) / 60)
    return f"{mins} min ago" if mins < 120 else f"{mins // 60} h ago" if mins < 2880 else f"{mins // 1440} days ago"


def cmd_team(a) -> int:
    """Team access, this computer's side (core.team): join with an invite, ask, check, leave."""
    from .core import install, team
    try:
        if a.action == "join":
            if not a.arg:
                print("Paste the invite link: canopener-cli team join https://...", file=sys.stderr)
                return 2
            st = team.join(a.arg, a.name or team.default_name(), progress=print)
        elif a.action == "request":
            if not a.arg:
                print("Which server? canopener-cli team request https://...", file=sys.stderr)
                return 2
            st = team.request(a.arg, a.name or team.default_name())
        elif a.action == "check":
            st = team.check(progress=print, install_updates=a.yes or None)
            if st.update and _confirm(f"Install team plugin {st.update}?", a.yes):
                st = team.install_update(progress=print)
        elif a.action == "leave":
            if not _confirm("Leave team access? The team plugin is removed from this computer.", a.yes):
                return 1
            st = team.leave()
        else:
            st = team.describe()
    except team.TeamError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"This computer: {install.serial()}" + (f" ({st.name})" if st.name else ""))
    if st.server:
        print(f"Server: {st.server}  (last checked {_ago(team.load_state().get('checked'))})")
    print(st.message)
    return 0 if st.ok else 1


def cmd_team_admin(a) -> int:
    """Team access, the owner's side: needs the server's admin token."""
    import tempfile

    from .core import team, upload
    from .core.settings import Settings
    url = a.url or Settings().get("upload.url") or team.load_state().get("server") or upload.default_server()[0]
    token = team.admin_token()
    if not url or not token:
        print("Needs the server's address (--url, or this computer's upload server) and an admin token "
              f"(CANOPENER_ADMIN_TOKEN, or the file {os.path.join(config.config_dir(), 'admin-token')}).",
              file=sys.stderr)
        return 2
    adm = team.Admin(url, token)
    try:
        if a.action == "list":
            rows = adm.installs()
            pack = adm.pack()
            print(f"Team plugin on the server: {pack.get('version')} ({pack.get('size', 0) // 1024} kB, published "
                  f"{_ago(pack.get('published'))})" if pack else "No team plugin published yet (team-admin publish).")
            for r in rows:
                seen = _ago(r.get("last_seen")) if r.get("last_seen") else "not yet"
                print(f"{r['serial']}  {r['status']:<8}  {r.get('name') or '(no name)'}  "
                      f"has {r.get('pack_version') or 'nothing'}  last check {seen}")
            if not rows:
                print("No installs yet: team-admin invite NAME")
            return 0
        if a.action == "invite":
            inv = adm.invite(a.who or "", a.days)
            print(f"Send this link to {a.who or 'them'} (works once, until "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(inv['expires']))}):\n\n  {inv['link']}\n\n"
                  "They paste it into CANopener: Help > Team access > Invite link > Join.")
            return 0
        if a.action == "invites":
            for i in adm.invites():
                state = "used" if i.get("used_by") else "expired" if i["expires"] < time.time() else "open"
                print(f"{i['id']}  {state:<7}  {i.get('name') or '(no name)'}  "
                      f"expires {time.strftime('%Y-%m-%d', time.localtime(i['expires']))}")
            return 0
        if a.action == "uninvite":
            adm.uninvite(a.who or "")
            print("Invite cancelled.")
            return 0
        if a.action == "pack":
            print(adm.pack() or "No team plugin published yet.")
            return 0
        if a.action == "publish":
            if not a.who:
                print("What to publish? team-admin publish PATH (the plugin's folder or a zip)", file=sys.stderr)
                return 2
            with tempfile.TemporaryDirectory() as tmp:
                path, version, note = team.make_bundle(a.who, tmp, a.version or "")
                if note:
                    print(note)
                got = adm.publish(path, version)
            print(f"Published team plugin {got['version']} ({got['size'] // 1024} kB). Approved computers get it "
                  "at their next check (they're asked first).")
            return 0
        if not a.who:
            print(f"Which install? team-admin {a.action} CO-XXXX-XXXX", file=sys.stderr)
            return 2
        if a.action == "approve":
            r = adm.set(a.who, status="approved")
        elif a.action == "revoke":
            r = adm.set(a.who, status="revoked")
        elif a.action == "rename":
            r = adm.set(a.who, name=a.name or "")
        elif a.action == "forget":
            if not _confirm(f"Forget {a.who}? It loses the team plugin at its next check and needs a new invite.",
                            a.yes):
                return 1
            adm.forget(a.who)
            print("Forgotten.")
            return 0
        else:
            return 2
        print(f"{r['serial']}  {r['status']}  {r.get('name') or ''}")
        return 0
    except team.TeamError as e:
        print(e, file=sys.stderr)
        return 1


def cmd_capture(a) -> int:
    from .core.capture import Capture
    try:
        c = Capture.load(a.folder)
    except (OSError, ValueError) as e:
        print(f"Not a capture folder: {e}", file=sys.stderr)
        return 2
    if c.state != "saved":
        print("Capture was never saved (recording interrupted); recovering it.", file=sys.stderr)
    if a.machine or a.question is not None or a.concern is not None or a.name is not None:
        from .core import capture as cap
        fields = dict(c.machine)
        for item in a.machine or []:
            key, _, value = item.partition("=")
            if key not in cap.MACHINE_KEYS:
                print(f"Unknown machine field {key!r}: use {', '.join(cap.MACHINE_KEYS)}", file=sys.stderr)
                return 2
            fields[key] = value
        c.set_machine(fields, name=a.name, notes=a.concern, question=a.question)
        cap.remember_machine(c.machine)
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


def _size(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, round(n / 1024))} kB"


def _progress(sent: int, total: int):
    """One line that redraws in place on a terminal; nothing when the output is piped or logged."""
    if sys.stderr.isatty():
        print(f"\r\x1b[K  {_size(sent)} of {_size(total)}", end="", file=sys.stderr, flush=True)


def cmd_upload(a) -> int:
    import getpass

    from .core import upload
    from .core.settings import Settings
    s = Settings()
    if a.setup:
        url = input(f"Upload server address [{s.get('upload.url') or 'https://...'}]: ").strip() or s.get("upload.url")
        token = getpass.getpass("Upload token (Enter keeps the saved one): ").strip()
        s.update({"upload.url": upload.normalize_url(url), "upload.target": upload.HttpUploader.id})
        if token:
            upload.save_token(token)
        a.test = True
    if a.github:
        s.update({"upload.target": "github", "upload.github_repo": a.github.strip()})
        a.test = True
    if a.on_save is not None:
        s.set("upload.on_save", a.on_save == "on")
        print(f"Upload when saved: {a.on_save}")
    u = upload.target(s)
    if a.test:
        if u is None:
            print("No upload server set up: canopener-cli upload --setup", file=sys.stderr)
            return 1
        try:
            print(u.check(s))
        except upload.UploadError as e:
            print(e, file=sys.stderr)
            return 1
        return 0
    q = upload.UploadQueue(s)
    if a.status or not (a.folders or a.pending):
        where = u.describe(s) if u else "nowhere yet (canopener-cli upload --setup)"
        print(f"Uploads go to: {where}; upload when saved: {'on' if s.get('upload.on_save') else 'off'}")
        for folder in a.folders or q.folders():
            st = q.status(folder)
            print(f"  {os.path.basename(folder)}: {st.text or 'not uploaded'}" + (f"  {st.url}" if st.url else ""))
        return 0
    rc = 0
    for folder in a.folders:
        try:
            q.enqueue(folder)
        except upload.UploadError as e:
            print(f"{folder}: {e}", file=sys.stderr)
            rc = 1
    q.retry_now()
    q.on_change = lambda f: _progress(*q.progress[f]) if q.progress.get(f, (0, 0))[1] else None
    while True:
        folder = q.run_once(force=True)
        if folder is None:
            break
        st = q.status(folder)
        clear = "\r\x1b[K" if sys.stderr.isatty() else ""
        print(f"{clear}{os.path.basename(folder)}: {st.text}" + (f"\n  {st.url}" if st.url else ""), file=sys.stderr)
        if st.state != upload.DONE:
            rc = 1
            break               # no connection: the rest would fail the same way
    return rc


def cmd_hardware(a) -> int:
    from .core import hardware
    from .core.settings import Settings
    hw = hardware.Hardware(Settings())
    if a.stage:
        hw.set_stage(a.stage)
    for item in a.set or []:
        oid, _, state = item.partition("=")
        try:
            hw.set_state(oid, state)
        except ValueError as e:
            sys.exit(f"{e}. Options: {', '.join(hardware.BY_ID)}; states: yes, no, auto")
    for item in a.port or []:
        oid, _, path = item.partition("=")
        if oid not in ("meter_pico", "logic_pico", "esp32"):
            sys.exit("--port works for meter_pico, logic_pico and esp32")
        hw.settings.set(f"hardware.{oid}.port", path)
    for item in a.can or []:
        oid, _, rest = item.partition(":")
        if oid not in ("can_a", "can_b"):
            sys.exit("--can takes can_a or can_b, e.g. can_a:osc=40000000:spi=1000000")
        for kv in rest.split(":") if rest else []:
            key, _, value = kv.partition("=")
            if key not in ("osc", "spi") or not value.isdigit():
                sys.exit(f"bad --can setting {kv!r}: osc=HZ or spi=HZ")
            hw.settings.set(f"hardware.{oid}." + ("oscillator" if key == "osc" else "spi_hz"), int(value))
    marks = {hardware.OK: "ok ", hardware.WAIT: "?  ", hardware.BAD: "!! ", hardware.OFF: "-  "}
    for st in hw.report():
        o = st.option
        print(f"{marks[st.level]}{o.id:<15} stage {o.stage:<3} {st.state:<4} {o.name:<24} {st.text}")
        if st.level == hardware.BAD or a.verbose:
            print(f"{'':<23}{st.detail or o.hint}")
    print(f"\nStored in {hw.settings.file} (\"hardware\"). Not installed = never touched.")
    return _boot_config(hw, a)


def _boot_config(hw, a) -> int:
    """The Pi's boot settings for the installed options (core.bootconfig): show, and with --apply do."""
    from .core import bootconfig
    plan = bootconfig.plan(hw)
    if plan.empty:
        if bootconfig.restart_needed(hw.settings):
            print("Boot settings: saved; restart to finish.")
        elif any(hw.state(o) != "no" for o in bootconfig.PARTS):
            print("Boot settings: up to date.")
        return 0
    print("\nBoot settings to change for these options:")
    print("\n".join("  " + line for line in plan.describe(bootconfig.is_pi5())))
    if not a.apply:
        print("Run again with --apply to make the change.")
        return 0
    if not a.yes and input("Change the Pi's boot settings? [y/N] ").strip().lower() != "y":
        return 1
    result = bootconfig.apply(plan, hw.settings)
    print(result.message)
    return 0 if result.ok else 1


def cmd_bluetooth(a) -> int:
    """Bluetooth OBD adapters: list, scan, pair, rename, use, test, forget (core.bluetooth)."""
    from .core import bluetooth as bt
    from .core.settings import Settings
    settings = Settings()
    act = a.action
    if act in ("probe", "console", "kline"):
        return _elm_tool(a, settings)
    if act in ("pair", "rename", "use", "test", "forget") and not a.address:
        print(f"bluetooth {act} needs an ADDRESS (AA:BB:CC:DD:EE:FF)", file=sys.stderr)
        return 2
    addr = (a.address or "").upper()
    try:
        if act in ("list", "scan", "pair"):
            problem = bt.controller().problem()
            if problem:
                print(problem, file=sys.stderr)
                return 2
        if act == "list":
            chosen = (settings.get("elm.adapter") or "").upper()
            rows = bt.adapter_choices(settings, bt.paired_devices())
            if not rows:
                print("No Bluetooth OBD adapters yet. canopener-cli bluetooth scan, then bluetooth pair ADDRESS.")
            for address, label in rows:
                print(f"{'*' if address == chosen else ' '} {address}  {label}")
            from .core import adapter_buses
            extra = adapter_buses.available()
            if extra:
                print("\nAdapter bus setups from plugins (obd / uds --elm --elm-bus ID):")
                for b in extra:
                    state = "ready" if b.commands else "NOT FILLED IN YET"
                    print(f"  {b.id:24} {b.label}  [{state}; proven: {b.verified or 'no'}]"
                          + (f"  <- {b.source}" if b.source else ""))
            return 0
        if act == "scan":
            print(f"Looking for {bt.SCAN_SECONDS} s (plug the adapter into the vehicle first)...", flush=True)
            found = bt.scan()
            for d in found:
                tags = [t for t in ("OBD?" if d.looks_like_obd else "", "paired" if d.paired else "") if t]
                print(f"  {d.address}  {d.name or '(no name)':28} {d.kind_text}"
                      + (f"  [{', '.join(tags)}]" if tags else ""))
            if not found:
                print("  nothing found")
            return 0
        if act == "pair":
            print(bt.pair(addr, a.pin))
            if a.name:
                bt.set_name(settings, addr, a.name)
            settings.set("elm.adapter", addr)
            print(f"{bt.label(settings, addr)} is now the Bluetooth OBD adapter (obd --elm uses it).")
            return 0
        if act == "rename":
            bt.set_name(settings, addr, a.name or "")
            print(f"{addr}: {bt.label(settings, addr)}")
            return 0
        if act == "use":
            settings.set("elm.adapter", addr)
            print(f"Using {bt.label(settings, addr)}")
            return 0
        if act == "forget":
            if not _confirm(f"Unpair {bt.label(settings, addr)} ({addr})?", a.yes):
                return 1
            bt.remove(addr)
            bt.forget_name(settings, addr)
            print("Forgotten")
            return 0
        if act == "test":
            from .can.elm327 import ElmBackend
            b = ElmBackend(addr, protocol=settings.get("elm.protocol") or "0")
            print(f"Connecting to {bt.label(settings, addr)}...", flush=True)
            b.open()
            try:
                print(f"  adapter: {b.stn or b.version}" + (f" ({b.device})" if b.device else ""))
                print(f"  battery: {b.voltage or 'not reported'}")
                print(f"  vehicle: {b.protocol_name or 'no answer (key on? plugged in?)'}")
            finally:
                b.close()
            return 0
    except (bt.BluetoothError, OSError) as e:
        print(str(e), file=sys.stderr)
        return 1
    return 2


def _open_elm(spec: str, protocol: str):
    """An opened ElmBackend for 'bluetooth probe / console': an address (or name, tcp:, /dev/...),
    or 'sim[:vehicle]' for the simulated adapter. Returns (backend, simulator or None)."""
    from .can.elm327 import ElmBackend, SocketStream
    if spec == "sim" or spec.startswith("sim:"):
        from .sim.elm import SimulatedCar
        vehicle = spec.partition(":")[2] or "11"
        sim = SimulatedCar(vehicle=vehicle)
        sim.start()
        b = ElmBackend("simulated", protocol="A" if vehicle == "j1939" and protocol == "0" else protocol,
                       stream=SocketStream(sim.adapter.host_socket))
    else:
        sim = None
        b = ElmBackend(_elm_channel(spec), protocol=protocol)
    try:
        b.open()
    except Exception:
        if sim:
            sim.stop()
        raise
    return b, sim


def _elm_tool(a, settings) -> int:
    from .can import elm327, elm_probe
    protocol = a.protocol or settings.get("elm.protocol") or "0"
    try:
        print(f"Connecting to {a.address or 'the picked adapter'}...", file=sys.stderr, flush=True)
        b, sim = _open_elm(a.address or "", protocol)
    except (OSError, ValueError) as e:
        print(f"Couldn't connect: {e}", file=sys.stderr)
        return 1
    try:
        if a.action == "kline":
            from .can import kline_probe
            why = kline_probe.can_probe(b)
            if why:
                print(why, file=sys.stderr)
                return 1
            print(kline_probe.WARNING_FULL if a.scan else kline_probe.WARNING, file=sys.stderr)
            if a.scan and not a.yes and input("Engine off, key on? Run the full scan? [y/N] ").strip().lower() != "y":
                return 1
            report = kline_probe.run(b, progress=lambda t: print(f"  {t}...", file=sys.stderr, flush=True),
                                     full=a.scan)
            text = "\n".join(report.lines()) + "\n"
            if a.out:
                with open(os.path.expanduser(a.out), "w") as f:
                    f.write(text)
                print("\n".join(report.summary()))
                print(f"\nThe full transcript is in {a.out}")
            else:
                print(text, end="")
            return 0
        if a.action == "probe":
            items = elm_probe.probe(b, protocols=a.protocols, listen=a.listen or 0.0, j1939=a.j1939,
                                    progress=lambda t: print(f"  {t}...", file=sys.stderr, flush=True))
            print("\n".join(elm_probe.report_lines(items)))
            print("\nPaste this into docs/BLUETOOTH-OBD.md (\"Adapters tried\") to record what this adapter does.")
            return 0
        print(f"{b.describe()}. Adapter commands only (AT..., ST...); 'quit' to leave. Monitoring commands "
              f"(ATMA ...) run for {a.monitor_seconds:g} s.")
        while True:
            try:
                line = input("elm> ").strip()
            except EOFError:
                break
            if line.lower() in ("quit", "exit", "q"):
                break
            if not line:
                continue
            why = elm327.console_refusal(line)
            if why:
                print(f"  refused: {why}")
                continue
            lines = b.monitor(line, a.monitor_seconds) if elm327.is_monitor_command(line) else b.command(line, 5.0)
            print("\n".join(f"  {x}" for x in lines) or "  (no answer)")
        return 0
    except OSError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        b.close()
        if sim:
            sim.stop()


def _serial_lines(specs: list[str]) -> list[tuple[str, str]]:
    """["console=/dev/ttyUSB0", "/dev/ttyUSB1"] -> [("console", "/dev/ttyUSB0"), ("B", "/dev/ttyUSB1")]."""
    out = []
    for i, spec in enumerate(specs):
        name, eq, port = spec.partition("=")
        if not eq or "://" in name:
            name, port = chr(ord("A") + i), spec
        out.append((name.strip(), port.strip()))
    return out


def cmd_serial(a) -> int:
    from .serialline import port as sp
    from .serialline.framing import Framing, guess_baud
    if a.action == "ports":
        if not sp.available():
            print(sp.INSTALL_HINT, file=sys.stderr)
            return 1
        ports = sp.list_ports()
        if not ports:
            print("No serial ports found. Plug the USB-serial adapter in and try again.")
        for p in ports:
            lat = sp.latency_ms(p.device)
            note = f"   (latency timer {lat} ms: 1 ms keeps messages apart)" if lat and lat > 1 else ""
            stable = sp.by_id(p.device)
            print(f"{p.device:<16} {p.description or '?'}" + (f"\n{'':<16} {stable}" if stable != p.device else "")
                  + note)
        return 0
    if a.action == "baud":
        from .core import logic
        if not a.target or not a.channel:
            print("usage: canopener-cli serial baud CAPTURE.sr --channel D3", file=sys.stderr)
            return 2
        try:
            widths = logic.pulse_widths(a.target, a.channel)
        except (OSError, ValueError, KeyError) as e:
            print(f"Can't read {a.target}: {e}", file=sys.stderr)
            return 1
        rate, std = guess_baud(widths)
        if rate is None:
            print(f"No steady bit width in {len(widths)} pulses: not a UART at this sample rate, or noise.")
            return 1
        print(f"About {rate:,.0f} baud" + (f": {std} is the standard rate" if std else " (no standard rate near it)"))
        return 0
    if a.action == "analyze":
        from .core import serial_analysis
        if not a.target:
            print("usage: canopener-cli serial analyze CAPTURE_FOLDER (or serial.log)", file=sys.stderr)
            return 2
        try:
            r = serial_analysis.analyze_capture(a.target, gap=a.gap / 1000 if a.gap else None, head=a.head)
        except (OSError, ValueError) as e:
            print(f"Can't read {a.target}: {e}", file=sys.stderr)
            return 1
        print("\n".join(r.lines()))
        if a.json:
            import json
            with open(a.json, "w") as f:
                json.dump(r.to_dict(), f, indent=2)
        return 0

    # sniff: record the lines into a capture, Enter tags a timestamp
    import queue
    import threading

    from .core.capture import Capture
    if not a.line:
        print("Name at least one line: --line console=/dev/ttyUSB0 (one per direction of the link)", file=sys.stderr)
        return 2
    try:
        framing = Framing.parse(a.framing)
        sniffer = sp.Sniffer.open(_serial_lines(a.line), framing, labels=dict(x.split("=", 1) for x in a.label or []))
    except (ValueError, sp.SerialError) as e:
        print(e, file=sys.stderr)
        return 1
    if a.show:
        def show(c):
            print(f"{c.t:.3f} {c.line:>10} {c.data.hex(' ').upper()}", file=sys.stderr)
        sniffer.add_listener(show)
    cap = Capture(a.name or "Serial sniff", plan=[x.strip() for x in (a.plan or "").split(",") if x.strip()])
    cap.start(None, root=a.dir, meta={"source": "serial", "front_end": "canopener-cli"}, serial=sniffer)
    sniffer.start()
    lines = ", ".join(f"{t.line}={t.device}" for t in sniffer.taps)
    print(f"Recording {lines} at {framing}. Enter tags the next timestamp, text + Enter adds one with that "
          "label, q + Enter (or Ctrl-C) stops.", file=sys.stderr)
    typed: queue.Queue = queue.Queue()

    def read_stdin():
        for row in sys.stdin:
            typed.put(row.rstrip("\n"))
        typed.put(None)
    if sys.stdin and sys.stdin.isatty():
        threading.Thread(target=read_stdin, daemon=True).start()
    end = time.time() + a.seconds if a.seconds else None
    try:
        while end is None or time.time() < end:
            errors = sniffer.errors()
            if errors:
                print("Stopped: " + "; ".join(errors), file=sys.stderr)
                break
            try:
                row = typed.get(timeout=0.2)
            except queue.Empty:
                continue
            if row is None or row.strip().lower() == "q":
                break
            i = cap.add_marker(row.strip()) if row.strip() else cap.tag_next()
            print(f"  tagged {cap.title(i)}", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    cap.stop()
    sniffer.close()
    folder = cap.save(system=a.units)
    print(f"{cap.serial_bytes:,} bytes saved in {folder}", file=sys.stderr)
    with open(os.path.join(folder, "serial_analysis.txt")) as f:
        print(f.read())
    return 0


def cmd_changes(a) -> int:
    """What changed between the phases of a capture (core.changes): no decoder needed."""
    import json

    from .core import changes
    try:
        report = changes.analyze_capture(a.capture, parts=a.parts)
    except OSError as e:
        print(f"Can't read {a.capture}: {e}", file=sys.stderr)
        return 1
    if not report.frames:
        print("No frames in that capture.", file=sys.stderr)
        return 1
    if a.json:
        with open(os.path.expanduser(a.json), "w") as f:
            json.dump(report.to_dict(), f, indent=1)
    print("\n".join(report.lines(a.top)))
    if len(report.phases) < 2:
        print("\nOne phase only: tag timestamps while recording (or pass --parts N) to compare states.")
    return 0


def cmd_suggest(a) -> int:
    """Undecoded traffic in a log that other profiles on this unit decode (core.suggest)."""
    from .can.logfile import read_log
    from .core import suggest
    path = os.path.join(a.capture, "capture.log") if os.path.isdir(a.capture) else a.capture
    try:
        frames = read_log(path)
    except OSError as e:
        print(f"Can't read {path}: {e}", file=sys.stderr)
        return 1
    found, _errors = profiles.available_profiles()
    active = profiles.active()
    borrowed = suggest.from_frames(frames, found, active)
    using = f" (profile in use: {active.name})" if active is not None else ""
    if not borrowed:
        print(f"Nothing: no other profile here decodes this log's undecoded traffic{using}.")
        return 0
    print(f"{len(frames):,} frames. Values other profiles know, checked against this log{using}:")
    for b in borrowed:
        print(f"\n{b.describe()}")
        for c in b.checks:
            mark = "  ok " if c.ok else "  no "
            how = c.note or ("moved, in range" if c.moved else "fits, didn't move")
            print(f"{mark} {c.signal.name}  ({how})")
    return 0


def cmd_remote(a) -> int:
    """The helper's end of remote help (core.remote.Operator): list the units that are calling in,
    send one a command, save its screen."""
    import base64
    import json

    from .core import remote, upload
    from .core.settings import Settings
    url = a.url or os.environ.get("CANOPENER_REMOTE_URL") or Settings().get("upload.url") or upload.default_server()[0]
    token = os.environ.get("CANOPENER_REMOTE_TOKEN") or ""
    path = os.path.join(config.config_dir(), "remote-token")
    if not token and os.path.exists(path):
        with open(path) as f:
            token = f.read().strip()
    if not url or not token:
        print("Needs the server's address (--url, or this unit's upload server) and a remote token "
              f"(CANOPENER_REMOTE_TOKEN, or the file {path}).", file=sys.stderr)
        return 2
    op = remote.Operator(url, token)
    try:
        sessions = op.sessions()
        if a.action == "list":
            for s in sessions:
                left = max(0, int((s["expires"] - time.time()) / 60))
                print(f"{s['id']}  {s['label'] or '(no label)'}  unit {s['device'][:6]}  {left} min left  "
                      f"{s['done']} done  {s.get('info') or ''}")
            if not sessions:
                print("No unit is calling in. On the handheld: System > Remote help > Start.")
            return 0
        sid = a.session or (sessions[0]["id"] if len(sessions) == 1 else "")
        if not sid:
            print("Which session? (remote list shows them; --session ID)", file=sys.stderr)
            return 2
        sid = next((s["id"] for s in sessions if s["id"].startswith(sid)), sid)
        if a.action == "close":
            op.close(sid)
            return 0
        if a.action == "shot":
            got = op.send(sid, "screenshot")
            if not got.get("ok"):
                print(got.get("error"), file=sys.stderr)
                return 1
            with open(a.name or "screen.png", "wb") as f:
                f.write(base64.b64decode(got["value"]["png_base64"]))
            print(a.name or "screen.png")
            return 0
        args = json.loads(a.args) if a.args else {}
        got = op.send(sid, a.name or "status", args, wait=a.wait)
        print(json.dumps(got, indent=1, ensure_ascii=False))
        return 0 if got.get("ok") else 1
    except (upload.UploadError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1


def cmd_profiles(a) -> int:
    if getattr(a, "upload", None):
        from .core import upload
        from .core.settings import Settings
        found, _errors = profiles.available_profiles()
        # only your own: a plugin's profiles (a private one, say) stay where they were installed from
        todo = [p for p in found if profiles.origin(p) == "user" and p.path and (a.upload == "all" or p.id == a.upload)]
        if not todo:
            print(f"No profile of your own called {a.upload!r} (canopener-cli profiles lists them).", file=sys.stderr)
            return 1
        failed = 0
        for p in todo:
            try:
                got = upload.upload_profile(Settings(), p.path)
                how = "already there" if got.get("unchanged") else f"uploaded, revision {got['revision']}"
                print(f"{p.id}: {how}")
            except upload.UploadError as e:
                failed += 1
                print(f"{p.id}: {e}", file=sys.stderr)
        return 1 if failed else 0
    found, errors = profiles.available_profiles()
    for p in found:
        print(f"{p.id:<20} {p.name}{'' if p.builtin else '  (user)'}")
    for e in errors:
        print(f"!! {e}", file=sys.stderr)
    print(f"\nUser profiles live in {profiles.user_dir()}")
    return 0


def _elm_args(p):
    p.add_argument("--elm", nargs="?", const="", metavar="ADAPTER",
                   help="use a Bluetooth OBD adapter (ELM327-style): the picked one, or a name / address / "
                   "tcp:HOST:PORT / /dev/rfcomm0, or 'sim' for the simulated one. Slower than a CAN interface.")
    p.add_argument("--elm-bus", default="", metavar="ID",
                   help="with --elm: a plugin's bus setup for the adapter (canopener-cli bluetooth lists them)")
    p.add_argument("--elm-protocol", default="0", metavar="N",
                   help="with --elm: the adapter's protocol, 0 = search (default), 6 = CAN 11-bit 500k, "
                   "7 = 29-bit 500k, 8 / 9 = 250k")


IFACE_HELP = "a Linux CAN interface (can0), or a python-can adapter as interface:channel (pcan:PCAN_USBBUS1)"
BITRATE_HELP = "bits/s for a python-can adapter (a Linux interface keeps the bitrate it was set up with)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="canopener-cli", description="CANopener: J1939, CANopen and OBD-II diagnostics")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--units", choices=list(units.SYSTEMS), default=units.METRIC)
    for q, (_, options) in units.QUANTITIES.items():
        ap.add_argument(f"--{q}", choices=list(options), help=f"{q} unit (implies --units custom)")
    ap.add_argument("--profile", help="machine profile id (see 'profiles')")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="read fault codes from a live bus")
    s.add_argument("--iface", default="can0", help=IFACE_HELP)
    s.add_argument("--bitrate", type=int, default=250000, help=BITRATE_HELP)
    s.add_argument("--sim", action="store_true", help="scan the built-in simulator instead")
    s.add_argument("--listen-only", action="store_true", help="send nothing (DM1 + live data only)")
    s.add_argument("--elm", nargs="?", const="", metavar="ADAPTER",
                   help="through a Bluetooth OBD adapter on J1939 (250k): the picked one, a name / address, or "
                   "'sim' (a simulated machine). It may miss frames on a busy bus.")
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

    it = sub.add_parser("interfaces", help="list CAN interfaces")
    it.add_argument("--python-can", action="store_true", help="also look for adapters python-can can open "
                                                             "(PEAK, Kvaser, gs_usb ...; takes a few seconds)")
    it.set_defaults(fn=cmd_interfaces)

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
    pr = sub.add_parser("profiles", help="list machine profiles (--upload sends your own to the upload server)")
    pr.add_argument("--upload", metavar="ID", help="send one of your own profiles (or 'all') to the upload server")
    pr.set_defaults(fn=cmd_profiles)

    rm = sub.add_parser("remote", help="the helper's end of remote help: list the units calling in "
                        "(System > Remote help on the handheld), send one a command, save its screen")
    rm.add_argument("action", choices=["list", "send", "shot", "close"], nargs="?", default="list")
    rm.add_argument("name", nargs="?", help="send: the action (status, state, codes, logs, file, request ...); "
                    "shot: the file to save (screen.png)")
    rm.add_argument("--args", help='send: the action\'s arguments as JSON, e.g. \'{"requests": ["0100"]}\'')
    rm.add_argument("--session", help="the session id (or its first characters); not needed when only one is open")
    rm.add_argument("--url", help="the server's address (default: this unit's upload server)")
    rm.add_argument("--wait", type=float, default=120.0, help="send: seconds to wait for the answer")
    rm.set_defaults(fn=cmd_remote)
    ch = sub.add_parser("changes", help="what changed between the timestamps of a capture: the bytes and bits "
                        "that differ from one state of the machine to the next, with no decoder needed")
    ch.add_argument("capture", help="a saved capture folder (its timestamps are the phases) or a log file")
    ch.add_argument("--parts", type=int, default=0, metavar="N",
                    help="a capture without timestamps: compare N equal slices of it")
    ch.add_argument("--top", type=int, default=15, help="changes listed per step (default 15)")
    ch.add_argument("--json", metavar="FILE", help="also write everything (every field, per phase) as JSON")
    ch.set_defaults(fn=cmd_changes)
    sg = sub.add_parser("suggest", help="undecoded traffic in a log that other profiles on this unit decode, each "
                        "value checked against the log (the profile in use: --profile)")
    sg.add_argument("capture", help="a capture folder or a log file")
    sg.set_defaults(fn=cmd_suggest)
    sr = sub.add_parser("serial", help="serial lines (a controller's console port): ports (list them), sniff "
                        "(record one or more lines into a capture, Enter tags a timestamp), analyze (what the bytes "
                        "look like: message kinds, checksums, exchanges, text), baud (the rate from a logic capture)")
    sr.add_argument("action", choices=["ports", "sniff", "analyze", "baud"])
    sr.add_argument("target", nargs="?", help="analyze: a capture folder or serial.log; baud: a sigrok .sr file")
    sr.add_argument("--line", action="append", metavar="NAME=PORT",
                    help="sniff: a line to record (a port, or pyserial URL); repeat for each direction")
    sr.add_argument("--label", action="append", metavar="NAME=TEXT", help="sniff: what a line carries")
    sr.add_argument("--framing", default="9600 8N1", help="sniff: baud rate and framing (default 9600 8N1)")
    sr.add_argument("--seconds", type=float, help="sniff: stop after this long")
    sr.add_argument("--name", help="sniff: the capture's name")
    sr.add_argument("--plan", help="sniff: the timestamps to tag, comma separated (\"connect,menu,tester\")")
    sr.add_argument("--dir", help="sniff: where to save the capture (default: the logs folder)")
    sr.add_argument("--show", action="store_true", help="sniff: print every chunk as it arrives")
    sr.add_argument("--gap", type=float, help="analyze: ms of quiet that end a message (default 10)")
    sr.add_argument("--head", type=int, default=1, help="analyze: leading bytes that name a message kind")
    sr.add_argument("--json", metavar="FILE", help="analyze: also write everything as JSON")
    sr.add_argument("--channel", help="baud: the logic channel the line is on (D3, Probe 1, ...)")
    sr.set_defaults(fn=cmd_serial)
    b = sub.add_parser("bluetooth", help="Bluetooth OBD adapters (ELM327-style): list, scan, pair, rename, use, "
                       "test, forget, probe (what it can do), console (adapter commands), kline (read-only deep "
                       "probe of an older K-line vehicle: what answers besides generic OBD-II)")
    b.add_argument("action", nargs="?", default="list",
                   choices=["list", "scan", "pair", "rename", "use", "test", "forget", "probe", "console", "kline"])
    b.add_argument("address", nargs="?", help="the adapter's Bluetooth address, AA:BB:CC:DD:EE:FF (probe / "
                   "console: also a name you gave it, or sim / sim:vpw / sim:j1939 ... for the simulated one)")
    b.add_argument("--name", help="pair / rename: a name for it (e.g. 'vLinker (truck)')")
    b.add_argument("--pin", default="1234", help="pair: the PIN if it asks (default 1234; some use 0000)")
    b.add_argument("--yes", action="store_true", help="forget without asking")
    b.add_argument("--protocol", help="probe / console: the adapter protocol to start with (0 = search, A = J1939)")
    b.add_argument("--protocols", action="store_true",
                   help="probe: also try each OBD-II protocol with a standard read (wakes K-line cars: slow)")
    b.add_argument("--listen", type=float, metavar="SECONDS", help="probe: also listen passively this long")
    b.add_argument("--j1939", action="store_true", help="probe --listen: listen on J1939 (250k) instead")
    b.add_argument("--scan", action="store_true",
                   help="kline: also try every data-block and identifier number: ENGINE OFF only (asks first; "
                   "--yes skips the question)")
    b.add_argument("--out", metavar="FILE", help="kline: write the full transcript here and print the summary")
    b.add_argument("--monitor-seconds", type=float, default=5.0, help="console: how long ATMA etc. run")
    b.set_defaults(fn=cmd_bluetooth)

    hw = sub.add_parser("hardware", help="installed options on the handheld: what's fitted and whether it's found")
    hw.add_argument("--stage", help="mark everything up to this build stage installed, the rest not (1, 2a ... 8)")
    hw.add_argument("--set", action="append", metavar="ID=STATE", help="one option: yes / no / auto (repeatable)")
    hw.add_argument("--can", action="append", metavar="ID:osc=HZ:spi=HZ",
                    help="a CAN stack's crystal and SPI speed, e.g. can_a:osc=20000000:spi=1000000 (spi=0: full speed)")
    hw.add_argument("--apply", action="store_true", help="also set up the Pi's boot settings and drivers for them")
    hw.add_argument("--yes", action="store_true", help="--apply without asking")
    hw.add_argument("--port", action="append", metavar="ID=PATH", help="serial port of a Pico or the ESP32")
    hw.add_argument("-v", "--verbose", action="store_true", help="show what each option is and where to look")
    hw.set_defaults(fn=cmd_hardware)

    cl = sub.add_parser("clear", help="clear J1939 fault codes: active (DM11, default) or stored (DM3); "
                                      "asks first, then reads the codes again")
    cl.add_argument("--iface", default="can0", help=IFACE_HELP)
    cl.add_argument("--bitrate", type=int, default=250000, help=BITRATE_HELP)
    cl.add_argument("--sim", action="store_true", help="clear the built-in simulator's codes instead")
    g = cl.add_mutually_exclusive_group()
    g.add_argument("--stored", action="store_true", help="stored / previously active codes (DM3)")
    g.add_argument("--both", action="store_true", help="active and stored codes (DM11 + DM3)")
    cl.add_argument("--module", metavar="SA", help="only this module's source address in hex, e.g. 00 "
                                                   "(default: every module)")
    cl.add_argument("--yes", action="store_true", help="don't ask first")
    cl.set_defaults(fn=cmd_clear)

    o = sub.add_parser("obd", help="OBD-II (J1979) scan tool: codes from every module, readiness, live data, "
                                   "vehicle info; --clear / --evap-test send commands")
    o.add_argument("--iface", default="can0", help=IFACE_HELP)
    o.add_argument("--bitrate", type=int, default=500000, help=BITRATE_HELP)
    o.add_argument("--sim", action="store_true", help="use the built-in OBD-II simulator instead")
    o.add_argument("--freeze", action="store_true", help="also read the freeze frame")
    o.add_argument("--tests", action="store_true", help="also read on-board test results (Mode 06)")
    o.add_argument("--all", action="store_true", help="freeze frame and test results too")
    o.add_argument("--watch", type=float, metavar="SECONDS", help="keep printing live data this long")
    o.add_argument("--bench", type=float, metavar="SECONDS", help="time how fast the connection answers, this long")
    _elm_args(o)
    g = o.add_mutually_exclusive_group()
    g.add_argument("--clear", action="store_true", help="clear codes on every module (Mode 04; asks first)")
    g.add_argument("--evap-test", action="store_true", help="start the EVAP leak test (Mode 08; asks first)")
    o.add_argument("--yes", action="store_true", help="don't ask before --clear / --evap-test")
    o.set_defaults(fn=cmd_obd)

    ud = sub.add_parser("uds", help="read a module via UDS (ISO 14229): DTCs with status, a data identifier")
    ud.add_argument("--iface", default="can0", help=IFACE_HELP)
    ud.add_argument("--bitrate", type=int, default=500000, help=BITRATE_HELP)
    ud.add_argument("--sim", action="store_true", help="read the built-in simulator instead")
    ud.add_argument("--module", default="7E0", help="module request CAN ID in hex (default 7E0)")
    ud.add_argument("--did", help="also read this data identifier (hex, e.g. F190)")
    ud.add_argument("--info", action="store_true",
                    help="also read the standard identification: VIN, part / serial numbers, software versions")
    ud.add_argument("--supported", action="store_true", help="also list every code the module can set")
    ud.add_argument("--find", action="store_true",
                    help="find modules: ask every ID 700-7EF if something's there (TesterPresent; a minute or two)")
    _elm_args(ud)
    ud.set_defaults(fn=cmd_uds)

    co = sub.add_parser("canopen", help="CANopen nodes: who's on the bus, state, errors (EMCY and error "
                                        "history), identity; --nmt / --clear-errors send commands")
    co.add_argument("--iface", default="can0", help=IFACE_HELP)
    co.add_argument("--bitrate", type=int, default=250000, help=BITRATE_HELP)
    co.add_argument("--sim", action="store_true", help="use the built-in CANopen simulator instead")
    co.add_argument("--listen-only", action="store_true", help="send nothing: heartbeats, EMCY and PDOs only")
    co.add_argument("--listen", type=float, default=2.0, metavar="SECONDS", help="listen this long first")
    co.add_argument("--find", action="store_true", help="ask every node ID 1-127 (finds nodes with no heartbeat)")
    co.add_argument("--info", action="store_true", help="read each node's identity, name and versions (SDO)")
    co.add_argument("--errors", action="store_true", help="read each node's error history (object 1003)")
    co.add_argument("--events", action="store_true", help="also list boot-ups, state changes and NMT commands")
    co.add_argument("--node", help="only this node ID, in hex (e.g. 01)")
    co.add_argument("--nmt", choices=["start", "stop", "preop", "reset", "reset-comm"],
                    help="send an NMT command to --node (default every node; asks first)")
    co.add_argument("--clear-errors", action="store_true", help="clear --node's error history (asks first)")
    co.add_argument("--yes", action="store_true", help="don't ask before --nmt / --clear-errors")
    co.add_argument("--pdo-map", action="store_true",
                    help="ask each node what's in its process data (PDO mapping), so it can be decoded")
    co.add_argument("--save-profile", metavar="NAME",
                    help="with the PDO mapping: save a profile that decodes those PDOs (name them later)")
    co.add_argument("--force", action="store_true", help="replace a profile of the same name")
    co.set_defaults(fn=cmd_canopen)

    mo = sub.add_parser("modules", help="which modules are on the bus, and which the profile (--profile) says the "
                                        "machine has but aren't heard or went quiet; only listens")
    mo.add_argument("--iface", default="can0", help=IFACE_HELP)
    mo.add_argument("--bitrate", type=int, default=250000, help=BITRATE_HELP)
    msrc = mo.add_mutually_exclusive_group()
    msrc.add_argument("--sim", action="store_true", help="listen to the built-in J1939 simulator instead")
    msrc.add_argument("--canopen-sim", action="store_true", help="listen to the built-in CANopen simulator instead")
    msrc.add_argument("--log", metavar="FILE", help="check a recorded candump log instead (as it ends)")
    mo.add_argument("--listen", type=float, default=5.0, metavar="SECONDS", help="listen this long first")
    mo.add_argument("--watch", action="store_true", help="then keep listening and print each module that drops "
                    "out, is missing or comes back")
    mo.add_argument("--remember", nargs="?", const="-", metavar="NAME",
                    help="on a machine that works: save the modules heard as this machine's, in the --profile if "
                    "it's your own, else in a new profile built on it (NAME, or 'My <profile>')")
    mo.set_defaults(fn=cmd_modules)

    im = sub.add_parser("import", help="make a profile from a DBC file, or a CANopen device's EDS / DCF file")
    im.add_argument("file", help="the .dbc, .eds or .dcf file")
    im.add_argument("--name", help="the profile's name (default: the file's, or the device's product name)")
    im.add_argument("--node", help="EDS: the device's node ID in hex (a DCF usually says)")
    im.add_argument("--protocol", choices=["j1939", "canopen", "j1939+canopen"],
                    help="DBC: which screens the profile uses (default: from the IDs in the file)")
    im.add_argument("--show", action="store_true", help="print the profile instead of saving it")
    im.add_argument("--force", action="store_true", help="replace a profile of the same name")
    im.set_defaults(fn=cmd_import)

    sub.add_parser("allow-setup", help="install a sudoers rule so bringing up a CAN interface "
                   "needs no password (only 'ip link set canN ...', no stored password)"
                   ).set_defaults(fn=cmd_allow_setup)

    up = sub.add_parser("update", help="check GitHub for a newer CANopener and install it (git pull)")
    up.add_argument("--check", action="store_true", help="only check, don't install")
    up.add_argument("--yes", action="store_true", help="install without asking")
    up.set_defaults(fn=cmd_update)

    pl = sub.add_parser("plugins", help="plugins from GitHub repositories (private ones too): sign in, add, "
                        "check, update; enable / disable a built-in one (zapi_console)")
    pl.add_argument("action", nargs="?", default="list",
                    choices=["list", "update", "add", "remove", "login", "logout", "repos", "enable", "disable"])
    pl.add_argument("repo", nargs="?", help="owner/name (add, remove), or a built-in plugin's id (enable, disable)")
    pl.add_argument("--dir", help="add: where to put the checkout (default ~/<name>)")
    pl.set_defaults(fn=cmd_plugins)

    tm = sub.add_parser("team", help="team access: get the team plugin from your upload server (join with an "
                        "invite link, or request and wait for approval); check, leave")
    tm.add_argument("action", nargs="?", default="status", choices=["status", "join", "request", "check", "leave"])
    tm.add_argument("arg", nargs="?", help="join: the invite link; request: the server's address")
    tm.add_argument("--name", help="what to call this computer on the server (default: user on host)")
    tm.add_argument("--yes", action="store_true", help="check: install an update without asking; leave: don't ask")
    tm.set_defaults(fn=cmd_team)

    ta = sub.add_parser("team-admin", help="team access, the owner's side (admin token): list installs, invite, "
                        "approve, revoke, rename, forget; publish the team plugin")
    ta.add_argument("action", choices=["list", "invite", "invites", "uninvite", "approve", "revoke", "rename",
                                       "forget", "publish", "pack"])
    ta.add_argument("who", nargs="?", help="an install (serial CO-XXXX-XXXX, ID or name); invite: a name for it; "
                    "uninvite: the invite's id; publish: the plugin's folder (a git checkout) or a zip")
    ta.add_argument("--name", help="rename: the new name")
    ta.add_argument("--days", type=float, default=7, help="invite: days until it expires (default 7, at most 90)")
    ta.add_argument("--version", help="publish: the version to show (default: git describe, or the date)")
    ta.add_argument("--url", help="the server (default: this computer's upload server)")
    ta.add_argument("--yes", action="store_true", help="forget: don't ask")
    ta.set_defaults(fn=cmd_team_admin)

    c = sub.add_parser("capture", help="re-generate and print a capture folder's summary (optionally trim it)")
    c.add_argument("folder")
    c.add_argument("--trim", nargs=2, type=float, metavar=("BEFORE", "AFTER"),
                   help="first keep only BEFORE minutes before the first timestamp to AFTER minutes after the last")
    c.add_argument("--keep-full", action="store_true", help="with --trim: keep the full log as capture_full.log")
    c.add_argument("--machine", action="append", metavar="FIELD=VALUE",
                   help="set a machine detail (make, model, serial, unit, hours, customer); repeat for more")
    c.add_argument("--name", help="rename the capture (the folder keeps its name)")
    c.add_argument("--concern", help="the customer concern")
    c.add_argument("--question", help="what to look at in the data (shown in its README)")
    c.set_defaults(fn=cmd_capture)

    upl = sub.add_parser("upload", help="send saved captures to the upload server (deploy/upload-server); "
                                        "no folders: show where uploads go and what's waiting")
    upl.add_argument("folders", nargs="*", help="capture folders to upload now")
    upl.add_argument("--pending", action="store_true", help="send everything waiting in the upload queue")
    upl.add_argument("--status", action="store_true", help="show the upload state of the folders (or the queue)")
    upl.add_argument("--setup", action="store_true", help="set the server address and token, then test them")
    upl.add_argument("--test", action="store_true", help="check the server and the token (sends nothing)")
    upl.add_argument("--on-save", choices=["on", "off"], help="queue every capture for upload when it's saved")
    upl.add_argument("--github", metavar="OWNER/NAME",
                     help="upload to this private GitHub repository instead, with the GitHub sign-in "
                          "(canopener-cli plugins login), then test it")
    upl.set_defaults(fn=cmd_upload)

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
