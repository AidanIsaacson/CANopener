"""Captures: a recording of the bus plus labelled timestamp markers.

The workflow (shared by the desktop GUI and, later, the handheld's hardware keys):

1. Plan: name the job, write down the customer concern, and list the timestamps you expect
   to tag ("key on", "start", "drive", "shutdown", ...). Plans can be saved as templates.
2. ``start()``: frames are written to ``capture.log`` in a hidden pending folder.
3. While recording, ``tag_next()`` tags the next untagged planned timestamp (or adds a
   generic one when the plan runs out) and ``tag(n)`` tags timestamp *n* directly.
4. ``stop()``, then edit labels / delete markers, then ``save()`` (or ``discard()``).
   Saving moves the folder to its final name and writes markers, a live-data spreadsheet
   and a summary (see ``capture_report``).

Markers and metadata are written to disk on every change, so a crash mid-recording leaves a
pending folder that ``pending_captures()`` finds and ``Capture.load()`` can recover.
"""

import csv
import datetime
import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass

from .. import __version__
from . import config

CAPTURE_LOG = "capture.log"
MARKERS_CSV = "markers.csv"
META_JSON = "capture.json"
LIVE_CSV = "live_data.csv"
SUMMARY_TXT = "summary.txt"
PENDING_PREFIX = ".recording_"

IDLE, RECORDING, STOPPED, SAVED = "idle", "recording", "stopped", "saved"


@dataclass
class Marker:
    label: str
    t: float | None = None      # wall-clock time it was tagged; None = planned, not tagged yet
    planned: bool = True        # False for generic / repeat markers added while recording
    note: str = ""

    @property
    def tagged(self) -> bool:
        return self.t is not None


def safe_name(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._ -]+", "", text or "").strip().replace(" ", "_")
    return s[:60]


