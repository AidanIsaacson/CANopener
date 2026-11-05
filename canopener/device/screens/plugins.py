"""System > Software > Plugins: sign in to GitHub and install / update plugins from repositories, with no
keyboard. The work is ``core.github`` (sign-in) and ``core.plugin_repos`` (git + pip).

Sign-in shows a short code; the user opens github.com/login/device on a phone, types the code
there and approves. The handheld only waits.
"""

import threading
import time

import pygame

from ... import plugins
from ...core import github, plugin_repos
from .. import input as inp
from .base import InfoDialog, Row, RowList, Screen, SoftKey, choose, confirm


def _token():
    acct = github.load_account()
    return acct.token if acct else None


def offer_restart(app, msg: str):
    def restart():
        app.restart = True
        app.running = False
    confirm(app, "Restart now?", msg + "\n\nPlugins load when CANopener starts. A recording in progress is "
            "stopped and saved first.", "Restart", restart, danger=False)


class PluginsScreen(Screen):
    title = "Plugins"

    def __init__(self, app):
        super().__init__(app)
        self.status: dict[str, str] = {}        # repo -> last check / install result
        self.rows = RowList(self._rows)

    def _rows(self):
        acct = github.load_account()
        rows = [Row("GitHub", kind="header")]
        if acct:
            rows += [Row("Signed in as", acct.login or "(signed in)", kind="info"),
                     Row("Sign out", select=self._sign_out, hint="forget the sign-in on this device")]
        else:
            rows.append(Row("Sign in to GitHub", select=lambda: self.app.push(SignInScreen(self.app)),
                            hint="approve it on your phone: nothing to type here"))
        rows.append(Row("Plugin repositories", kind="header"))
        srcs = plugin_repos.sources(self.ctx.settings)
        for src in srcs:
            rows.append(Row(src.repo, lambda src=src: self.status.get(src.repo, ""),
                            select=lambda src=src: self._source_menu(src), hint=src.path))
        rows.append(Row("Add a repository", select=self._add, hint="pick from the ones your sign-in can read"))
        if srcs:
            rows += [Row("Check all for updates", select=self._check_all),
                     Row("Update all", select=self._update_all, hint="download and install, then restart")]
        rows.append(Row("Loaded now", kind="header"))
        found = plugins.discover()
        if not found:
            rows.append(Row("None", kind="info"))
        for p in found:
            rows.append(Row(p.name or p.id, p.version if not p.error else "failed to load", kind="info",
                            level=(lambda: "bad") if p.error else (lambda: "ok"), hint=p.error or ""))
        return rows

    # --- sign-in ---
    def _sign_out(self):
        confirm(self.app, "Sign out of GitHub?", "This device forgets the sign-in. Installed plugins stay; "
                "updating a private one needs signing in again. (To cut it off from GitHub's side as well: "
                "github.com > Settings > Applications.)", "Sign out", github.sign_out)

    # --- repositories ---
    def _add(self):
        token = _token()
        if not token:
            self.ctx.toast("Sign in to GitHub first")
            return
        self.ctx.run_job("Listing repositories", lambda: github.repositories(token), self._pick_repo)

    def _pick_repo(self, names: list):
        have = {s.repo.lower() for s in plugin_repos.sources(self.ctx.settings)}
        options = [(n, n) for n in names if n.lower() not in have]
        if not options:
            self.app.push(InfoDialog(self.app, "Add a repository", [
                "Your sign-in can't read any repositories that aren't on the list already.",
                "", "On GitHub, install the CANopener GitHub App on the plugin's repository "
                "(github.com > Settings > Applications > CANopener > Configure), then try again."]))
            return
        choose(self.app, "Install which?", options, None, self._confirm_add)

    def _confirm_add(self, repo: str):
        src = plugin_repos.Source(repo)
        confirm(self.app, f"Install {repo}?", f"Downloads it to {src.path} and installs it into CANopener. "
                "Needs internet; reading the bus carries on meanwhile.", "Install",
                lambda: self._do_add(repo), danger=False)

    def _do_add(self, repo: str):
        src = plugin_repos.add_source(self.ctx.settings, repo)
        self._run("Installing " + repo, lambda: plugin_repos.install(src, _token()), src.repo)

    def _source_menu(self, src):
        def pick(what):
            if what == "check":
                self.ctx.run_job("Checking " + src.repo, lambda: plugin_repos.check(src, _token()), self._checked)
            elif what == "update":
                self._run("Updating " + src.repo, lambda: plugin_repos.update(src, _token()), src.repo)
            elif what == "remove":
                confirm(self.app, f"Remove {src.repo}?", f"It comes off this list. Its folder ({src.path}) and "
                        "the installed plugin stay as they are.", "Remove",
                        lambda: plugin_repos.remove_source(self.ctx.settings, src.repo))
        choose(self.app, src.repo, [("Check for updates", "check"), ("Install / update now", "update"),
                                    ("Remove from the list", "remove")], None, pick)

    def _checked(self, st):
        self.status[st.repo] = st.message
        lines = [st.message] + ([""] + [f"- {c}" for c in st.commits[:15]] if st.commits else [])
        keys = [SoftKey("Update", lambda: (self.app.pop(), self._run(
            "Updating " + st.repo, lambda: plugin_repos.update(self._src(st.repo), _token()), st.repo)))
                ] if st.available else []
        self.app.push(InfoDialog(self.app, st.repo, lines, keys=keys))

    def _src(self, repo):
        return next(s for s in plugin_repos.sources(self.ctx.settings) if s.repo == repo)

    def _check_all(self):
        srcs, token = plugin_repos.sources(self.ctx.settings), _token()
        self.ctx.run_job("Checking plugins", lambda: [plugin_repos.check(s, token) for s in srcs], self._all_checked)

    def _all_checked(self, results):
        lines = []
        for st in results:
            self.status[st.repo] = st.message
            lines += [st.repo, "  " + st.message] + [f"  - {c}" for c in st.commits[:8]] + [""]
        keys = [SoftKey("Update all", lambda: (self.app.pop(), self._update_all()))] if any(
            st.available or (st.ok and not st.installed) for st in results) else []
        self.app.push(InfoDialog(self.app, "Plugins", lines, keys=keys))

    def _update_all(self):
        srcs, token = plugin_repos.sources(self.ctx.settings), _token()

        def work():
            res = [(s.repo, *plugin_repos.update(s, token)) for s in srcs]
            changed = any(ok and "up to date" not in msg for _, ok, msg in res)
            return all(ok for _, ok, _ in res), "\n\n".join(f"{r}: {m}" for r, _, m in res), changed
        self.ctx.run_job("Updating plugins", work, lambda r: self._done(r[0], r[1], restart=r[2]))

    def _run(self, label, fn, repo):
        def done(result):
            ok, msg = result
            self.status[repo] = msg.splitlines()[0] if msg else ""
            self._done(ok, msg, restart=ok and "up to date" not in msg)
        self.ctx.run_job(label, fn, done)

    def _done(self, ok, msg, restart):
        if ok and restart:
            offer_restart(self.app, msg)
        else:
            self.app.push(InfoDialog(self.app, "Plugins" if ok else "Didn't work", msg.splitlines()))

    def on_action(self, action, n=1):
        return self.rows.on_action(action, n)

    def draw(self, ui, rect):
        self.rows.draw(ui, rect)


