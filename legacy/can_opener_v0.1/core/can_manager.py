"""
CAN bus manager.
Handles interface bring-up via subprocess, python-can Bus object,
background reader thread, and frame dispatch to registered callbacks.
"""

import threading
import subprocess
import time
import queue
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

try:
    import can
    CAN_AVAILABLE = True
except ImportError:
    CAN_AVAILABLE = False


@dataclass
class CANFrame:
    timestamp:  float
    arb_id:     int
    data:       bytes
    is_extended: bool = True
    is_error:   bool  = False
    channel:    str   = "can0"


# Bitrate → auto-scan order
AUTO_BITRATES = [250000, 500000, 125000, 1000000]
VALID_BITRATES = [125000, 250000, 500000, 1000000]


class CANManager:
    def __init__(self):
        self._bus        = None
        self._thread     = None
        self._running    = False
        self._callbacks: List[Callable[[CANFrame], None]] = []
        self._error_cb:  Optional[Callable[[str], None]] = None
        self._status_cb: Optional[Callable[[str], None]] = None
        self.connected   = False
        self.interface   = "can0"
        self.bitrate     = 250000
        self.protocol    = "J1939"
        self.frame_count = 0
        self.error_count = 0
        self._fps_count  = 0
        self._fps_time   = time.time()
        self.fps         = 0.0

    # ── Public API ──────────────────────────────────────────────────────────────

    def register_frame_callback(self, cb: Callable[[CANFrame], None]):
        self._callbacks.append(cb)

    def register_error_callback(self, cb: Callable[[str], None]):
        self._error_cb = cb

    def register_status_callback(self, cb: Callable[[str], None]):
        self._status_cb = cb

    def connect(self, interface: str, bitrate: int, protocol: str,
                auto_baud: bool = False) -> Tuple[bool, str]:
        """
        Bring up interface and open python-can bus.
        Returns (success, message).
        Interface "sim" routes to the simulator regardless of other settings.
        Falls back to sim if python-can is not available.
        """
        if self.connected:
            self.disconnect()

        self.interface = interface
        self.protocol  = protocol

        # Sim mode: explicit request or no python-can available
        if interface == "sim" or not CAN_AVAILABLE:
            return self._connect_sim()

        if auto_baud:
            return self._auto_connect(interface, protocol)

        br = int(bitrate)
        ok, msg = self._bring_up_interface(interface, br)
        if not ok:
            return False, msg
        return self._open_bus(interface, br)

    def _connect_sim(self) -> Tuple[bool, str]:
        from .sim import CANSimulator
        sim = CANSimulator()
        for cb in self._callbacks:
            sim.register_frame_callback(cb)
        sim.register_error_callback(self._error_cb)
        sim.register_status_callback(self._status_cb)
        # Mirror sim state onto self so status bar reads correctly
        ok, msg = sim.connect()
        if ok:
            self._sim      = sim
            self.connected = True
            self.interface = "sim"
            self.bitrate   = 250000
            # Proxy fps/frame_count updates via a polling thread
            self._sim_proxy_running = True
            t = threading.Thread(target=self._sim_proxy, daemon=True)
            t.start()
        return ok, msg

    def _sim_proxy(self):
        """Keep self.fps and self.frame_count in sync with sim."""
        while getattr(self, "_sim_proxy_running", False):
            sim = getattr(self, "_sim", None)
            if sim:
                self.fps         = sim.fps
                self.frame_count = sim.frame_count
            time.sleep(0.5)

    def disconnect(self):
        self._sim_proxy_running = False
        sim = getattr(self, "_sim", None)
        if sim:
            sim.disconnect()
            self._sim = None
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        if self._bus:
            try:
                self._bus.shutdown()
            except Exception:
                pass
            self._bus = None
        self.connected   = False
        self.frame_count = 0
        self._fps_count  = 0
        self.fps         = 0.0
        self._set_status("Disconnected")

    def send_request(self, pgn: int, sa: int = 0xF9, da: int = 0xFF):
        """Send a J1939 PGN request frame (PGN 0xEA00)."""
        if not self._bus or not self.connected:
            return False
        try:
            # Request PGN: priority 6, PF=0xEA, DA, SA
            arb_id = (6 << 26) | (0xEA << 16) | (da << 8) | sa
            data   = bytes([pgn & 0xFF, (pgn >> 8) & 0xFF, (pgn >> 16) & 0xFF,
                            0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
            msg = can.Message(arbitration_id=arb_id, data=data,
                              is_extended_id=True)
            self._bus.send(msg)
            return True
        except Exception as e:
            if self._error_cb:
                self._error_cb(f"Send error: {e}")
            return False

    # ── Internal ────────────────────────────────────────────────────────────────

    def _bring_up_interface(self, iface: str, bitrate: int) -> Tuple[bool, str]:
        """Attempt to bring up the CAN interface via ip link."""
        try:
            # Bring down first (ignore errors)
            subprocess.run(
                ["sudo", "ip", "link", "set", iface, "down"],
                capture_output=True, timeout=3
            )
            result = subprocess.run(
                ["sudo", "ip", "link", "set", iface, "up",
                 "type", "can", "bitrate", str(bitrate)],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                err = result.stderr.strip()
                # If it fails, try without sudo (vcan, already-up interfaces)
                result2 = subprocess.run(
                    ["ip", "link", "set", iface, "up",
                     "type", "can", "bitrate", str(bitrate)],
                    capture_output=True, text=True, timeout=5
                )
                if result2.returncode != 0:
                    return False, f"Interface bring-up failed: {err}"
            return True, "OK"
        except subprocess.TimeoutExpired:
            return False, "Interface bring-up timed out"
        except Exception as e:
            return False, str(e)

    def _open_bus(self, iface: str, bitrate: int) -> Tuple[bool, str]:
        """Open python-can bus and start reader thread."""
        if not CAN_AVAILABLE:
            return False, "python-can not installed"
        try:
            self._bus = can.interface.Bus(
                channel=iface,
                interface="socketcan",
                bitrate=bitrate,
                receive_own_messages=False,
            )
            self.bitrate   = bitrate
            self.connected = True
            self._running  = True
            self.frame_count = 0
            self.error_count = 0
            self._fps_time   = time.time()
            self._thread = threading.Thread(
                target=self._reader_loop, daemon=True)
            self._thread.start()
            self._set_status(f"Connected  {iface}  {bitrate//1000}k")
            return True, f"Connected on {iface} @ {bitrate//1000}k"
        except Exception as e:
            self.connected = False
            return False, str(e)

    def _auto_connect(self, iface: str, protocol: str) -> Tuple[bool, str]:
        """Try each bitrate until we receive valid frames."""
        self._set_status("Auto-scanning...")
        for br in AUTO_BITRATES:
            self._set_status(f"Trying {br//1000}k...")
            ok, msg = self._bring_up_interface(iface, br)
            if not ok:
                continue
            try:
                bus = can.interface.Bus(
                    channel=iface, interface="socketcan", bitrate=br)
                # Listen for up to 2 seconds for valid frames
                deadline = time.time() + 2.0
                found = False
                while time.time() < deadline:
                    msg_rx = bus.recv(timeout=0.2)
                    if msg_rx and not msg_rx.is_error_frame:
                        found = True
                        break
                bus.shutdown()
                if found:
                    return self._open_bus(iface, br)
            except Exception:
                pass
        return False, "Auto-scan: no valid frames found on any bitrate"

    def _reader_loop(self):
        """Background thread: read frames and dispatch to callbacks."""
        while self._running:
            try:
                msg = self._bus.recv(timeout=0.1)
                if msg is None:
                    continue
                self.frame_count += 1
                self._fps_count  += 1

                # Update FPS every second
                now = time.time()
                if now - self._fps_time >= 1.0:
                    self.fps       = self._fps_count / (now - self._fps_time)
                    self._fps_count = 0
                    self._fps_time  = now

                if msg.is_error_frame:
                    self.error_count += 1
                    continue

                frame = CANFrame(
                    timestamp   = msg.timestamp,
                    arb_id      = msg.arbitration_id,
                    data        = bytes(msg.data),
                    is_extended = msg.is_extended_id,
                    is_error    = msg.is_error_frame,
                    channel     = self.interface,
                )
                for cb in self._callbacks:
                    try:
                        cb(frame)
                    except Exception:
                        pass

            except can.CanError as e:
                if self._running:
                    self._running  = False
                    self.connected = False
                    if self._error_cb:
                        self._error_cb(f"Bus error: {e}")
                    self._set_status("Bus Error — Reconnect")
                break
            except Exception:
                pass

    def _set_status(self, msg: str):
        if self._status_cb:
            self._status_cb(msg)
