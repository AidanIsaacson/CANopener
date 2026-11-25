"""The guided Profile Builder on the handheld: it says what to do, counts, then shows what followed
live so the technician can work the control and pick the value that moves with it (core.guide
has the logic, core.fieldwatch the live values, core.observed saves the profile)."""

import os
import time

from ...core import guide, observed, profiles, suggest
from .base import ConfirmDialog, InfoDialog, Row, RowList, Screen, SoftKey, choose, confirm
from .explore import ExploreScreen, fmt_field, moving_level

HELP = ["It first asks what the machine is, so it only asks for things that machine has, and so the controls are "
        "mapped with the key on and nothing running before the engine or pump is started.",
        "", "It asks you to do one thing at a time on the machine: wait for 'Get ready', then do it and hold it "
            "until it says 'Let go'.",
        "", "Then it shows what moved, live: work the control a few more times and watch. The value that follows "
            "it lights up and counts up; tick it and press Keep. When one clearly follows the control, it's ticked "
            "for you. The list holds still for a moment whenever you press a key.",
        "", "First it asks for hands off, to learn what changes by itself. Values that change while nobody touches "
            "the machine are left out (the screen says how many).",
        "", "Values the profile in use already names are shown too, so you can see they moved; they aren't "
            "offered again.",
        "", "A control with two directions (lift and lower, travel both ways) is asked for both ways. When the "
            "second way moves the value kept for the first, it's ticked for you and the two make one signal "
            "(signed when it swings either side of rest).",
        "", "Interlocks: a control worked with nobody on the seat or the pedal often makes the machine show a "
            "'release the control' warning. That's expected. The builder says which warning came with which "
            "step, names it in the profile, and waits for it to clear before the next step.",
        "", "Values another profile on this unit already knows (the same message on a sister machine) are offered "
            "first and ticked, with that profile's name.",
        "", "Doing a step twice and getting the same answer marks it 'seen again'; working the control while "
            "picking marks it 'checked'. It only listens: nothing is sent to the machine.",
        "", "Everything is also recorded as a normal log, with timestamps at each step ('get ready', the step, "
            "'let go') and a record of what was offered and kept in the log's README."]


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
                app.push(GuideScreen(app, title, steps, borrowed_hints(ctx)))
            return

        def answered(value):
            answers[q.id] = value
            ask()
        choose(app, q.text, list(q.options), None, answered)

    best = ctx.check_profiles()
    if best is not None:
        def load_first():
            ctx.apply_profile(best.profile.id)
            ctx.toast(f"Using {best.profile.name}: the builder only looks for what it doesn't name yet")
            ask()
        app.push(ConfirmDialog(app, "A profile fits this bus",
                               f"{best.describe()}.\n\nLoad it first? Then the builder only looks for what it "
                               f"doesn't already name. (In use now: {ctx.profile_name()}.)", "Load it", load_first,
                               danger=False, on_no=ask))
        return
    ask()


def borrowed(ctx) -> list:
    """What other profiles on the unit know about the undecoded traffic heard now (core.suggest)."""
    if not ctx.live:
        return []
    found, _errors = profiles.available_profiles()
    return suggest.from_watch(ctx.fieldwatch, found, profiles.active())


def borrowed_hints(ctx) -> dict:
    try:
        return suggest.hints(borrowed(ctx))
    except Exception:                                       # a hint is a nicety: never stop the builder
        return {}


def status(ctx) -> str:
    n = sum(1 for f in (os.listdir(profiles.user_dir()) if os.path.isdir(profiles.user_dir()) else [])
            if f.startswith("observed-"))
    return f"{n} built here" if n else "Map an unknown machine"


def bus_rows(ctx) -> list:
    """What it's listening to, and the profile in use: shown at the top of the builder pages."""
    if not ctx.live:
        return [Row("Not connected", kind="info", level=lambda: "bad", hint="CAN > Connect first")]
    p = profiles.active()
    named = len(p.signals) if p is not None else 0
    return [Row(f"Listening: {ctx.frames_per_second():.0f} frames a second", kind="info",
                level=lambda: "ok" if ctx.frames_per_second() > 0 else "wait",
                hint=f"Profile in use: {ctx.profile_name()} ({named} values named)")]