class SignInScreen(Screen):
    """Shows github.com/login/device and a code, and waits for the user to approve it elsewhere."""

    title = "Sign in to GitHub"

    def __init__(self, app, client_id: str | None = None):
        super().__init__(app)
        self.client_id = client_id if client_id is not None else github.client_id(self.ctx.settings)
        self.code: github.DeviceCode | None = None
        self.error: str | None = None
        self.done_as: str | None = None
        self._cancel = threading.Event()
        self._thread = None

    def on_enter(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._work, name="github-sign-in", daemon=True)
            self._thread.start()

    def on_leave(self):
        self._cancel.set()

    def _work(self):
        try:
            self.code = github.start_sign_in(self.client_id)
            token = github.wait_for_token(self.client_id, self.code, cancelled=self._cancel.is_set)
            if token is None:
                return
            login = github.whoami(token)
            github.save_account(github.Account(token, login))
            self.done_as = login or "GitHub"
        except github.GitHubError as e:
            self.error = str(e)
        except Exception as e:      # noqa: BLE001 - shown on the screen
            self.error = f"Sign-in failed: {e}"

    def _restart(self):
        self._cancel.set()
        self._cancel = threading.Event()
        self.code, self.error, self.done_as, self._thread = None, None, None, None
        self.on_enter()

    def softkeys(self):
        if self.done_as:
            return [SoftKey("Done", lambda: self.app.pop(self))]
        keys = [SoftKey("Cancel", lambda: self.app.pop(self))]
        if self.error:
            keys.append(SoftKey("Try again", self._restart))
        return keys

    def on_action(self, action, n=1):
        if action in (inp.BACK, inp.SELECT) and (self.done_as or action == inp.BACK):
            self.app.pop(self)
            return True
        return False

    def draw(self, ui, rect):
        t = ui.theme
        x, w = rect.x + t.pad * 3, rect.w - t.pad * 6
        y = rect.y + t.pad * 3
        if self.done_as:
            ui.text(f"Signed in as {self.done_as}", (rect.centerx, rect.centery - t.touch), "title", "ok",
                    align="center", max_w=w)
            ui.paragraph("Next: Add a repository, on the Plugins page.",
                         pygame.Rect(x, rect.centery, w, t.touch * 2), "body", "dim")
            return
        if self.error:
            ui.text("Couldn't sign in", (x, y), "big", "bad", valign="top")
            ui.paragraph(self.error, pygame.Rect(x, y + t.touch, w, rect.h - t.touch * 2), "body", "text")
            return
        if self.code is None:
            ui.text("Asking GitHub for a code…", (rect.centerx, rect.centery), "big", "dim", align="center")
            return
        ui.text("On your phone or computer, open", (x, y), "body", "dim", valign="top")
        y += t.font("body").get_linesize() + t.pad
        url = self.code.verification_uri.removeprefix("https://")
        ui.text(url, (x, y), "title", "accent", valign="top", max_w=w)
        y += t.font("title").get_linesize() + t.pad * 2
        ui.text("and enter this code:", (x, y), "body", "dim", valign="top")
        y += t.font("body").get_linesize() + t.pad
        ui.text(self.code.user_code, (rect.centerx, y), "huge", "text", align="center", valign="top")
        y += t.font("huge").get_linesize() + t.pad * 2
        left = max(0, int(self.code.expires_at - time.monotonic()))
        ui.text(f"Waiting for you to approve it…  (code good for {left // 60}:{left % 60:02d})",
                (x, y), "small", "dim", valign="top", max_w=w)
