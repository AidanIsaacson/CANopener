"""Everything the handheld's screens share, with no Pygame in it: settings, the CAN connection,
the live-data models, the meters, the capture (data log), background jobs and status messages.

Screens call methods here and read attributes each frame; nothing here draws. Slow work (scans,
connecting, saving a capture) runs on a worker thread and its result comes back through
``tick()``, which the app calls once per frame on the UI thread, the same polling model as the
desktop GUI (docs/ARCHITECTURE.md, "Threading model"). Tests drive this class directly.
"""

import json
import os
import queue
import threading
import time
import traceback
from collections import deque

from .. import __version__
from ..can import pythoncan
from ..core import battery as batt
from ..core import bluetooth as bt
from ..core import capture as cap
from ..core import (
    config,
    identity,
    interfaces,
    link,
    meters,
    profile_match,
    profiles,
    roster,
    scan,
    sound,
    units,
    upload,
)
from ..core.calibration import Calibration
from ..core.capture import Capture
from ..core.connection import (
    CANOPEN_SIM,
    ELM,
    ELM_SIM,
    OBD_SIM,
    PROFILE_SIM,
    SIMULATOR,
    SOCKETCAN,
    open_connection,
)
from ..core.fieldwatch import FieldWatch
from ..core.hardware import Hardware
from ..core.live import LiveData, ObdLiveData
from ..core.measure import MeasurementHub, MeasurementLiveData
from ..core.settings import Settings
from ..core.sync import PpsReader, PulseClock

SOURCES = [(SOCKETCAN, "CAN adapter"), (ELM, "Bluetooth OBD"), (SIMULATOR, "Simulator (J1939 machine)"),
           (OBD_SIM, "Simulator (OBD-II car)"), (ELM_SIM, "Simulator (Bluetooth OBD car)"),
           (CANOPEN_SIM, "Simulator (CANopen truck)"), (PROFILE_SIM, "Simulate the profile")]
SIMULATORS = (SIMULATOR, OBD_SIM, ELM_SIM, CANOPEN_SIM, PROFILE_SIM)    # listed only with System > Simulators
BLUETOOTH_WARNING = ("Slower than a wired CAN adapter: live data updates a few times a second, and bus "
                     "monitoring and Transmit aren't available.")
PINS = ["1234", "0000", "6789", "1111"]     # what adapters ask for, most common first
BITRATES = [0] + interfaces.BITRATES        # 0 = auto (signal finder)
SAMPLE_EVERY = 0.2                          # live models: 5 samples a second, like the desktop
TRAFFIC_CHECK_SECS = 5.0    # how long into a connection the one-off "little traffic" check waits
LINK_CHECK_SECS = 1.0
TOAST_SECS = 3.5


def bitrate_label(b: int) -> str:
    return "Auto (find signal)" if not b else f"{b // 1000}k"


def source_label(kind: str) -> str:
    return dict(SOURCES).get(kind, kind)


BUSIER_LISTEN = 1.5         # seconds each port is listened to when picking the busier one


def busier(main_frames: int, other_frames: int) -> bool:
    """Should the other port be the main bus? Only when it's clearly busier (a near tie keeps the
    port that was chosen)."""
    return other_frames >= 3 and other_frames > main_frames * 1.2


def measure_port(iface: str, bitrate: int, secs: float = BUSIER_LISTEN) -> tuple[int, int, str]:
    """(frames heard, bitrate, why nothing) on one port, listen-only: never a frame sent, so
    listening at a wrong bitrate can't disturb the machine. Auto (0) finds the bitrate first."""
    from ..can.backends import SocketCanBackend
    why = ""
    if not bitrate:
        bitrate, log = interfaces.detect_bitrate(iface)
        if not bitrate:
            return 0, 0, (log[-1] if log else "no traffic")
    ok, msg = interfaces.bring_up(iface, bitrate, True)
    if not ok:
        return 0, bitrate, msg.split("\n")[0]
    b = SocketCanBackend(iface)
    try:
        b.open()
        good, _errors = interfaces.count_traffic(b, secs)
    except OSError as e:
        return 0, bitrate, str(e)
    finally:
        b.close()
    return good, bitrate, why


