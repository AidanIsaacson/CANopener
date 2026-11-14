"""The guided Profile Builder on the handheld: it says what to do, counts, and names what followed
(core.guide has the logic; core.observed saves the profile)."""

import os
import time

from ...core import capture as cap
from ...core import guide, observed, profiles
from .base import InfoDialog, Row, RowList, Screen, SoftKey, choose, confirm

HELP = ["It first asks what the machine is, so it only asks for things that machine has, and so the controls are "
        "mapped with the key on and nothing running before the engine or pump is started.",
        "", "It asks you to do one thing at a time on the machine: wait for 'Get ready', then do it and hold it "
        "until it says 'Let go'. It watches the bus while you do, and names whatever followed after the step.",
        "", "First it asks for hands off, to learn what changes by itself (those are never offered).",
        "", "If nothing followed, try again a little slower, or skip. Doing a step twice and getting the same "
            "answer marks it 'seen again'. It only listens: nothing is sent to the machine.",
        "", "Everything is also recorded as a normal log, with a timestamp at each step."]


def start(app):
    """Ask what the machine is and open the builder (from the Log page)."""
    ctx = app.ctx
    if not ctx.live:
        ctx.toast("Connect to the machine's bus first")
        return
    answers: dict = {}

    def ask():
        q = guide.next_question(answers)
        if q is None:
            title, steps = guide.build_script(answers)
            if ctx.recording or ctx.start_log(stream=False):
                app.push(GuideScreen(app, title, steps))
            return

        def answered(value):
            answers[q.id] = value
            ask()
        choose(app, q.text, list(q.options), None, answered)
    ask()


def status(ctx) -> str:
    n = sum(1 for f in (os.listdir(profiles.user_dir()) if os.path.isdir(profiles.user_dir()) else [])
            if f.startswith("observed-"))
    return f"{n} built here" if n else "Map an unknown machine"


class BuilderHome(Screen):
    """The Profile builder tile: what it does, and Start."""

    title = "Profile builder"

    def __init__(self, app):
        super().__init__(app)
        self.rows = RowList(self._rows)

    def _rows(self):
        ctx = self.ctx
        rows = [Row("Build a profile for this machine", kind="header"),
                Row("It asks what the machine is, then walks you through its controls and functions one at a "
                    "time and names what each one does on the bus.", kind="info",
                    hint="It only listens. Takes 5 to 15 minutes; skip anything the machine doesn't have.")]
        if not ctx.live:
            rows.append(Row("Connect to the machine's bus first", kind="info", level=lambda: "bad",
                            hint="CAN > Connect"))
        rows.append(Row("Start", kind="action", select=lambda: start(self.app)))
        built = []
        folder = profiles.user_dir()
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            doc = observed.load_doc(os.path.join(folder, name))
            if doc:
                built.append((os.path.splitext(name)[0], doc))
        if built:
            rows.append(Row("Built on this unit", kind="header"))
            active = profiles.active()
            for pid, doc in built:
                rows.append(Row(doc["name"], f"{len(doc.get('signals', []))} values"
                                + (", in use" if active and active.id == pid else ""),
                                select=lambda pid=pid: self._use(pid)))
        return rows

    def _use(self, pid):
        self.ctx.apply_profile(pid)
        self.ctx.toast("Using " + self.ctx.profile_name())

    def softkeys(self):
        return [SoftKey("Start", lambda: start(self.app)), None, None,
                SoftKey("Help", lambda: self.app.push(InfoDialog(self.app, self.title, HELP)))]

    def on_action(self, action, n=1):
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)