class BuilderHome(Screen):
    """The Profile builder tile: what it does, which profiles fit this bus, Start, Explore."""

    title = "Profile builder"

    def __init__(self, app):
        super().__init__(app)
        self.rows = RowList(self._rows)
        self._checked = 0.0
        self._borrowed: list = []

    def on_enter(self):
        self._checked = 0.0

    def _matches(self):
        ctx = self.ctx
        if ctx.live and time.time() - self._checked > 5.0:
            self._checked = time.time()
            ctx.check_profiles()
            try:
                self._borrowed = borrowed(ctx)
            except Exception:
                self._borrowed = []
        return ctx.profile_matches if ctx.live else []

    def _rows(self):
        ctx = self.ctx
        rows = [Row("Build a profile for this machine", kind="header"),
                Row("It asks what the machine is, then walks you through its controls and functions one at a "
                    "time and shows what each one does on the bus, live.", kind="info",
                    hint="It only listens. Takes 5 to 15 minutes; skip anything the machine doesn't have.")]
        rows += bus_rows(ctx)
        rows.append(Row("Start the guided builder", kind="action", select=lambda: start(self.app)))
        rows.append(Row("Explore the bus", kind="menu", select=self.explore,
                        hint="Every value, live: pick one and name it yourself"))
        matches = self._matches()
        if matches:
            active = profiles.active()
            rows.append(Row("Profiles that fit this bus", kind="header"))
            for m in matches[:4]:
                using = active is not None and active.id == m.profile.id
                rows.append(Row(m.profile.name, "in use" if using else "Load",
                                select=lambda m=m: self._use(m.profile.id),
                                hint=f"{m.matched} of its {m.total} messages are on this bus"))
        if self._borrowed and ctx.live:
            n = sum(len(b.good) for b in self._borrowed)
            rows.append(Row("Values other profiles know", kind="header"))
            rows.append(Row(f"Add all {n} to the profile in use", kind="action", select=self._borrow_all,
                            hint="Undecoded messages here that a profile for a similar machine decodes, checked "
                                 "against what's on the bus. Each keeps where it came from."))
            for b in self._borrowed[:6]:
                rows.append(Row(b.label, f"{len(b.good)} from {b.profile_name}", kind="info",
                                hint=", ".join(c.signal.name for c in b.good[:4])))
        built = []
        folder = profiles.user_dir()
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            doc = observed.load_doc(os.path.join(folder, name))
            if doc:
                built.append((os.path.splitext(name)[0], doc, os.path.join(folder, name)))
        if built:
            rows.append(Row("Built on this unit", kind="header"))
            active = profiles.active()
            for pid, doc, path in built:
                bits = [f"{len(doc.get('signals', []))} values"]
                if active and active.id == pid:
                    bits.append("in use")
                if pid in ctx.share_failed:
                    bits.append("not shared yet")
                rows.append(Row(doc["name"], ", ".join(bits), select=lambda pid=pid, path=path: self._built(pid, path)))
        return rows

    def _borrow_all(self):
        ctx = self.ctx
        entries = [e for b in self._borrowed for e in b.entries()]
        if not entries:
            return
        froms = sorted({b.profile_name for b in self._borrowed})

        def go():
            if ctx.add_borrowed(entries, froms):
                self._borrowed, self._checked = [], 0.0
        self.app.push(ConfirmDialog(self.app, "Add values from other profiles",
                                    f"{len(entries)} value(s) from {', '.join(froms)} fit what this bus sends. Add "
                                    f"them to the profile in use ({ctx.profile_name()})? They're marked 'borrowed' "
                                    "until the builder checks them here.", "Add them", go, danger=False))

    def explore(self):
        if not self.ctx.live:
            self.ctx.toast("Connect to the machine's bus first")
            return
        self.app.push(ExploreScreen(self.app))

    def _use(self, pid):
        self.ctx.apply_profile(pid)
        self.ctx.toast("Using " + self.ctx.profile_name())

    def _built(self, pid, path):
        def pick(what):
            if what == "use":
                self._use(pid)
            elif what == "share":
                if not self.ctx.share_profile(path):
                    self.ctx.toast("No upload server is set up (System > Uploads), or profile sharing is off")
        choose(self.app, "Profile built here", [("Use it", "use"), ("Share it with the upload server", "share")],
               None, pick)

    def softkeys(self):
        return [SoftKey("Start", lambda: start(self.app)), SoftKey("Explore", self.explore), None,
                SoftKey("Help", lambda: self.app.push(InfoDialog(self.app, self.title, HELP)))]

    def on_action(self, action, n=1):
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)


