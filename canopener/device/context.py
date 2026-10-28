"""Everything the handheld's screens share, with no Pygame in it: settings, the CAN connection,
the live-data models, the meters, the capture (data log), background jobs and status messages.

Screens call methods here and read attributes each frame; nothing here draws. Slow work (scans,
connecting, saving a capture) runs on a worker thread and its result comes back through
``tick()``, which the app calls once per frame on the UI thread, the same polling model as the
desktop GUI (docs/ARCHITECTURE.md, "Threading model"). Tests drive this class directly.
"""

import os
import queue
import threading
import time
import traceback
from collections import deque

from .. import __version__
from ..can import pythoncan
from ..core import capture as cap
from ..core import config, interfaces, link, meters, profiles, scan, sound, units
from ..core.capture import Capture
from ..core.connection import CANOPEN_SIM, OBD_SIM, PROFILE_SIM, SIMULATOR, SOCKETCAN, open_connection
from ..core.live import LiveData, ObdLiveData
from ..core.measure import MeasurementHub, MeasurementLiveData
from ..core.settings import Settings

SOURCES = [(SOCKETCAN, "CAN adapter"), (SIMULATOR, "Simulator (J1939 machine)"),
           (OBD_SIM, "Simulator (OBD-II car)"), (CANOPEN_SIM, "Simulator (CANopen truck)"),
           (PROFILE_SIM, "Simulate the profile")]
SIMULATORS = (SIMULATOR, OBD_SIM, CANOPEN_SIM, PROFILE_SIM)     # listed only with System > Show simulators
BITRATES = [0] + interfaces.BITRATES        # 0 = auto (signal finder)
SAMPLE_EVERY = 0.2                          # live models: 5 samples a second, like the desktop
LINK_CHECK_SECS = 1.0
TOAST_SECS = 3.5


def bitrate_label(b: int) -> str:
    return "Auto (find signal)" if not b else f"{b // 1000}k"


def source_label(kind: str) -> str:
    return dict(SOURCES).get(kind, kind)