def fmt_elapsed(seconds: float) -> str:
    sign = "-" if seconds < 0 else "+"
    seconds = abs(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(int(m), 60)
    return f"{sign}{h}:{m:02d}:{s:04.1f}" if h else f"{sign}{m:02d}:{s:04.1f}"


class Capture:
    def __init__(self, name: str = "", notes: str = "", plan: list[str] | None = None):
        self.name = name
        self.notes = notes
        self.markers: list[Marker] = [Marker(label) for label in (plan or [])]
        self.state = IDLE
        self.started: float | None = None
        self.stopped: float | None = None
        self.folder: str | None = None      # working folder (pending) or saved folder
        self.root: str | None = None        # where it will be saved by default
        self.meta: dict = {}
        self.frames = 0
        self._session = None

    # ------------------------------------------------------------------
    # markers
    # ------------------------------------------------------------------
    def title(self, index: int) -> str:
        return f"{index + 1} - {self.markers[index].label}"

    def next_index(self) -> int | None:
        return next((i for i, m in enumerate(self.markers) if not m.tagged), None)

    def next_title(self) -> str:
        i = self.next_index()
        return self.title(i) if i is not None else f"{len(self.markers) + 1} - Timestamp {len(self.markers) + 1}"

    def add_planned(self, label: str | None = None) -> int:
        self.markers.append(Marker(label or f"Timestamp {len(self.markers) + 1}"))
        self._changed()
        return len(self.markers) - 1

    def tag_next(self, t: float | None = None) -> int:
        """Tag the next untagged timestamp, or add a generic one after the last."""
        t = time.time() if t is None else t
        i = self.next_index()
        if i is None:
            self.markers.append(Marker(f"Timestamp {len(self.markers) + 1}", t, planned=False))
            i = len(self.markers) - 1
        else:
            self.markers[i].t = t
        self._changed()
        return i

    def tag(self, index: int, t: float | None = None) -> int | None:
        """Tag timestamp ``index`` (0-based). Never overwrites: a repeat press adds "(2)" after it.

        Returns the index tagged, or None when there is no such timestamp.
        """
        t = time.time() if t is None else t
        if not 0 <= index < len(self.markers):
            return None
        m = self.markers[index]
        if not m.tagged:
            m.t = t
            self._changed()
            return index
        base = re.sub(r" \(\d+\)$", "", m.label)
        n = 1 + sum(1 for x in self.markers if re.sub(r" \(\d+\)$", "", x.label) == base)
        pos = index + 1
        while pos < len(self.markers) and not self.markers[pos].planned \
                and re.sub(r" \(\d+\)$", "", self.markers[pos].label) == base:
            pos += 1
        self.markers.insert(pos, Marker(f"{base} ({n})", t, planned=False))
        self._changed()
        return pos

    def undo_last(self) -> int | None:
        """Take back the most recent tag. Planned timestamps go back to untagged."""
        tagged = [(m.t, i) for i, m in enumerate(self.markers) if m.tagged]
        if not tagged:
            return None
        _, i = max(tagged)
        if self.markers[i].planned:
            self.markers[i].t = None
        else:
            del self.markers[i]
        self._changed()
        return i

    def rename(self, index: int, label: str):
        self.markers[index].label = label.strip() or self.markers[index].label
        self._changed()

    def set_note(self, index: int, note: str):
        self.markers[index].note = note
        self._changed()

    def delete(self, index: int):
        del self.markers[index]
        self._changed()

    def move(self, index: int, to: int):
        to = max(0, min(to, len(self.markers) - 1))
        self.markers.insert(to, self.markers.pop(index))
        self._changed()

    def clear_tag(self, index: int):
        self.markers[index].t = None
        self._changed()

    def tagged(self) -> list[tuple[int, Marker]]:
        """Tagged markers in time order, with their list index."""
        return sorted(((i, m) for i, m in enumerate(self.markers) if m.tagged), key=lambda x: x[1].t)

    def phase_at(self, t: float) -> str:
        """Title of the most recent marker at or before ``t`` ("" before the first one)."""
        cur = ""
        for i, m in self.tagged():
            if m.t <= t:
                cur = self.title(i)
            else:
                break
        return cur

    def elapsed(self, now: float | None = None) -> float:
        if self.started is None:
            return 0.0
        end = self.stopped if self.stopped is not None else (time.time() if now is None else now)
        return end - self.started

    def plan_labels(self) -> list[str]:
        return [m.label for m in self.markers if m.planned]

    def fresh_copy(self) -> "Capture":
        """A new, idle capture with the same plan and notes, e.g. to repeat the test."""
        c = Capture(self.name, self.notes, self.plan_labels())
        c.root = self.root
        return c

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def log_path(self) -> str | None:
        return os.path.join(self.folder, CAPTURE_LOG) if self.folder else None

    def start(self, session, root: str | None = None, meta: dict | None = None):
        if self.state != IDLE:
            raise RuntimeError(f"capture is {self.state}")
        self.root = root or self.root or config.logs_dir()
        self.started = time.time()
        stamp = datetime.datetime.fromtimestamp(self.started).strftime("%Y%m%d_%H%M%S")
        self.folder = config.ensure_dir(os.path.join(self.root, f"{PENDING_PREFIX}{stamp}"))
        self.meta = dict(meta or {})
        self.meta.setdefault("app_version", __version__)
        session.start_recording(self.log_path)
        self._session = session
        self.state = RECORDING
        self._write_state()

    def stop(self):
        if self.state != RECORDING:
            return
        done = self._session.stop_recording() if self._session else None
        self.frames = done[1] if done else self.frames
        self._session = None
        self.stopped = time.time()
        self.state = STOPPED
        self._write_state()

    @property
    def recorded_frames(self) -> int:
        rec = self._session.recording if self._session else None
        return rec.count if rec else self.frames

    def default_folder_name(self) -> str:
        stamp = datetime.datetime.fromtimestamp(self.started or time.time()).strftime("%Y%m%d_%H%M%S")
        return f"{stamp}_{safe_name(self.name) or 'capture'}"

    def save(self, parent_dir: str | None = None, *, extra_files: dict[str, str] | None = None,
             system: str = "metric", progress=None) -> str:
        """Finish a stopped capture: move it to its final folder and write every output file.

        Calling it again on a saved capture rewrites the outputs in place (after relabelling).
        Returns the capture folder.
        """
        if self.state not in (STOPPED, SAVED):
            raise RuntimeError(f"can't save a capture that is {self.state}")
        if self.state == STOPPED or parent_dir is not None:
            parent = config.ensure_dir(parent_dir or self.root or config.logs_dir())
            dest = os.path.join(parent, self.default_folder_name())
            n = 2
            while os.path.exists(dest) and os.path.abspath(dest) != os.path.abspath(self.folder):
                dest = os.path.join(parent, f"{self.default_folder_name()}_{n}")
                n += 1
            if os.path.abspath(dest) != os.path.abspath(self.folder):
                shutil.move(self.folder, dest)
                self.folder = dest
        self.state = SAVED
        for name, text in (extra_files or {}).items():
            with open(os.path.join(self.folder, name), "w") as f:
                f.write(text)
        from .capture_report import write_outputs  # imported here: it pulls in the decoders
        write_outputs(self, system=system, progress=progress)
        self._write_state()
        return self.folder

    def discard(self):
        if self.state == RECORDING:
            self.stop()
        if self.folder and os.path.isdir(self.folder):
            shutil.rmtree(self.folder)
        self.folder = None
        self.state = IDLE

    # ------------------------------------------------------------------
    # files
    # ------------------------------------------------------------------
    def _changed(self):
        if self.folder and self.state in (RECORDING, STOPPED, SAVED):
            self._write_state()

    def to_dict(self) -> dict:
        return {
            "name": self.name, "notes": self.notes, "state": self.state,
            "started": self.started, "stopped": self.stopped, "frames": self.recorded_frames,
            "markers": [asdict(m) for m in self.markers], "meta": self.meta,
        }

    def _write_state(self):
        if not self.folder:
            return
        tmp = os.path.join(self.folder, META_JSON + ".tmp")
        with open(tmp, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        os.replace(tmp, os.path.join(self.folder, META_JSON))
        with open(os.path.join(self.folder, MARKERS_CSV), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["number", "label", "title", "time", "elapsed_s", "elapsed", "note"])
            for i, m in enumerate(self.markers):
                if m.tagged and self.started is not None:
                    wall = datetime.datetime.fromtimestamp(m.t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    w.writerow([i + 1, m.label, self.title(i), wall, f"{m.t - self.started:.3f}",
                                fmt_elapsed(m.t - self.started), m.note])
                else:
                    w.writerow([i + 1, m.label, self.title(i), "", "", "not tagged", m.note])

    @classmethod
    def load(cls, folder: str) -> "Capture":
        with open(os.path.join(folder, META_JSON)) as f:
            d = json.load(f)
        c = cls(d.get("name", ""), d.get("notes", ""))
        c.markers = [Marker(**m) for m in d.get("markers", [])]
        c.started, c.stopped = d.get("started"), d.get("stopped")
        c.frames = d.get("frames", 0)
        c.meta = d.get("meta", {})
        c.folder = folder
        c.root = os.path.dirname(os.path.abspath(folder))
        state = d.get("state", SAVED)
        if state == RECORDING:              # the app died mid-recording: recover as stopped
            c.stopped = c.stopped or _last_frame_time(os.path.join(folder, CAPTURE_LOG)) or c.started
            state = STOPPED
        c.state = state
        return c

    @property
    def pending(self) -> bool:
        return bool(self.folder) and os.path.basename(self.folder).startswith(PENDING_PREFIX)


def _last_frame_time(path: str) -> float | None:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            tail = f.read().decode(errors="replace").strip().splitlines()
        return float(tail[-1].split(")")[0].lstrip("(")) if tail else None
    except (OSError, ValueError, IndexError):
        return None


def pending_captures(root: str | None = None) -> list[str]:
    """Unsaved capture folders left behind (stopped but not saved, or the app crashed)."""
    root = root or config.logs_dir()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [os.path.join(root, n) for n in names
            if n.startswith(PENDING_PREFIX) and os.path.exists(os.path.join(root, n, META_JSON))]


def saved_captures(root: str | None = None) -> list[str]:
    root = root or config.logs_dir()
    try:
        names = sorted(os.listdir(root), reverse=True)
    except OSError:
        return []
    return [os.path.join(root, n) for n in names
            if not n.startswith(".") and os.path.exists(os.path.join(root, n, META_JSON))]


# ----------------------------------------------------------------------------
# Marker-list templates
# ----------------------------------------------------------------------------
BUILTIN_TEMPLATES = {
    "Intermittent shutdown": ["key on", "start", "drive", "shutdown", "wait", "key cycle 1",
                              "key cycle 2", "key on again", "start"],
    "Cold start": ["key on", "glow / preheat", "crank", "start", "idle", "first load"],
    "Fault reproduction": ["baseline", "fault appears", "fault clears"],
}


def _templates_path() -> str:
    return os.path.join(config.config_dir(), "marker_templates.json")


def load_templates() -> dict[str, list[str]]:
    """Built-in templates plus the user's (user ones win on a name clash)."""
    out = dict(BUILTIN_TEMPLATES)
    try:
        with open(_templates_path()) as f:
            user = json.load(f)
        out.update({str(k): [str(x) for x in v] for k, v in user.items() if isinstance(v, list)})
    except (OSError, ValueError, AttributeError):
        pass
    return out


def _user_templates() -> dict:
    try:
        with open(_templates_path()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_template(name: str, labels: list[str]):
    d = _user_templates()
    d[name] = list(labels)
    config.ensure_dir(config.config_dir())
    with open(_templates_path(), "w") as f:
        json.dump(d, f, indent=2)


def delete_template(name: str) -> bool:
    d = _user_templates()
    if name not in d:
        return False
    del d[name]
    with open(_templates_path(), "w") as f:
        json.dump(d, f, indent=2)
    return True