class GuideScreen(Screen):
    title = "Guided"

    def __init__(self, app, script: str, steps: list, hints: dict | None = None):
        super().__init__(app)
        self.script = script
        self.g = guide.Guide(list(steps), watch=self.ctx.fieldwatch, title=script, hints=dict(hints or {}))
        self.rows = RowList(self._rows)
        self.session = self.ctx.session
        self.session.add_listener(self.g.feed)
        self.saved = False
        self._cued = ""
        self._marked = 0                            # g.marks already written to the log
        self._journaled = None                      # what of g.summary() is in the capture already

    def on_leave(self):
        if self.session is not None:
            self.session.remove_listener(self.g.feed)

    # --- what's on screen ---
    def _rows(self):
        g, ctx = self.g, self.ctx
        g.tick()
        self._cue()
        self._mark()
        s = g.step
        big, small = g.prompt()
        head = "Finished" if s is None or g.state == guide.DONE else f"Step {g.index + 1} of {len(g.steps)}: {s.name}"
        rows = [Row(s.stage, kind="info", level=lambda: "sim")] if s is not None and s.stage \
            and g.state != guide.DONE else []
        rows += [Row(head, kind="header"), Row(big, kind="info", hint=small,
                                              level=lambda: {guide.DO: "ok", guide.REST: "wait", guide.QUIET: "wait",
                                                             guide.SETTLE: "wait"}.get(g.state, "idle"))]
        note = g.stage_note()
        if note:
            rows.append(Row("Before you start this part", kind="info", hint=note, level=lambda: "sim"))
        if g.settle_note:
            rows.append(Row("A warning stayed on", kind="info", hint=g.settle_note, level=lambda: "bad"))
        left = g.remaining()
        if left:
            rows.append(Row("Hold it" if g.state == guide.DO else "Wait", f"{left:.0f} s", kind="info"))
        if g.state == guide.SETTLE:
            rows.append(Row("Skip waits no longer", kind="info", hint="Skip moves on to the next step"))
        if g.state == guide.RESULT:
            rows += self._pick_rows()
        if g.kept:
            rows.append(Row(f"Named so far ({len(g.kept)})", kind="header"))
            rows += [Row(e["name"], e["observed"]["sure"], kind="info", hint=key) for key, e in g.kept.items()]
        if not g.frames_seen:
            rows.append(Row("No frames from the bus yet", kind="info", level=lambda: "bad",
                            hint="Is it connected? CAN > Connect"))
        elif g.bus_note:
            rows.append(Row("Quiet bus", kind="info", hint=g.bus_note, level=lambda: "bad"))
        rows += bus_rows(ctx)
        return rows

    def _pick_rows(self) -> list:
        g, watch = self.g, self.ctx.fieldwatch
        rows = []
        if g.offers:            # lit and counting up: it follows the control (the list holds still under a key)
            rows.append(Row("Which one moved? Work it and watch" + (" (held still)" if g.sorting_paused() else ""),
                            kind="header"))
        for i, o in enumerate(g.offers):
            info = watch.info(o.key)
            badges = []
            if o.moved:
                badges.append(f"changed {o.moved}x since 'let go'")
            if o.chatters:
                badges.append("bounces on and off")
            if o.mirror:
                badges.append(f"kept for {o.mirror}: moved the other way (one signal for both)")
            if o.hint:
                badges.append(f"{o.hint[1]} calls it '{o.hint[0]}'")
            if o.auto:
                badges.append("ticked for you: it followed")
            if o.again:
                badges.append("seen again")
            if o.also:
                badges.append("also moved for " + ", ".join(o.also[:2]))
            hint = "  ·  ".join([o.key, o.text] + badges)
            rows.append(Row(o.label, lambda k=o.key, i=info: fmt_field(i, watch.value(k)),
                            select=lambda i=i: g.toggle(i), check=lambda o=o: o.keep, hint=hint,
                            level=lambda k=o.key: moving_level(watch, k, time.time())))
        if g.known_keys:
            rows.append(Row("Already named, moved too", kind="header"))
            for name, key in g.known_keys[:6]:
                info = watch.info(key)
                rows.append(Row(name, lambda k=key, i=info: fmt_field(i, watch.value(k)), kind="info", hint=key,
                                level=lambda k=key: moving_level(watch, k, time.time())))
        if g.hidden:
            rows.append(Row(f"{g.hidden} left out: they change by themselves", kind="info",
                            hint="Seen changing while nobody touched the machine (or the low digits of a reading)"))
        return rows

    def _mark(self):
        """Each step's timestamps go into the log as they happen, and the record of what was offered
        and kept into the capture, so leaving without Save keeps them."""
        c, g = self.ctx.capture, self.g
        while self._marked < len(self.g.marks):
            t, label = self.g.marks[self._marked]
            self._marked += 1
            if self.ctx.recording:
                c.add_marker(label, t)
        state = (len(g.journal), len(g.kept), g.journal[-1].get("result") if g.journal else "")
        if state != self._journaled and self.ctx.recording and g.journal:
            self._journaled = state
            c.set_meta("guide", g.summary())

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

    def keep(self):
        g = self.g
        n = sum(1 for o in g.offers if o.keep)
        name = g.step.name if g.step else ""
        g.accept()
        self.ctx.toast(f"Kept {n} for {name}" if n else f"{name}: nothing kept")

    def save(self):
        g, ctx = self.g, self.ctx
        c = ctx.capture
        if not self.saved:
            self._mark()
            if ctx.recording and g.journal:
                c.set_meta("guide", g.summary())
            self.saved = True
            if ctx.recording:
                ctx.stop_log()
                ctx.save_log()
        if not g.kept:
            ctx.toast("Nothing was named, so there's no profile to save. The log is kept.")
            self.app.pop(self)
            return
        machine = dict(c.machine)
        active = profiles.active()
        base = f", built on {active.name}" if active is not None and active.id not in profiles.GENERIC.values() else ""
        opts = []
        target = observed.in_use_target(active)
        if target is not None:
            opts.append((target.label, ("in_use", "")))
        label = " ".join(str(machine.get(k, "")) for k in ("make", "model", "serial")).strip()
        if label:
            opts.append((f"New profile: {label}{base}", ("new", label)))
        stamp = time.strftime("%Y-%m-%d %H%M")
        opts.append((f"New profile: {self.script} {stamp}{base}", ("new", f"{self.script} {stamp}")))
        folder = profiles.user_dir()
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            path = os.path.join(folder, name)
            doc = observed.load_doc(path)
            if doc and (target is None or os.path.abspath(path) != os.path.abspath(target.path)):
                opts.append((f"Add to: {doc['name']}", ("add", path)))

        def go(what):
            how, value = what
            proto = ctx.protocol if ctx.protocol in profiles.GENERIC else profiles.J1939
            source = f"guided profile builder ({self.script}), log {os.path.basename(c.folder or '')}"
            bus = observed.bus_fingerprint([k for k, _n, _r in ctx.fieldwatch.streams()])
            emcy = g.emcy_names(known=set(active.emcy) if active is not None else set())
            try:
                if how == "add":
                    path = observed.save_profile(observed.build_profile("", g.entries(), proto, source, machine,
                                                                        base=observed.load_doc(value), bus=bus,
                                                                        emcy=emcy), value)
                elif how == "in_use":
                    path = observed.save_entries(g.entries(), active, protocol=proto, source=source, machine=machine,
                                                 bus=bus, emcy=emcy)
                else:
                    path = observed.save_entries(g.entries(), active, new_name=value, protocol=proto, source=source,
                                                 machine=machine, bus=bus, emcy=emcy)
            except (OSError, profiles.ProfileError) as e:
                ctx.toast(f"Couldn't save the profile: {e}")
                return
            ctx.apply_profile(os.path.splitext(os.path.basename(path))[0])
            ctx.toast(f"Saved {len(g.kept)} value(s) in '{ctx.profile_name()}': Live shows them now")
            ctx.share_profile(path)
            self.app.pop(self)
        choose(self.app, "Save the named values", opts, None, go)

    def softkeys(self):
        g = self.g
        if g.state == guide.DONE:
            return [SoftKey("Save", self.save), None, None,
                    SoftKey("Help", lambda: self.app.push(InfoDialog(self.app, self.title, HELP)))]
        if g.state == guide.RESULT:
            ticked = any(o.keep for o in g.offers)
            return [SoftKey("Keep, next" if ticked else "Next", self.keep), SoftKey("Again", g.again),
                    SoftKey("Skip", g.skip), SoftKey("Finish", self.finish)]
        busy = g.state != guide.WAITING
        return [SoftKey("Start", self.begin, enabled=not busy),
                SoftKey("Skip", g.skip, enabled=g.state in (guide.WAITING, guide.SETTLE)),
                SoftKey("Finish", self.finish, enabled=not busy),
                SoftKey("Help", lambda: self.app.push(InfoDialog(self.app, self.title, HELP)))]

    def on_action(self, action, n=1):
        self.g.touch()
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)
