"""The check a front-end runs when it starts: is anything newer, and is team access still on?

Three things are looked at, all in one background call (``check``):

* **Team access** (``core.team``), always, whatever the update setting: a computer whose access was
  withdrawn loses the team plugin here, without being asked, and the first install after approval
  happens here too. A newer build of the plugin is only reported.
* **The app itself** (``core.updates``, git), when update checks are on (the default).
* **Plugins from GitHub** (``core.plugin_repos``), the same.

Nothing is installed by ``check`` except as above: the front-end shows what it found and calls
``apply`` only when the user says yes. Offline (the usual case in the field) every part fails
quietly and ``Found.any`` is False. Standard library only.
"""

from dataclasses import dataclass, field

from . import plugin_repos, team, updates


@dataclass
class Found:
    app: "updates.UpdateStatus | None" = None                  # set only when an update is there
    plugins: list = field(default_factory=list)                 # [(Source, PluginStatus)] with updates
    team: "team.Status | None" = None                           # the team check's result (when joined)

    @property
    def team_update(self) -> str:
        return self.team.update if self.team is not None else ""

    @property
    def withdrawn(self) -> bool:
        """Team access was just withdrawn and the plugin removed: say so (no question to ask)."""
        return self.team is not None and self.team.state == team.WITHDRAWN and self.team.changed

    @property
    def any(self) -> bool:
        return bool(self.app or self.plugins or self.team_update)

    def lines(self) -> list[str]:
        out = []
        if self.app:
            out.append(f"CANopener {self.app.latest or 'update'} (you have {self.app.current})")
            out += [f"    • {c}" for c in self.app.commits[:8]]
            if len(self.app.commits) > 8:
                out.append(f"    … and {len(self.app.commits) - 8} more")
        for src, st in self.plugins:
            out.append(f"Plugin {src.repo}: {st.behind} change(s)")
            out += [f"    • {c}" for c in st.commits[:5]]
        if self.team_update:
            out.append(f"Team plugin {self.team_update}" + (f" (you have {self.team.version})"
                                                            if self.team.version else ""))
        return out


def _token():
    from . import github
    acct = github.load_account()
    return acct.token if acct else None


def check(settings, updates_too: bool = True) -> Found:
    """Look; changes nothing but team access (see the module docstring). Never raises."""
    f = Found()
    try:
        if team.enrolled():
            f.team = team.check()
    except Exception:           # noqa: BLE001 - a startup check must never stop the app
        pass
    if not updates_too:
        return f
    try:
        if updates.repo_root():
            s = updates.check()
            if s.available and not s.ahead:
                f.app = s
    except Exception:           # noqa: BLE001
        pass
    try:
        srcs = plugin_repos.sources(settings)
        token = _token() if srcs else None
        for src in srcs:
            st = plugin_repos.check(src, token)
            if st.available:
                f.plugins.append((src, st))
    except Exception:           # noqa: BLE001
        pass
    return f


def apply(found: Found, progress=None) -> tuple[bool, str]:
    """Install everything ``found`` lists (after the user said yes). ``(all ok, report)``."""
    ok, report = True, []
    if found.app:
        good, msg = updates.apply(progress=progress)
        ok &= good
        report.append(msg)
        if good and found.app.installer_changed:
            report.append("This update also changes the system setup: run the installer once in a terminal:\n  "
                          + updates.installer_command())
    if found.plugins:
        token = _token()
        for src, _st in found.plugins:
            good, msg = plugin_repos.update(src, token, progress=progress)
            ok &= good
            report.append(msg)
    if found.team_update:
        st = team.install_update(progress=progress)
        ok &= st.ok
        report.append(st.message)
    return ok, "\n\n".join(r for r in report if r)