class GuideScreen(Screen):
    title = "Profile builder"

    def __init__(self, app, script: str, steps: list):
        super().__init__(app)
        self.script = script
        self.g = guide.Guide(list(steps))
        self.rows = RowList(self._rows)
        self.session = self.ctx.session
        self.session.add_listener(self.g.feed)
        self.saved = False
        self._cued = ""

    def on_leave(self):
        if self.session is not None:
            self.session.remove_listener(self.g.feed)

    # --- what's on screen ---
    def _rows(self):
        g = self.g
        g.tick()
        self._cue()
        s = g.step
        big, small = g.prompt()
        head = "Finished" if s is None or g.state == guide.DONE else f"Step {g.index + 1} of {len(g.steps)}: {s.name}"
        rows = [Row(s.stage, kind="info", level=lambda: "sim")] if s is not None and s.stage \
            and g.state != guide.DONE else []
        rows += [Row(head, kind="header"), Row(big, kind="info", hint=small,
                                              level=lambda: {guide.DO: "ok", guide.REST: "wait", guide.QUIET: "wait"}
                                              .get(g.state, "idle"))]
        left = g.remaining()
        if left:
            rows.append(Row("Hold it" if g.state == guide.DO else "Wait", f"{left:.0f} s", kind="info"))
        if g.state == guide.RESULT:
            for i, o in enumerate(g.offers):
                rows.append(Row(o.cand.key, "seen again" if o.again else "", select=lambda i=i: g.toggle(i),
                                check=lambda o=o: o.keep, hint=o.text))
        if g.kept:
            rows.append(Row(f"Named so far ({len(g.kept)})", kind="header"))
            rows += [Row(e["name"], key, kind="info") for key, e in g.kept.items()]
        if not g.frames_seen:
            rows.append(Row("No frames from the bus yet", kind="info", level=lambda: "bad"))
        return rows

    def _cue(self):
        """A chirp when it's time to act and when to let go."""
        g = self.g
        if g.state != self._cued:
            self._cued = g.state
            if g.state == guide.DO:
                self.ctx.cue("tag")
            elif g.state == guide.RESULT:
                self.ctx.cue("rec_stop")

    # --- keys ---
    def begin(self):
        s = self.g.step
        if s is not None and s.caution and self.g.state == guide.WAITING:
            confirm(self.app, s.name, f"{s.caution}\n\n{s.prompt}.", "Go on", self.g.begin, danger=False)
        else:
            self.g.begin()

    def finish(self):
        self.g.finish()

    def save(self):
        g, ctx = self.g, self.ctx
        c = ctx.capture
        if not self.saved:                          # the steps become the log's timestamps
            for t, label in g.marks:
                if label != "(rest)":
                    c.markers.append(cap.Marker(label, t, planned=False))
            self.saved = True
            if ctx.recording:
                ctx.stop_log()
                ctx.save_log()
        if not g.kept:
            ctx.toast("Nothing was named, so there's no profile to save. The log is kept.")
            self.app.pop(self)
            return
        machine = dict(c.machine)
        label = " ".join(str(machine.get(k, "")) for k in ("make", "model", "serial")).strip()
        opts = [(f"New profile: {label}", ("new", label))] if label else []
        stamp = time.strftime("%Y-%m-%d %H%M")
        opts.append((f"New profile: {self.script} {stamp}", ("new", f"{self.script} {stamp}")))
        folder = profiles.user_dir()
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            doc = observed.load_doc(os.path.join(folder, name))
            if doc:
                opts.append((f"Add to: {doc['name']}", ("add", os.path.join(folder, name))))

        def go(what):
            how, value = what
            proto = ctx.protocol if ctx.protocol in profiles.GENERIC else profiles.J1939
            source = f"guided profile builder ({self.script}), log {os.path.basename(c.folder or '')}"
            try:
                if how == "add":
                    path = observed.save_profile(observed.build_profile("", g.entries(), proto, source, machine,
                                                                        base=observed.load_doc(value)), value)
                else:
                    path = observed.save_profile(observed.build_profile(value, g.entries(), proto, source, machine))
            except (OSError, profiles.ProfileError) as e:
                ctx.toast(f"Couldn't save the profile: {e}")
                return
            ctx.apply_profile(os.path.splitext(os.path.basename(path))[0])
            ctx.toast(f"Saved {len(g.kept)} value(s) as '{ctx.profile_name()}': Live shows them now")
            ctx.share_profile(path)
            self.app.pop(self)
        choose(self.app, "Save as a profile", opts, None, go)

    def softkeys(self):
        g = self.g
        if g.state == guide.DONE:
            return [SoftKey("Save", self.save), None, None,
                    SoftKey("Help", lambda: self.app.push(InfoDialog(self.app, self.title, HELP)))]
        if g.state == guide.RESULT:
            return [SoftKey("Keep, next", g.accept), SoftKey("Again", g.again), SoftKey("Skip", g.skip),
                    SoftKey("Finish", self.finish)]
        busy = g.state != guide.WAITING
        return [SoftKey("Start", self.begin, enabled=not busy), SoftKey("Skip", g.skip, enabled=not busy),
                SoftKey("Finish", self.finish, enabled=not busy),
                SoftKey("Help", lambda: self.app.push(InfoDialog(self.app, self.title, HELP)))]

    def on_action(self, action, n=1):
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)
