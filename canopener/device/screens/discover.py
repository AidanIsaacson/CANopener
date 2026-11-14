"""What changed? On a saved log: the bytes and bits that followed what you did to the machine,
named from a short list and saved as a profile (core.changes and core.observed do the work)."""

import os
import time

from ...core import observed, profiles
from .base import InfoDialog, Row, RowList, Screen, SoftKey, choose

HELP = ["Tag a timestamp each time you change one thing on the machine (key on, start, lift, tilt...). "
        "This compares what the bus said before and after each timestamp and lists what followed.",
        "", "Pick a line to name it. Named values are saved as a profile, and Live shows them straight away.",
        "", "Counters, checksums and the scan tool's own questions are left out."]


class DiscoverScreen(Screen):
    title = "What changed"

    def __init__(self, app, report, source: str = "", machine: dict | None = None):
        super().__init__(app)
        self.report, self.source, self.machine = report, source, machine or {}
        self.show_known = False
        self.cands = observed.candidates(report)
        self.picked: dict[str, dict] = {}       # candidate key -> its profile entry
        self.rows = RowList(self._rows)

    def _rows(self):
        r = self.report
        phases = "  >  ".join(p.label for p in r.phases)
        rows = [Row(f"{r.frames} frames, {len(r.phases)} phases", kind="info", hint=phases)]
        if len(r.phases) < 2:
            rows.append(Row("Nothing to compare: this log has no timestamps", kind="info",
                            hint="Tag one each time you change something on the machine, then come back."))
            return rows
        cands = observed.candidates(r, include_known=True) if self.show_known else self.cands
        rows.append(Row(f"{len(cands)} thing(s) changed" + (" (named ones included)" if self.show_known else ""),
                        kind="header"))
        for c in cands:
            entry = self.picked.get(c.key)
            name = entry["name"] if entry else (c.field.known or (c.suggestion().get("name", "") + "?"
                                                                  if c.suggestion() else ""))
            rows.append(Row(c.key, name, select=lambda c=c: self.name_it(c), hint=c.evidence[0],
                            check=(lambda c=c: c.key in self.picked) if not c.field.known else None))
        if not cands:
            rows.append(Row("Nothing stood out between the timestamps", kind="info"))
        return rows

    # --- naming one ---
    def name_it(self, cand):
        if cand.field.known:
            self.app.push(InfoDialog(self.app, cand.key, [f"Already named: {cand.field.known}", ""] + cand.evidence))
            return
        hint = cand.suggestion()
        opts = []
        if hint:
            opts.append((f"{hint['name']} ({hint['unit']}): it follows that value", ("use", hint)))
        opts += [("Pick a name...", ("pick", None)), ("What was seen", ("why", None))]
        if cand.key in self.picked:
            opts.append(("Don't keep this one", ("drop", None)))

        def chosen(what):
            how, value = what
            if how == "use":
                self.keep(cand, **value)
            elif how == "pick":
                self.pick_group(cand)
            elif how == "why":
                lines = list(cand.evidence)
                if cand.follows is not None:
                    lines += ["", cand.follows.text()]
                self.app.push(InfoDialog(self.app, cand.key, lines))
            else:
                self.picked.pop(cand.key, None)
        choose(self.app, cand.key, opts, None, chosen)

    def pick_group(self, cand):
        groups = list(dict.fromkeys(g for g, _n, _u in observed.GENERIC_NAMES))
        choose(self.app, "What part of the machine?", [(g, g) for g in groups], None,
               lambda g: self.pick_name(cand, g))

    def pick_name(self, cand, group):
        names = [(n + (f" ({u})" if u else ""), (n, u)) for g, n, u in observed.GENERIC_NAMES if g == group]
        hint = cand.suggestion()

        def chosen(value):
            name, unit = value
            same = hint and hint.get("unit") == unit
            self.keep(cand, name, unit, hint["scale"] if same else 1.0, hint["offset"] if same else 0.0, group)
        choose(self.app, group, names, None, chosen)

    def keep(self, cand, name, unit="", scale=1.0, offset=0.0, group="Observed"):
        taken = {e["name"] for k, e in self.picked.items() if k != cand.key}
        n, base = 2, name
        while name in taken:                        # two lift switches: "Lift", "Lift 2"
            name, n = f"{base} {n}", n + 1
        self.picked[cand.key] = observed.signal_entry(cand, name, unit, scale, offset, group)
        self.ctx.toast(f"{cand.key} = {name}")

    # --- saving ---
    def save(self):
        if not self.picked:
            self.ctx.toast("Name at least one value first")
            return
        machine = " ".join(str(self.machine.get(k, "")) for k in ("make", "model", "serial")).strip()
        opts = []
        if machine:
            opts.append((f"New profile: {machine}", ("new", machine)))
        opts.append(("New profile: Observed " + time.strftime("%Y-%m-%d %H:%M"),
                     ("new", "Observed " + time.strftime("%Y-%m-%d %H%M"))))
        folder = profiles.user_dir()
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            doc = observed.load_doc(os.path.join(folder, name))
            if doc:
                opts.append((f"Add to: {doc['name']}", ("add", os.path.join(folder, name))))

        def go(what):
            how, value = what
            entries = list(self.picked.values())
            proto = observed.protocol_of(self.report)
            try:
                if how == "add":
                    doc = observed.build_profile("", entries, proto, self.source, self.machine,
                                                 base=observed.load_doc(value))
                    path = observed.save_profile(doc, value)
                else:
                    path = observed.save_profile(observed.build_profile(value, entries, proto, self.source,
                                                                        self.machine))
            except (OSError, profiles.ProfileError) as e:
                self.ctx.toast(f"Couldn't save the profile: {e}")
                return
            pid = os.path.splitext(os.path.basename(path))[0]
            self.ctx.apply_profile(pid)
            self.ctx.toast(f"Saved {len(entries)} value(s) as '{self.ctx.profile_name()}': Live shows them now")
            self.picked.clear()
        choose(self.app, "Save as a profile", opts, None, go)

    def softkeys(self):
        def flip():
            self.show_known = not self.show_known
        return [SoftKey("Back", lambda: self.app.pop(self)),
                SoftKey("Hide named" if self.show_known else "Show named", flip),
                SoftKey(f"Save ({len(self.picked)})", self.save, enabled=bool(self.picked)),
                SoftKey("Help", lambda: self.app.push(InfoDialog(self.app, "What changed", HELP)))]

    def on_action(self, action, n=1):
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)