class DeviceContext:
    def __init__(self, settings: Settings | None = None, *, start_meters: bool = True):
        self.settings = settings or Settings()
        config.set_data_dir(self.settings.get("data_dir") or None)
        units.set_custom(self.settings.get("units.custom") or {})
        self.connection = None
        self.session = None
        self.j1939_live = LiveData()
        self.obd_live = ObdLiveData()
        self.meter_live = MeasurementLiveData()
        self.hub = MeasurementHub()
        self.result = None                  # last J1939 ScanResult
        self.obd_codes: dict | None = None  # last OBD-II read: module -> {stored/pending/permanent: [codes]}
        self.obd_client = None
        self.obd_streamer = None
        self.capture = Capture(plan=list(self.settings.get("log.plan") or []))
        self.last_saved: str | None = None
        self.busy: str | None = None        # what a running job is doing, for a spinner
        self.toasts: deque = deque(maxlen=4)
        self.finder = None
        self.finder_log: deque = deque(maxlen=8)
        self.adapter = link.Status(link.OFF, "Adapter")
        self.bus = link.Status(link.OFF, "Bus: not connected")
        self.focus_spn: int | None = None   # a fault code's SPN to show in Live Data
        self.focus_pids: list[int] = []     # the OBD-II equivalent
        self.python_can_found: list[str] = []
        self._jobs: queue.Queue = queue.Queue()
        self._last_sample = 0.0
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

    def apply_meters(self):
        """(Re)start the measurement sources switched on in settings."""
        for src in self.hub.sources():
            self.hub.remove_source(src.id)
        for src in meters.sources_from_settings(self.settings):
            self.hub.add_source(src)
        self.meter_live.clear()

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

    def _drain_jobs(self):
        while True:
            try:
                kind, done, payload = self._jobs.get_nowait()
            except queue.Empty:
                return
            if kind in ("ok", "error"):
                self.busy = None
            if kind == "error":
                self.toast(payload)
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
    def connect(self, kind: str | None = None):
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
        if kind == SOCKETCAN and not bitrate and not name.startswith("vcan"):
            info = next((i for i in interfaces.list_interfaces(details=False) if i.name == name), None)
            if info is None or info.state != "up":
                self.start_finder(connect_after=True)       # Auto: let the finder pick the bitrate
                return
        self.disconnect()
        if kind == OBD_SIM and self.protocol != profiles.OBD2:
            self.apply_profile("obd2")
        elif (kind == SIMULATOR and not profiles.speaks_j1939()) or \
                (kind == PROFILE_SIM and self.protocol == profiles.OBD2):
            self.apply_profile("generic")
        elif kind == CANOPEN_SIM and not profiles.speaks_canopen():
            self.apply_profile("sim-canopen")

        def work():
            if kind == SOCKETCAN and bitrate and not name.startswith("vcan") and not python_can:
                info = next((i for i in interfaces.list_interfaces() if i.name == name), None)
                if info is None:
                    raise OSError(interfaces.no_adapter_hint().split("\n")[0])
                if interfaces.needs_setup(info, bitrate, listen_only):     # down, or left set differently
                    ok, msg = interfaces.bring_up(name, bitrate, listen_only)
                    if not ok:
                        raise OSError(f"couldn't bring {name} up: {msg}")
            return open_connection(kind, name, listen_only=listen_only, profile=profiles.active(), bitrate=bitrate)

        self.run_job("Connecting", work, self._connected)

    def _connected(self, conn):
        self.connection = conn
        self.session = conn.session
        self.result = None
        self.obd_codes = None
        self.j1939_live.clear()
        self.obd_live.clear()
        self._bus_level, self._bus_was_ok = link.OFF, False
        if self.protocol == profiles.OBD2:
            from ..core.obd import ObdClient
            from ..core.obd_stream import ObdStreamer
            self.obd_client = ObdClient(conn.session)
            self.obd_streamer = ObdStreamer(self.obd_client)
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
        if self.connection is not None:
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

    def cue(self, name: str, link_status: bool = False):
        if link_status and not self.settings.get("sound.link_cues"):
            return
        sound.play(name, speaker=bool(self.settings.get("sound.speaker")),
                   buzzer=str(self.settings.get("sound.buzzer") or sound.AUTO))

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
            return self.run_job("Reading codes", client.read_dtcs_by_ecu, self._obd_codes_read)
        session = self.session
        return self.run_job(scan.PRESETS[preset][0], lambda: scan.run_preset(session, preset), self._scanned)

    def _scanned(self, result):
        self.result = result
        extra = " (listen-only: broadcast codes only)" if result.listen_only else ""
        self.toast(f"{result.title}: {result.code_count} code(s){extra}")

    def _obd_codes_read(self, by_ecu):
        self.obd_codes = by_ecu
        n = sum(len(c) for kinds in by_ecu.values() for c in kinds.values())
        self.toast(f"{len(by_ecu)} module(s) answered, {n} code(s)")

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
            meta.update(interface=self.settings.get("can.iface"), bitrate=self.settings.get("can.bitrate"))
        if self.session is not None:
            meta["listen_only"] = self.session.listen_only
        return meta

    def toggle_log(self):
        self.stop_log() if self.recording else self.start_log()

    def start_log(self) -> bool:
        if self.recording:
            return True
        has_meters = bool(self.hub.sources())
        if not self.live and not has_meters:
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
                    measurements=self.hub if has_meters else None)
        except (OSError, ValueError) as e:
            self.toast(f"Can't start the log: {e}")
            return False
        self.settings.set("log.plan", c.plan_labels())
        if self.protocol == profiles.OBD2 and self.obd_streamer is not None and not self.obd_streamer.running:
            self.toggle_obd_stream()        # a car only talks when asked: keep it talking while logging
        self.toast("Recording")
        return True

    def stop_log(self):
        if not self.recording:
            return
        self.capture.stop()
        self.toast("Stopped: Save keeps it, or tag / rename first")

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
            self.toast(f"Saved {os.path.basename(folder)}")
            if then_start:
                self.start_log()
        self.run_job("Saving the log", lambda: c.save(system=system), done)

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
        if now - self._last_sample >= SAMPLE_EVERY:
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
            self.bus = link.bus_status(s, now)
        if not self.live or (self.bus.level != link.OK and now - s.started_at < link.QUIET_SECS):
            return
        was, self._bus_level = self._bus_level, self.bus.level
        if self.bus.level == link.IDLE or was == link.IDLE:
            pass
        elif was == link.OK and self.bus.level != link.OK:
            self.cue("lost", link_status=True)
        elif was in (link.WAIT, link.BAD) and self.bus.level == link.OK:
            self.cue("back" if self._bus_was_ok else "found", link_status=True)
        if self.bus.level == link.OK:
            self._bus_was_ok = True

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
        if self.recording:
            self.capture.stop()
        if save and self.capture.state == cap.STOPPED:
            try:
                self.last_saved = self.capture.save(system=self.units)
            except Exception:
                traceback.print_exc()
        self.disconnect()
        self.hub.stop()

    def pending_captures(self) -> list[str]:
        return cap.pending_captures()