class DeviceContext:
    def __init__(self, settings: Settings | None = None, *, start_meters: bool = True):
        self.settings = settings or Settings()
        config.set_data_dir(self.settings.get("data_dir") or None)
        units.set_custom(self.settings.get("units.custom") or {})
        self.connection = None
        self.session = None
        self.j1939_live = LiveData()
        self.fieldwatch = FieldWatch()      # every field on the bus, live (the builder and the bus explorer)
        self.share_failed: set = set()      # profiles (ids) whose last share failed: offered again
        self.profile_matches: list = []     # known profiles this bus looks like (core.profile_match)
        self._matches_checked = False
        self.roster: roster.Roster | None = None    # the missing-module check, about once a second
        self.module_alerts = roster.Alerts()
        self._roster_t = 0.0
        self.obd_live = ObdLiveData()
        self.meter_live = MeasurementLiveData()
        self.calibration = Calibration(self.settings)  # per-channel zero + gain (System > Meters > Calibration)
        self.hub = MeasurementHub(calibration=self.calibration)
        self.hardware = Hardware(self.settings)     # installed options: what this unit has fitted
        self.pulse_clock = PulseClock()
        self.pps: PpsReader | None = None
        self.result = None                  # last J1939 ScanResult
        self.tunnel = None                  # core.tunnel.Tunnel while the SSH tunnel is (or was) open
        self.remote = None                  # core.remote.RemoteLink while remote help is (or was) on
        self.remote_app = None              # the app (for a screenshot) and how to ask a question on it:
        self.remote_show_ask = None         # show(text, on_yes); both set by the front-end
        self._ui_calls: queue.Queue = queue.Queue()
        self.obd_thin: list = []            # modules that list no live data at all (core.obd.THIN_NOTE)
        self.obd_codes: dict | None = None  # last OBD-II read: module -> {stored/pending/permanent: [codes]}
        self.obd_client = None
        self.obd_streamer = None
        self.capture = Capture(plan=list(self.settings.get("log.plan") or []))
        self.last_saved: str | None = None
        self.uploads = upload.UploadQueue(self.settings)     # started by the app (start_uploads)
        self.busy: str | None = None        # what a running job is doing, for a spinner
        self.toasts: deque = deque(maxlen=4)
        self.finder = None
        self.finder_log: deque = deque(maxlen=8)
        self.adapter = link.Status(link.OFF, "Adapter")
        self.bus = link.Status(link.OFF, "Bus: not connected")
        self.second = None                  # a second bus watched listen-only (a Connection), or None
        self.serial = None                  # serial lines a mode is listening to (a serialline Sniffer, e.g. the
        #                                     Zapi console's taps): the data log records them too
        self.second_port = None             # its hardware.CanPort
        self.second_bitrate = 0
        self.bus2 = link.Status(link.OFF, "Bus: not connected")
        self.traffic_hint = ""              # what to try when this bus has little traffic
        self._rate, self._rate2 = link.RateMeter(), link.RateMeter()
        self._traffic_checked = False
        self.focus_spn: int | None = None   # a fault code's SPN to show in Live Data
        self.focus_pids: list[int] = []     # the OBD-II equivalent
        self.python_can_found: list[str] = []
        self.bt_state: bt.ControllerState | None = None     # Bluetooth OBD adapters (core.bluetooth)
        self.bt_paired: list[bt.Device] = []
        self.bt_found: list[bt.Device] = []
        self.bt_scanned = False
        self.speaker = None                 # (tone, volume 0-1) -> bool: the UI's own player (Pygame's mixer)
        self._jobs: queue.Queue = queue.Queue()
        self._last_sample = 0.0
        self.held = False                   # Hold: live values frozen on the screen (the bus and log go on)
        self.battery_monitor = batt.BatteryMonitor()    # the pack, when the button ADC reads it
        self._link_checked = 0.0
        self._bus_level = link.OFF
        self._bus_was_ok = False
        self.apply_profile(self.settings.get("profile"))
        if start_meters:
            self.apply_meters()

    # ------------------------------------------------------------------
    # settings-derived state
    # ------------------------------------------------------------------
    @property
    def units(self) -> str:
        return self.settings.get("units")

    @property
    def protocol(self) -> str:
        return profiles.protocol()

    @property
    def live(self) -> bool:
        return self.session is not None and not self.session.offline

    @property
    def can_transmit(self) -> bool:
        return self.live and not self.session.listen_only

    @property
    def can_model(self) -> LiveData:
        return self.obd_live if self.protocol == profiles.OBD2 else self.j1939_live

    def apply_profile(self, pid: str | None):
        p = profiles.find(pid) if pid else None
        profiles.apply(p)
        self.settings.set("profile", p.id if p else "")
        self.j1939_live.clear()

    def profile_name(self) -> str:
        p = profiles.active()
        return p.name if p else "No profile"

    # --- installed options -> the Pi's boot settings (core.bootconfig) ---
    def boot_plan(self):
        from ..core import bootconfig
        return bootconfig.plan(self.hardware)

    def apply_boot(self, plan, then=None) -> bool:
        """Write the boot settings for ``plan`` and load what can be loaded now (a job: it runs the
        root helper). ``then(result)`` on the UI thread."""
        from ..core import bootconfig
        settings = self.settings

        def done(result):
            self.hardware._cache.clear()
            self.apply_meters()
            if then:
                then(result)
        return self.run_job("Changing boot settings", lambda: bootconfig.apply(plan, settings), done)

    def boot_restart_needed(self) -> bool:
        from ..core import bootconfig
        return bootconfig.restart_needed(self.settings)

    def apply_meters(self):
        """(Re)start the measurement sources: installed options plus those switched on in settings."""
        for src in self.hub.sources():
            self.hub.remove_source(src.id)
        self._stop_pps()
        if self.hardware.available("sync"):
            self.pps = PpsReader(self.pulse_clock)
            self.pps.start()
        for src in meters.sources_from_settings(self.settings, self.hardware, self.pulse_clock):
            self.hub.add_source(src)
        self.hub.set_off(self.settings.get("meters.off") or [])
        self.meter_live.clear()

    def set_meter_channel(self, channel_id: str, on: bool):
        """Switch one meter input on or off: an input that's off isn't shown, charted or recorded."""
        off = [c for c in (self.settings.get("meters.off") or []) if c != channel_id]
        if not on:
            off.append(channel_id)
        self.settings.set("meters.off", off)
        self.hub.set_off(off)
        if not on:
            self.meter_live.drop([channel_id])

    # --- the unit's own CAN ports (a two-channel HAT, or stacks A / B) ---
    def can_ports(self):
        return self.hardware.can_ports()

    def can_port_on(self, port_id: str) -> bool:
        return port_id not in (self.settings.get("can.ports_off") or [])

    def set_can_port(self, port_id: str, on: bool):
        """A port that's off is left out of the interface list. If it was the one in use, the
        connection setting moves to a port that's still on."""
        off = [p for p in (self.settings.get("can.ports_off") or []) if p != port_id]
        if not on:
            off.append(port_id)
        self.settings.set("can.ports_off", off)
        ports = self.can_ports()
        hidden = {p.iface for p in ports if p.id in off and p.iface}
        if self.settings.get("can.iface") in hidden:
            other = next((p.iface for p in ports if p.id not in off and p.iface), None)
            if other:
                self.settings.set("can.iface", other)

    def second_rate(self) -> float:
        return self._rate2.rate or 0.0

    def second_speaks(self) -> str:
        """What's been heard on the second bus, in words."""
        s2 = self.second.session
        heard = [n for n, yes in (("J1939", s2.has_j1939), ("CANopen", s2.has_canopen)) if yes]
        return " and ".join(heard) + " heard" if heard else "nothing recognised yet"

    def other_port(self, iface: str | None = None):
        """The on-board port that isn't the one in use (and isn't switched off), or None."""
        iface = iface or self.settings.get("can.iface")
        ports = self.can_ports()
        if not any(p.iface == iface for p in ports):
            return None
        return next((p for p in ports if p.iface and p.iface != iface and self.can_port_on(p.id)), None)

    def second_to_watch(self, iface: str | None = None):
        """The port to watch as a second bus on the next connect (``can.second`` = "watch"), or None."""
        return self.other_port(iface) if self.settings.get("can.second") == "watch" else None

    def port_label(self, iface: str | None = None) -> str:
        iface = iface or self.settings.get("can.iface")
        return next((p.label for p in self.can_ports() if p.iface == iface), "CAN")

    def swap_ports(self):
        """Make the other port the main bus (and this one the second): the two swap bitrates too."""
        other = self.other_port()
        if other is None:
            self.toast("No other CAN port to switch to")
            return
        s = self.settings
        was_connected = self.connection is not None
        if was_connected:
            self.disconnect()
        main_br, second_br = int(s.get("can.bitrate") or 0), int(s.get("can.second.bitrate") or 0)
        s.set("can.iface", other.iface)
        s.set("can.bitrate", second_br)
        s.set("can.second.bitrate", main_br)
        s.set("can.main_auto", False)       # chosen by hand: Connect doesn't pick the busier one any more
        self.toast(f"{other.label} is now the main bus (set by hand)")
        if was_connected:
            self.connect()

    def _find_busier(self, name: str, other):
        """Listen to both ports (listen-only, each at its bitrate, or Auto finds it) and make the
        busier one the main bus, then connect. The ports keep their own bitrate settings."""
        s = self.settings
        br_main, br_other = int(s.get("can.bitrate") or 0), int(s.get("can.second.bitrate") or 0)
        here = self.port_label(name)

        def work():
            return measure_port(name, br_main), measure_port(other.iface, br_other)

        def done(found):
            (got_a, rate_a, why_a), (got_b, rate_b, why_b) = found
            swap = busier(got_a, got_b)
            fps = lambda n: f"{n / BUSIER_LISTEN:.0f}"  # noqa: E731
            if swap:
                s.set("can.iface", other.iface)
                s.set("can.bitrate", br_other)
                s.set("can.second.bitrate", br_main)
                self.toast(f"{other.label} is the busier bus ({fps(got_b)} frames a second, {here} {fps(got_a)}): "
                           f"it's the main bus. CAN > Connect > Main bus sets it by hand")
            elif got_a or got_b:
                self.toast(f"{here} is the busier bus ({fps(got_a)} frames a second, {other.label} {fps(got_b)})")
            else:
                self.toast(f"No traffic on either port ({why_a or why_b or 'nothing heard'}). Key on?")
            self.connect(SOCKETCAN, _found=(rate_b, rate_a) if swap else (rate_a, rate_b))
        self.run_job(f"Listening to {here} and {other.label} for the busier bus", work, done)

    def can_iface_choices(self, names: list[str]) -> list[tuple[str, str]]:
        """(label, interface) for the interface list: on-board ports by their connector name,
        ports switched off left out."""
        ports = self.can_ports()
        off = set(self.settings.get("can.ports_off") or [])
        hidden = {p.iface for p in ports if p.id in off and p.iface}
        label = {p.iface: f"{p.label} ({p.iface})" for p in ports if p.iface}
        return [(label.get(n, n), n) for n in dict.fromkeys(names) if n and n not in hidden]

    def iio_ratio_changed(self):
        """The Pi ADC's divider setting changed: its old calibration no longer fits."""
        if self.calibration.reset_source("iio"):
            self.toast("ADC divider changed: calibrate its channels again (System > Meters > Calibration)")
        self.apply_meters()

    def _stop_pps(self):
        if self.pps is not None:
            self.pps.stop()
            self.pps = None

    def toast(self, text: str):
        self.toasts.append((text, time.time() + TOAST_SECS))

    def current_toast(self) -> str | None:
        now = time.time()
        while self.toasts and self.toasts[0][1] < now:
            self.toasts.popleft()
        return self.toasts[-1][0] if self.toasts else None

    # ------------------------------------------------------------------
    # background jobs
    # ------------------------------------------------------------------
    def run_job(self, label: str, fn, done=None) -> bool:
        """Run ``fn()`` on a worker thread; ``done(result)`` runs on the UI thread in ``tick``.
        One job at a time (the screens show ``busy`` and refuse a second)."""
        if self.busy:
            self.toast(f"Busy: {self.busy}")
            return False
        self.busy = label

        def work():
            try:
                self._jobs.put(("ok", done, fn()))
            except Exception as e:      # shown to the user; the app keeps going
                traceback.print_exc()
                self._jobs.put(("error", None, f"{label} failed: {e}"))

        threading.Thread(target=work, name="device-job", daemon=True).start()
        return True

    # ------------------------------------------------------------------
    # remote help (core.remote, device.remote_actions)
    # ------------------------------------------------------------------
    def post(self, fn):
        """Run ``fn()`` on the UI thread at the next tick (for worker threads)."""
        self._ui_calls.put(fn)

    def remote_start(self, minutes: float = 30.0) -> str:
        """Open a remote-help session. Returns '' or why it can't."""
        from ..core import remote, upload
        from . import remote_actions
        if self.remote is not None and self.remote.running:
            return ""
        if not upload.HttpUploader().configured(self.settings):
            return "No upload server is set up on this unit (it carries remote help too)."
        remote_actions.register_all()
        show = self.remote_show_ask
        ask = remote_actions.Asker(self.post, show) if show else None
        machine = self.capture.machine_summary() if self.capture.machine else ""
        self.remote = remote.RemoteLink(self.settings, label=machine or self.describe_connection(), minutes=minutes,
                                        confirm=ask, info=lambda: {"front_end": "device",
                                                                   "connection": self.describe_connection()},
                                        context={"ctx": self, "app": self.remote_app})
        self.remote.start()
        return ""

    def remote_stop(self):
        if self.remote is not None:
            self.remote.stop()
        self.tunnel_stop()

    def tunnel_start(self) -> str:
        """Open the SSH tunnel for hands-on help (``core.tunnel``). '' or why it can't."""
        from ..core import tunnel
        if not upload.HttpUploader().configured(self.settings):
            return "No upload server is set up on this unit (the tunnel goes to it)."
        if self.tunnel is None or not (self.tunnel.running or self.tunnel.state == "connecting"):
            self.tunnel = tunnel.Tunnel(self.settings)
            self.tunnel.start()
        return ""

    def tunnel_stop(self):
        if self.tunnel is not None:
            self.tunnel.stop()

    def _drain_jobs(self):
        while True:
            try:
                fn = self._ui_calls.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:           # a remote question must not take the instrument down
                traceback.print_exc()
        while True:
            try:
                kind, done, payload = self._jobs.get_nowait()
            except queue.Empty:
                return
            if kind in ("ok", "error"):
                self.busy = None
            if kind == "error":
                self.toast(payload)
                self.cue("error")
            elif kind == "finder":
                self._finder_event(payload)
            elif done is not None:
                done(payload)

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """For tests: tick until the running job finishes."""
        end = time.time() + timeout
        while time.time() < end:
            self.tick()
            if not self.busy:
                return True
            time.sleep(0.02)
        return False

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------
    def connect(self, kind: str | None = None, _found: tuple | None = None):
        """Connect with the settings. With two on-board ports in use and the main bus on Auto
        (``can.main_auto``), both are listened to first and the busier one becomes the main bus
        (``_find_busier``, which comes back here with the bitrates it found)."""
        if self.busy:
            return
        kind = kind or self.settings.get("can.source")
        name = self.settings.get("can.iface") or "can0"
        bitrate = int(self.settings.get("can.bitrate") or 0)
        listen_only = bool(self.settings.get("can.listen_only"))
        python_can = kind == SOCKETCAN and pythoncan.is_spec(name)
        if python_can and not bitrate:
            self.toast("Pick a bitrate for a python-can adapter (Auto works with can0-style interfaces)")
            return
        if kind == SOCKETCAN and _found is None and not python_can and self.settings.get("can.main_auto") \
                and not name.startswith("vcan"):
            other = self.other_port(name)
            if other is not None and other.iface:
                self._find_busier(name, other)
                return
        if _found is not None and not bitrate:
            bitrate = _found[0]                 # what the listening found (the setting stays Auto)
        if kind == SOCKETCAN and not bitrate and not name.startswith("vcan"):
            info = next((i for i in interfaces.list_interfaces(details=False) if i.name == name), None)
            if info is None or info.state != "up":
                self.start_finder(connect_after=True)       # Auto: let the finder pick the bitrate
                return
        elm_j1939 = (self.settings.get("elm.protocol") or "0").upper() == "A"
        elm_addr = self.settings.get("elm.adapter") if kind == ELM else (
            "sim:j1939" if elm_j1939 else f"sim:{self.settings.get('elm.sim_vehicle') or '11'}")
        if kind == ELM and not elm_addr:
            self.toast("Pick a Bluetooth OBD adapter first: Bluetooth adapters, on this page")
            return
        self.disconnect()
        if (kind == OBD_SIM or (kind in (ELM, ELM_SIM) and not elm_j1939)) and self.protocol != profiles.OBD2:
            self.apply_profile("obd2")
        elif kind in (ELM, ELM_SIM) and elm_j1939 and not profiles.speaks_j1939():
            self.apply_profile("generic")
        elif (kind == SIMULATOR and not profiles.speaks_j1939()) or \
                (kind == PROFILE_SIM and self.protocol == profiles.OBD2):
            self.apply_profile("generic")
        elif kind == CANOPEN_SIM and not profiles.speaks_canopen():
            self.apply_profile("sim-canopen")

        watch = self.second_to_watch(name) if kind == SOCKETCAN else None
        br2 = int(self.settings.get("can.second.bitrate") or 0) or (_found[1] if _found else 0)

        def open_second(conn):
            """The other port, listen-only at its own bitrate. A failure here never costs the main bus."""
            try:
                rate = br2
                if not rate:
                    rate, log = interfaces.detect_bitrate(watch.iface)
                    if not rate:
                        raise OSError("no traffic at any bitrate" + (f" ({log[-1]})" if log else ""))
                else:
                    ok, msg = interfaces.bring_up(watch.iface, rate, True)
                    if not ok:
                        raise OSError(msg)
                conn.second = (open_connection(SOCKETCAN, watch.iface, listen_only=True, bitrate=rate), watch, rate)
            except Exception as e:          # noqa: BLE001 - reported, and the main connection carries on
                conn.second_error = f"{watch.label} not watched: {e}"
            return conn

        def work():
            conn = work_main()
            return open_second(conn) if watch is not None else conn

        def work_main():
            if kind in (ELM, ELM_SIM):
                return open_connection(kind, elm_addr, elm_protocol=self.settings.get("elm.protocol") or "0",
                                       listen_only=listen_only,
                                       elm_bus=(self.settings.get("elm.bus") or "") if kind == ELM else "")
            if kind == SOCKETCAN and bitrate and not name.startswith("vcan") and not python_can:
                info = next((i for i in interfaces.list_interfaces() if i.name == name), None)
                if info is None:
                    raise OSError(interfaces.no_adapter_hint().split("\n")[0])
                if interfaces.needs_setup(info, bitrate, listen_only):     # down, or left set differently
                    ok, msg = interfaces.bring_up(name, bitrate, listen_only)
                    if not ok:
                        raise OSError(f"couldn't bring {name} up: {msg}")
            return open_connection(kind, name, listen_only=listen_only, profile=profiles.active(), bitrate=bitrate)

        self.run_job(f"Connecting to {self.bt_label()}" if kind == ELM else "Connecting", work, self._connected)

    def _connected(self, conn):
        self.connection = conn
        self.session = conn.session
        self.result = None
        self.obd_codes = None
        self.j1939_live.clear()
        self.obd_live.clear()
        self._bus_level, self._bus_was_ok = link.OFF, False
        self._traffic_checked, self.traffic_hint = False, ""
        self.fieldwatch.clear()
        conn.session.add_listener(self.fieldwatch.feed)
        self.profile_matches, self._matches_checked = [], False
        self.roster, self.module_alerts = None, roster.Alerts()
        got = getattr(conn, "second", None)
        if got is not None:
            self.second, self.second_port, self.second_bitrate = got
        elif getattr(conn, "second_error", ""):
            self.toast(conn.second_error)
        if self.protocol == profiles.OBD2:
            from ..core.obd import ObdClient
            from ..core.obd_stream import ObdStreamer
            self.obd_client = ObdClient(conn.session)
            self.obd_streamer = ObdStreamer(self.obd_client)
        if conn.kind in (ELM, ELM_SIM):             # the battery voltage at the OBD port, as a meter channel
            self.hub.add_source(meters.ObdVoltageSource(conn.session.backend))
        self.toast(f"Connected: {conn.describe()}" + ("  (listen-only)" if conn.session.listen_only else ""))

    def disconnect(self):
        if self.capture.state == cap.RECORDING and self.connection is not None:
            self.stop_log()                 # the log can't keep a bus that's gone
        if self.obd_streamer is not None:
            self.obd_streamer.stop()
            self.obd_streamer = None
        if self.obd_client is not None:
            self.obd_client.close()
            self.obd_client = None
        self.hub.remove_source(meters.ObdVoltageSource.id)
        if self.second is not None:
            self.second.close()
        self.second, self.second_port = None, None
        self.bus2 = link.Status(link.OFF, "Bus: not connected")
        self.traffic_hint = ""
        if self.connection is not None:
            self.connection.session.remove_listener(self.fieldwatch.feed)
            self.connection.close()
            self.toast("Disconnected")
        self.connection = None
        self.session = None

    def sources(self) -> list[tuple[str, str]]:
        """(kind, label) choices for Source: the simulators only when they're switched on."""
        show = self.settings.get("show_simulators")
        return [(k, lb) for k, lb in SOURCES if show or k not in SIMULATORS]

    def set_show_simulators(self, on: bool):
        self.settings.set("show_simulators", on)
        if not on and self.settings.get("can.source") in SIMULATORS:
            self.settings.set("can.source", SOCKETCAN)     # (an open connection stays as it is)

    def find_python_can(self):
        """Look for adapters python-can can open; they join the Interface choices."""
        if not pythoncan.available():
            self.toast(pythoncan.INSTALL_HINT)
            return

        def done(found):
            self.python_can_found = found
            self.toast(("Found " + ", ".join(found)) if found else "python-can found no adapters")
        self.run_job("Looking for adapters", pythoncan.detect, done)

    # --- Bluetooth OBD adapters ---
    def bt_label(self, address: str | None = None) -> str:
        addr = (address if address is not None else self.settings.get("elm.adapter")) or ""
        paired = next((d.name for d in self.bt_paired if d.address == addr.upper()), "")
        return bt.label(self.settings, addr, paired) if addr else "none picked"

    def bt_choices(self) -> list[tuple[str, str]]:
        """(address, label) for the Adapter picker."""
        return bt.adapter_choices(self.settings, self.bt_paired)

    def bt_refresh(self, then=None):
        def load():
            state = bt.controller()
            return state, (bt.paired_devices() if state.powered else [])

        def done(result):
            self.bt_state, self.bt_paired = result
            if then:
                then()
        self.run_job("Checking Bluetooth", load, done)

    def bt_power_on(self):
        self.run_job("Turning Bluetooth on", bt.power_on, lambda ok: self.bt_refresh())

    def bt_scan(self):
        def done(found):
            paired = {d.address for d in self.bt_paired}
            self.bt_found = [d for d in found if d.address not in paired]
            self.bt_scanned = True
            self.toast(f"Found {len(self.bt_found)} device(s)" if self.bt_found
                       else "Nothing found. Adapter plugged in, key on?")
        self.run_job(f"Scanning ({bt.SCAN_SECONDS} s)", bt.scan, done)

    def bt_use(self, address: str, name: str = ""):
        if address.upper() not in bt.names(self.settings):
            bt.set_name(self.settings, address, name or self.bt_label(address))
        self.settings.set("elm.adapter", address.upper())
        self.settings.set("can.source", ELM)

    def bt_pair(self, address: str, pin: str):
        name = next((d.name for d in self.bt_found if d.address == address), "") or address

        def done(msg):
            self.bt_found = [d for d in self.bt_found if d.address != address]
            self.bt_use(address, name)
            self.toast(f"{msg}: using {self.bt_label(address)}")
            self.bt_refresh()
        self.run_job(f"Pairing with {name}", lambda: bt.pair(address, pin), done)

    def bt_rename(self, address: str, name: str):
        bt.set_name(self.settings, address, name)

    def bt_forget(self, address: str):
        def done(_):
            bt.forget_name(self.settings, address)
            self.toast("Forgotten")
            self.bt_refresh()
        self.run_job("Forgetting", lambda: bt.remove(address), done)

    def bt_test(self, address: str, then):
        """Connect, ask the adapter what it can do (``can.elm_probe``, the quick part), disconnect.
        ``then(lines)`` on the UI thread."""
        if self.connection is not None and self.connection.kind == ELM and \
                self.connection.session.backend.channel.upper() == address.upper():
            self.toast("Disconnect first: it's connected now")
            return

        def test():
            from ..can import elm_probe
            from ..can.elm327 import ElmBackend
            b = ElmBackend(address, protocol=self.settings.get("elm.protocol") or "0")
            b.open()
            try:
                return [f"{it.name}: {it.status}" for it in elm_probe.probe(b)]
            finally:
                b.close()
        self.run_job(f"Testing {self.bt_label(address)}", test, then)

    def describe_connection(self) -> str:
        if self.connection is None:
            return "Not connected"
        return self.connection.describe() + ("  [listen-only]" if self.session.listen_only else "")

    # --- signal finder ---
    @property
    def finding(self) -> bool:
        return self.finder is not None

    def start_finder(self, connect_after: bool = True) -> bool:
        if self.finder is not None:
            return True
        name = self.settings.get("can.iface") or "can0"
        if pythoncan.is_spec(name):
            self.toast("Find signal works with can0-style interfaces. For a python-can adapter, pick the "
                       "bitrate and Connect.")
            return False
        if not interfaces.valid_name(name):
            self.toast(f"Bad interface name {name!r}")
            return False
        self.disconnect()
        choice = int(self.settings.get("can.bitrate") or 0)
        last = int(self.settings.get("can.last_bitrate", 0) or 0) or None
        order = link.bitrate_order(last or interfaces.BITRATES[0]) if not choice else [choice]
        self.finder_log.clear()
        self._connect_after_find = connect_after
        self.finder = link.SignalFinder(name, order, lambda ev: self._jobs.put(("finder", None, ev)),
                                        listen_only=bool(self.settings.get("can.listen_only")))
        self.finder.start()
        self.finder_log.append("Listening, listen-only: move the probes until it chimes")
        return True

    def stop_finder(self):
        f, self.finder = self.finder, None
        if f is not None:
            f.stop()

    def _finder_event(self, ev):
        if self.finder is None:
            return
        self.finder_log.append(ev.text)
        if ev.state == "locked":
            self.finder.stop()
            self.finder = None
            if ev.bitrate:
                self.settings.set("can.last_bitrate", ev.bitrate)
            self.cue("found")
            if self._connect_after_find:
                self.settings.set("can.source", SOCKETCAN)
                self.connect(SOCKETCAN)     # the finder left the interface up at the bitrate it found
        elif ev.state == "failed":
            self.finder.stop()
            self.finder = None
            self.toast(ev.text)

    def cue(self, event: str, link_status: bool = False) -> bool:
        """Play the tone System > Sound picked for ``event`` (core.sound.EVENTS), on the speaker
        (``self.speaker`` if the UI has a quicker way than a player command) and the buzzer."""
        s = self.settings
        if link_status and not s.get("sound.link_cues"):
            return False
        if event == "touch" and not s.get("sound.touch"):
            return False
        tone = sound.tone_for(event, s.get("sound.tones"))
        return False if tone is None else self.play_tone(tone)

    def play_tone(self, tone: str) -> bool:
        """One tone at the set volume, whatever the events are set to (System > Sound previews)."""
        s = self.settings
        if tone not in sound.TONES:
            return False
        try:
            volume = max(0, min(100, int(s.get("sound.volume")))) / 100
        except (TypeError, ValueError):
            volume = 0.8
        started = False
        if s.get("sound.speaker") and volume > 0:
            started = self.speaker(tone, volume) if self.speaker is not None else sound.play_speaker(tone, volume)
        return sound.play_buzzer(tone, str(s.get("sound.buzzer") or sound.AUTO)) or started

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def read_codes(self, preset: str = "active") -> bool:
        if not self.live:
            self.toast("Connect first")
            return False
        if self.protocol == profiles.OBD2:
            if self.obd_client is None or not self.obd_client.available:
                self.toast("Reading codes needs a connection that can transmit (not listen-only)")
                return False
            client = self.obd_client

            def read():
                by_ecu = client.read_dtcs_by_ecu()
                return by_ecu, (client.thin_ecus() if by_ecu else [])
            return self.run_job("Reading codes", read, self._obd_codes_read)
        session = self.session
        return self.run_job(scan.PRESETS[preset][0], lambda: scan.run_preset(session, preset), self._scanned)

    def _scanned(self, result):
        self.result = result
        if result.tx_error and not result.code_count:
            self.toast(f"Couldn't send the requests: {result.tx_error}")
            return
        extra = " (listen-only: broadcast codes only)" if result.listen_only else ""
        self.toast(f"{result.title}: {result.code_count} code(s){extra}")
        if result.code_count:
            self.cue("codes")

    def _obd_codes_read(self, value):
        from ..core.obd import THIN_SHORT
        by_ecu, self.obd_thin = value
        self.obd_codes = by_ecu
        n = sum(len(c) for kinds in by_ecu.values() for c in kinds.values())
        if not by_ecu:
            self.toast("No module answered the code read" + (f": {self.session.health.tx_error}"
                                                             if self.session.health.tx_error else ""))
        elif self.obd_thin and not n:
            self.toast(f"{len(by_ecu)} module(s) answered, no codes. {THIN_SHORT} (More...)")
        else:
            self.toast(f"{len(by_ecu)} module(s) answered, {n} code(s)")
        if n:
            self.cue("codes")

    # --- clearing codes (changes the vehicle: screens confirm first, default No) ---
    def clear_j1939(self, which: str, da: int, then=None) -> bool:
        """DM11 / DM3 (``core.j1939_clear``), then read the codes back. ``then(lines, ok)`` gets
        the outcome on the UI thread."""
        from ..core import j1939_clear
        if not self.can_transmit:
            self.toast("Clearing codes has to transmit: connect without listen-only")
            return False
        session = self.session
        pgns = {j1939_clear.ACTIVE: scan.ACTIVE, j1939_clear.STORED: scan.STORED}.get(which,
                                                                                     scan.ACTIVE + scan.STORED)

        def work():
            result = j1939_clear.clear_codes(session, which, da)
            time.sleep(1.2)                 # the next DM1 broadcast shows what's still active
            return result, scan.run_scan(session, pgns, title="Codes after clearing")

        def done(value):
            result, after = value
            self.result = after
            if then:
                then(result.lines(), result.ok)
        return self.run_job("Clearing codes", work, done)

    def clear_obd(self, then=None) -> bool:
        """OBD-II Mode 04 on every module, then read the codes back."""
        client = self.obd_client
        if client is None or not client.available:
            self.toast("Clearing codes has to transmit: connect to the car without listen-only")
            return False

        def work():
            result = client.clear_dtcs()
            time.sleep(0.5)                 # let the modules settle before reading back
            return result, client.read_dtcs_by_ecu()

        def done(value):
            result, by_ecu = value
            self.obd_codes = by_ecu
            lines = [f"{client.ecu_label(ecu)}: {msg}" for ecu, (_, msg) in result.items()]
            if then:
                then(lines or ["No module answered the clear request."], any(ok for ok, _ in result.values()))
        return self.run_job("Clearing codes", work, done)

    def kline_probe_refusal(self) -> str:
        """'' if the K-line deep probe can run on this connection, else why not."""
        from ..can import kline_probe
        if not self.live or self.session is None:
            return "Connect first"
        if not self.can_transmit:
            return "The deep probe has to ask questions: connect without listen-only"
        return kline_probe.can_probe(self.session.backend)

    def kline_probe(self, then=None, full: bool = False) -> bool:
        """The K-line deep probe (``can.kline_probe``: read-only), recorded into a log so it can be
        uploaded: a log is started for it if none is running, then stopped and saved.
        ``then(title, lines)`` gets the summary on the UI thread."""
        from ..can import kline_probe

        def work(backend, folder, progress):
            report = kline_probe.run(backend, progress, full=full)
            if folder:
                with open(os.path.join(folder, kline_probe.REPORT_FILE), "a") as f:
                    f.write("\n".join(report.lines()) + "\n\n")      # a second run in one log is added below
                data = report.to_dict()
                if data["plan"] or not os.path.exists(os.path.join(folder, kline_probe.REPORT_JSON)):
                    with open(os.path.join(folder, kline_probe.REPORT_JSON), "w") as f:
                        json.dump(data, f, indent=1)
            return report.summary()
        return self._kline_job("Deep probe", work, then)

    def kline_plan(self):
        """What the newest saved deep probe with a full scan found to read (``kline_probe.Plan``), or None."""
        from ..can import kline_probe
        root = config.logs_dir()
        try:
            names = sorted(os.listdir(root), reverse=True)
        except OSError:
            return None
        for name in names:
            plan = None
            try:
                with open(os.path.join(root, name, kline_probe.REPORT_JSON)) as f:
                    plan = kline_probe.plan_from_dict(json.load(f))
            except (OSError, ValueError, KeyError, TypeError):
                pass
            try:
                if plan is None:
                    with open(os.path.join(root, name, kline_probe.REPORT_FILE)) as f:
                        plan = kline_probe.plan_from_report(f.read())
            except OSError:
                continue
            if plan is not None:
                return plan
        return None

    def kline_values(self, then=None) -> bool:
        """Read again what the last full scan found, twice over, into ``kline_values.csv`` in a log."""
        from ..can import kline_probe
        plan = self.kline_plan()
        if plan is None:
            self.toast("No saved deep probe with a full scan on this unit yet")
            return False

        def work(backend, folder, progress):
            rows, summary = kline_probe.record_values(backend, plan, progress=progress)
            if folder:
                path = os.path.join(folder, kline_probe.VALUES_FILE)
                new = not os.path.exists(path)
                with open(path, "a") as f:
                    f.write("".join(line + "\n" for line in ([kline_probe.VALUES_HEADER] if new else []) + rows))
            return summary
        return self._kline_job("Reading values", work, then)

    def _kline_job(self, label: str, work, then=None) -> bool:
        """Run ``work(backend, capture folder, progress) -> summary lines`` holding the adapter, inside
        a log (started, stopped and saved for it if none is recording)."""
        why = self.kline_probe_refusal()
        if why:
            self.toast(why)
            return False
        if self.busy:
            self.toast(f"Busy: {self.busy}")
            return False
        own_log = not self.recording
        if own_log and not (self.start_log(stream=False) and self.recording):
            self.toast("The last log is being saved first: try again in a moment")
            return False
        if self.obd_streamer is not None and self.obd_streamer.running:
            self.obd_streamer.stop()            # the job holds the adapter: live data would only queue up
        backend, capture = self.session.backend, self.capture

        def progress(text):
            self.busy = f"{label}: {text}"

        def done(lines):
            lines = list(lines)
            if own_log and self.capture is capture and self.recording:
                self.capture.stop()
                lines += ["", "Saved as a log: upload it from the Log page."]
                self.save_log()
            if then:
                then(label, lines)
        return self.run_job(label, lambda: work(backend, capture.folder, progress), done)

    def obd_label(self, ecu: int) -> str:
        if self.obd_client is not None:
            return self.obd_client.ecu_label(ecu)
        return self.obd_live.source_label(ecu)

    def toggle_obd_stream(self):
        st = self.obd_streamer
        if st is None or not self.obd_client.available:
            self.toast("Streaming needs an OBD-II connection that can transmit")
            return
        if st.running:
            st.stop()
            return
        model = self.obd_live
        if model.order:
            st.start(self._obd_poll_list())
            return
        client = self.obd_client

        def found(pids):
            st.start(pids)
        self.run_job("Finding live values", client.live_pids, found)

    def _obd_poll_list(self) -> list[tuple[int, int]]:
        m = self.obd_live
        picked = [(m.rows[k].sa, m.rows[k].sig.pid) for k in m.selected if k in m.rows]
        return list(dict.fromkeys(picked)) or m.polled()

    # ------------------------------------------------------------------
    # the data log
    # ------------------------------------------------------------------
    @property
    def recording(self) -> bool:
        return self.capture.state == cap.RECORDING

    def capture_meta(self) -> dict:
        meta = {"source": self.connection.describe() if self.connection else "meters only",
                "profile": self.settings.get("profile") or "none", "units": self.units,
                "front_end": f"device {__version__}"}
        if self.connection is not None and self.connection.kind == SOCKETCAN:
            iface, bitrate = self.settings.get("can.iface"), int(self.settings.get("can.bitrate") or 0)
            if not bitrate:                             # Auto: say what it found, not 0
                found = interfaces.current_bitrate(iface)
                meta["bitrate_auto"] = True
                bitrate = found or 0
            meta.update(interface=iface, bitrate=bitrate)
            if self.can_ports():
                meta["port"] = self.port_label()
        if self.second is not None:
            meta["second"] = {"port": self.second_port.label, "interface": self.second_port.iface,
                              "bitrate": self.second_bitrate, "listen_only": True}
        if self.session is not None:
            meta["listen_only"] = self.session.listen_only
        cal = self.calibration.snapshot()
        if cal:
            meta["calibration"] = cal       # the correction the readings were taken with
        return meta

    def toggle_log(self):
        self.stop_log() if self.recording else self.start_log()

    def start_log(self, stream: bool = True) -> bool:
        """Start recording. ``stream=False`` doesn't start OBD-II live data with it (the caller is
        about to hold the adapter itself)."""
        if self.recording:
            return True
        has_meters = bool(self.hub.sources())
        if not self.live and not has_meters and self.serial is None:
            self.toast("Nothing to record: connect to a bus or switch on a meter")
            return False
        c = self.capture
        if c.state == cap.STOPPED:          # the last one isn't saved yet: keep it, don't ask mid-job
            self.save_log(then_start=True)
            return True
        if c.state == cap.SAVED:
            self.capture = c = c.fresh_copy()
        try:
            c.start(self.session if self.live else None, config.logs_dir(), self.capture_meta(),
                    measurements=self.hub if has_meters else None,
                    second=self.second.session if self.second is not None and self.live else None,
                    serial=self.serial)
        except (OSError, ValueError) as e:
            self.toast(f"Can't start the log: {e}")
            return False
        self.settings.set("log.plan", c.plan_labels())
        if stream and self.protocol == profiles.OBD2 and self.obd_streamer is not None \
                and not self.obd_streamer.running:
            self.toggle_obd_stream()        # a car only talks when asked: keep it talking while logging
        what = "meters only" if has_meters else "serial lines only"
        self.toast("Recording" if self.live else f"Recording {what}: not connected to a bus, so no CAN data")
        self.cue("rec_start")
        return True

    @property
    def battery(self) -> "batt.Battery | None":
        """The handheld's pack (button ADC input A0), or None when nothing reads it."""
        return self.battery_monitor.battery

    def battery_reading(self, adc_v: float | None, now: float | None = None) -> str | None:
        """One reading of the battery input, in ADC volts (from the UI's button thread). Warns once
        per step down; returns the event ("low", "very low", "empty") so the UI can power off at
        "empty"."""
        m, s = self.battery_monitor, self.settings
        m.divider = float(s.get("battery.divider") or m.divider)
        m.cells = int(s.get("battery.cells") or m.cells)
        m.power_off = bool(s.get("battery.power_off"))
        ev = m.update(adc_v, time.monotonic() if now is None else now)
        if ev:
            self.toast(m.message(ev))
            self.cue("battery")
        return ev

    def toggle_hold(self):
        """Freeze the live values on the screen (Live, the grid's traces), or let them go again.
        Only the screen stops: the bus, the meters and a running log carry on."""
        self.held = not self.held
        self.toast("Hold: values frozen on the screen (still recording)" if self.held and self.recording
                   else "Hold: values frozen on the screen" if self.held else "Hold off: live again")

    def stop_log(self):
        if not self.recording:
            return
        self.capture.stop()
        self.toast("Stopped: Save keeps it, or tag / rename first")
        self.cue("rec_stop")

    def tag(self):
        c = self.capture
        if not self.recording:
            if c.state == cap.IDLE:
                c.add_planned()
                self.settings.set("log.plan", c.plan_labels())
                self.toast(f"Planned {c.title(len(c.markers) - 1)}")
            else:
                self.toast("Not recording")
            return
        i = c.tag_next()
        self.cue("tag")
        self.toast(f"Tagged {c.title(i)} at {cap.fmt_elapsed(c.markers[i].t - c.started)}")

    def undo_tag(self):
        if self.recording and self.capture.undo_last() is not None:
            self.toast("Last timestamp taken back")

    def set_plan(self, labels: list[str]):
        if self.capture.state != cap.IDLE:
            self.capture = Capture(plan=labels)
        else:
            self.capture.markers = [cap.Marker(lb) for lb in labels]
        self.settings.set("log.plan", labels)

    def save_log(self, then_start: bool = False):
        c = self.capture
        if c.state not in (cap.STOPPED, cap.SAVED):
            return
        system = self.units

        def done(folder):
            self.last_saved = folder
            cap.remember_machine(c.machine)
            queued = upload.after_save(self.uploads, self.settings, folder)
            self.toast(f"Saved {os.path.basename(folder)}" + (", queued for upload" if queued else ""))
            if then_start:
                self.start_log()
        self.run_job("Saving the log", lambda: c.save(system=system), done)

    # --- machine details and uploads ---
    def machine_options(self) -> list[tuple[str, tuple[str, dict]]]:
        """For the Machine picker: what the bus says (merged in), recent machines (replace), clear."""
        opts = [(f"From the bus: {c.label}", ("merge", c.fields))
                for c in identity.candidates(self.session, self.obd_live.sniffer.info)]
        opts += [(f"Recent: {cap.machine_summary(m)}", ("replace", m)) for m in cap.recent_machines()[:12]]
        if self.capture.machine:
            opts.append(("No machine details", ("replace", {})))
        return opts

    def set_machine(self, choice: tuple[str, dict]):
        how, fields = choice
        c = self.capture
        c.set_machine({**c.machine, **fields} if how == "merge" else fields)
        if c.machine:
            cap.remember_machine(c.machine)
        self.toast(f"Machine: {c.machine_summary() or 'none'}")

    def github_repos(self, done):
        """List the repositories the GitHub sign-in can reach (a job: it asks GitHub), then
        ``done(names)``. False when not signed in."""
        from ..core import github
        acct = github.load_account()
        if acct is None:
            return False
        return self.run_job("Asking GitHub for your repositories", lambda: github.repositories(acct.token), done)

    def upload_to_github(self, repo: str):
        """Send uploads to this private repository from now on, and upload the saved log."""
        self.settings.update({"upload.target": "github", "upload.github_repo": repo})
        self.toast(f"Uploads go to {repo}")
        if self.capture.state == cap.SAVED:
            self.upload_log()

    def start_uploads(self):
        self.uploads.start()

    def upload_status(self) -> upload.Status:
        """The saved log's upload state (re-read at most twice a second: screens ask every frame)."""
        c = self.capture
        if c.state != cap.SAVED:
            return upload.Status()
        now = time.monotonic()
        cached = getattr(self, "_upload_cache", None)
        if cached and cached[0] == c.folder and now - cached[1] < 0.5:
            return cached[2]
        st = self.uploads.status(c.folder)
        self._upload_cache = (c.folder, now, st)
        return st

    def upload_log(self):
        c = self.capture
        if c.state != cap.SAVED or not c.folder:
            self.toast("Save the log first")
            return
        if upload.target(self.settings) is None:
            self.toast("Nowhere to upload to yet: sign in to GitHub (System > Software > Plugins) and pick a "
                       "private repository, or set up an upload server (canopener-cli upload --setup)")
            return
        try:
            self.uploads.enqueue(c.folder)
        except upload.UploadError as e:
            self.toast(str(e))
            return
        self._upload_cache = None
        self.uploads.retry_now()
        self.toast("Queued for upload: it goes when there's a connection")

    def share_profile(self, path: str) -> bool:
        """Send a profile made on this unit to the upload server, when one is set up and profile
        sharing is on (``upload.profiles``, on by default). Runs on its own thread, not as a job:
        saving the log at the same moment (the builder does both) must not crowd it out. Says how
        it went either way. Quiet if there's nowhere to send it."""
        if not self.settings.get("upload.profiles") or not upload.HttpUploader().configured(self.settings):
            return False
        settings = self.settings
        name = os.path.splitext(os.path.basename(path))[0]

        def work():
            try:
                got = upload.upload_profile(settings, path)
                text = ("Profile shared with the upload server"
                        + ("" if got.get("unchanged") else f" (revision {got.get('revision')})"))
            except Exception as e:      # noqa: BLE001 - said on screen; the profile is saved here anyway
                traceback.print_exc()
                text = f"Profile saved here but not shared: {e}"
                self.share_failed.add(name)
            else:
                self.share_failed.discard(name)
            self.post(lambda: self.toast(text))
        threading.Thread(target=work, name="share-profile", daemon=True).start()
        return True

    def find_changes(self, then) -> bool:
        """Compare what the bus said between the saved log's timestamps (``core.changes``).
        ``then(report)`` on the UI thread. A log without timestamps is cut into four equal parts."""
        from ..core import changes
        c = self.capture
        if c.state != cap.SAVED or not c.folder:
            self.toast("Save the log first")
            return False
        folder, parts = c.folder, 0 if c.tagged() else 4
        return self.run_job("Comparing the timestamps", lambda: changes.analyze_capture(folder, parts=parts), then)

    def discard_log(self):
        c = self.capture
        if c.state in (cap.STOPPED, cap.RECORDING):
            plan = c.fresh_copy()
            c.discard()
            self.capture = plan
            self.toast("Log discarded")

    # ------------------------------------------------------------------
    # per frame
    # ------------------------------------------------------------------
    def tick(self, now: float | None = None):
        now = time.time() if now is None else now
        self._drain_jobs()
        if now - self._last_sample >= SAMPLE_EVERY and not self.held:
            self._last_sample = now
            s = self.session
            if s is not None:
                if self.protocol == profiles.OBD2:
                    self.obd_live.sample(s)
                    if self.obd_streamer is not None and self.obd_streamer.running and self.obd_live.selected:
                        self.obd_streamer.todo = self._obd_poll_list()
                else:
                    self.j1939_live.sample(s)
            self.meter_live.sample(self.hub)
        self._update_link(now)

    def _update_link(self, now: float):
        kind = self.connection.kind if self.connection is not None else self.settings.get("can.source")
        if now - self._link_checked >= LINK_CHECK_SECS:
            self._link_checked = now
            try:
                if kind == ELM:
                    conn = self.connection
                    backend = conn.session.backend if conn is not None and conn.kind == ELM else None
                    label = self.bt_label() if self.settings.get("elm.adapter") or backend else ""
                    self.adapter = link.adapter_status(kind, label, backend=backend)
                else:
                    self.adapter = link.adapter_status(kind, self.settings.get("can.iface") or "can0")
            except OSError:
                self.adapter = link.Status(link.OFF, "Adapter")
        s = self.session
        if self.finder is not None:
            self.bus = link.Status(link.WAIT, "Finding signal", "The signal finder is listening.")
            return
        if self.protocol == profiles.OBD2 and self.live:
            st = self.obd_streamer
            self.bus = link.bus_status(s, now, last_answer=self.obd_live.sniffer.last_answer_t,
                                       polling=bool(st and st.running))
        else:
            self.bus = link.bus_status(s, now, rate=self._rate.update(s, now))
        if self.second is not None:
            s2 = self.second.session
            self.bus2 = link.bus_status(s2, now, rate=self._rate2.update(s2, now))
        if not self.live:
            return
        self._traffic_check(now)
        self._match_check(now)
        self._module_check(now)
        # for the chimes, a bus with little traffic is still a bus that's there
        level = link.OK if self.bus.level == link.LOW else self.bus.level
        if level != link.OK and now - s.started_at < link.QUIET_SECS:
            return
        was, self._bus_level = self._bus_level, level
        if level == link.IDLE or was == link.IDLE:
            pass
        elif was == link.OK and level != link.OK:
            self.cue("lost", link_status=True)
        elif was in (link.WAIT, link.BAD) and level == link.OK:
            self.cue("back" if self._bus_was_ok else "found", link_status=True)
        if level == link.OK:
            self._bus_was_ok = True

    def _traffic_check(self, now: float):
        """Once, a few seconds into a connection: say so if this bus has little or no traffic, and
        what to try (the other port, when the unit has two)."""
        s = self.session
        if self._traffic_checked or self.protocol == profiles.OBD2 or now - s.started_at < TRAFFIC_CHECK_SECS:
            return
        self._traffic_checked = True
        if self.bus.level == link.LOW:
            what = f"little traffic (about {self._rate.rate or 0:.0f} frames a second)"
        elif self.bus.level == link.WAIT:
            what = "no traffic"
        else:
            return
        wired = self.connection.kind == SOCKETCAN
        iface = getattr(s.backend, "channel", None)
        here, other = (self.port_label(iface) if wired else "CAN"), (self.other_port(iface) if wired else None)
        if other is None:
            self.traffic_hint = "Key on? The right connector?"
        elif self.second is not None and self.bus2.level == link.OK:
            self.traffic_hint = (f"{other.label} has the traffic (about {self._rate2.rate or 0:.0f} frames a "
                                 f"second): CAN > Connect > Make {other.label} the main bus")
        else:
            self.traffic_hint = f"Key on? Or try the other port: CAN > Connect > Make {other.label} the main bus"
        self.toast(f"{here}: {what}. {self.traffic_hint}")

    def _module_check(self, now: float):
        """About once a second: the missing-module check (``core.roster``) for the Bus and Codes pages,
        and a toast (with the 'module' sound) when a module drops out, is missing or comes back."""
        if now - self._roster_t < 1.0:
            return
        self._roster_t = now
        if self.protocol == profiles.OBD2:
            self.roster = None
            return
        self.roster = roster.check(self.session, now=now)
        said, bad = self.module_alerts.update(self.roster)
        if said:
            self.toast("; ".join(said))
            if bad:
                self.cue("module")

    def remember_modules(self) -> str:
        """Save the modules heard as this machine's (``roster.remember``) and use that profile.
        Returns what to tell the technician."""
        pid, found = roster.remember(self.session, profiles.active())
        self.apply_profile(pid)
        return f"Saved {len(found)} module(s) as this machine's ({self.profile_name()})"

    def _match_check(self, now: float):
        """Once, a few seconds into a connection: which known profiles this bus looks like. Said out
        loud when one fits clearly better than the profile in use."""
        s = self.session
        if self._matches_checked or now - s.started_at < TRAFFIC_CHECK_SECS + 2:
            return
        self._matches_checked = True
        best = self.check_profiles()
        if best is not None and best.share >= profile_match.GOOD_SHARE:
            self.toast(f"This looks like {best.profile.name} ({best.matched} of {best.total} of its messages here): "
                       "Profile builder > Load it")

    def check_profiles(self):
        """Rank the known profiles against the bus now (``profile_matches``); the best one that
        fits better than the profile in use, or None."""
        if self.session is None:
            return None
        found = profiles.arrange(profiles.available_profiles()[0])
        self.profile_matches = profile_match.similar(found, profiles.present_pgns(self.session))
        return profile_match.better_than_active(self.profile_matches, profiles.active())

    def frames_per_second(self) -> float:
        s = self.session
        if s is None:
            return 0.0
        now = time.time()
        prev = getattr(self, "_fps_mark", None)
        if prev is None or now - prev[0] >= 1.0:
            if prev is not None:
                self._fps = (s.rx_count - prev[1]) / (now - prev[0])
            self._fps_mark = (now, s.rx_count)
        return getattr(self, "_fps", 0.0)

    @property
    def unsaved(self) -> bool:
        """A log is recording or stopped but not saved (closing will save it)."""
        return self.capture.state in (cap.RECORDING, cap.STOPPED)

    def shutdown(self, save: bool = True):
        """Leave things tidy: stop the finder, stop a recording and save it (so switching UIs or
        powering off never leaves an unsaved log behind), close the bus. If saving fails the log
        stays a pending capture, which either UI offers to recover on its next start."""
        self.stop_finder()
        self.remote_stop()
        if self.recording:
            self.capture.stop()
        if save and self.capture.state == cap.STOPPED:
            try:
                self.last_saved = self.capture.save(system=self.units)
            except Exception:
                traceback.print_exc()
        self.disconnect()
        self.hub.stop()
        self.uploads.stop()
        self._stop_pps()

    def pending_captures(self) -> list[str]:
        return cap.pending_captures()
