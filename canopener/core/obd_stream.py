"""Keep an OBD-II car talking: poll a list of (module, PID) in a background thread.

A car's diagnostic port only carries live values when a tester asks, so a live view (or a
capture that's recording) needs a loop asking for them. The answers are decoded passively by
``core.live.ObdLiveData`` from the frames on the bus, so this only sends requests.

The desktop OBD-II tab has its own copy of this loop; the on-device UI uses this one.
"""

import threading
import time

POLL_GAP = 0.01         # between requests: ~100/s at most, polite to a busy gateway


class ObdStreamer:
    def __init__(self, client, gap: float = POLL_GAP):
        self.client = client            # core.obd.ObdClient
        self.gap = gap
        self.todo: list[tuple[int, int]] = []     # (module response id, PID); replace it any time
        self.rate = 0.0                 # answers per second
        self._running = False
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self, todo: list[tuple[int, int]] | None = None):
        if todo is not None:
            self.todo = list(todo)
        if self._running or not self.client.available:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="obd-stream", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        self.rate = 0.0

    def _loop(self):
        n, t0 = 0, time.time()
        while self._running:
            todo = list(self.todo)
            if not todo:
                time.sleep(0.2)
            for ecu, pid in todo:
                if not self._running:
                    break
                if self.client.read_pid(ecu, pid) is not None:
                    n += 1
                time.sleep(self.gap)
            if time.time() - t0 >= 1.0:
                self.rate, n, t0 = n / (time.time() - t0), 0, time.time()
