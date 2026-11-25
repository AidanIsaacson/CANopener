"""Team access (core/team.py + the upload server's /api/team): invites, requests, approval,
installing the team plugin, updates, withdrawal. pip is replaced by a recorder; the plugin here
is a made-up package."""

import importlib.util
import json
import os
import stat
import subprocess
import threading
import time
import urllib.request
import zipfile

import pytest

from canopener.core import install, team

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYPROJECT = '[build-system]\nrequires = ["setuptools"]\n\n[project]\nname = "made-up-team-plugin"\nversion = "1"\n'


def _server_module():
    spec = importlib.util.spec_from_file_location("upload_server_team",
                                                  os.path.join(ROOT, "deploy", "upload-server", "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(tmp_path):
    mod = _server_module()
    cfg = mod.Config({"DATA_DIR": str(tmp_path / "srv"), "HOST": "127.0.0.1", "PORT": "0",
                      "UPLOAD_TOKENS": "up-token", "ADMIN_TOKENS": "admin-token", "TEAM_MAX_PENDING": "2"})
    srv = mod.make_server(cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    cfg.public_url = base
    yield base, mod
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A stand-in /etc/machine-id; write to it to 'move' to another computer."""
    path = tmp_path / "machine-id"
    path.write_text("aaaa1111\n")
    monkeypatch.setattr(install, "MACHINE_ID_FILES", (str(path),))
    return path


class Pip:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv[3:])
        return subprocess.CompletedProcess(argv, 0, "", "")

    def installs(self):
        return [c for c in self.calls if c[0] == "install"]

    def uninstalls(self):
        return [c for c in self.calls if c[0] == "uninstall"]


@pytest.fixture
def pip():
    return Pip()


def _plugin_zip(tmp_path, text="v1", top="plugin-main/") -> str:
    path = tmp_path / f"plugin-{text}.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(top + "pyproject.toml", PYPROJECT)
        z.writestr(top + "made_up_team_plugin/__init__.py", f"PLUGIN_NAME = 'Made-up {text}'\n")
    return str(path)


def _admin(server) -> team.Admin:
    return team.Admin(server[0], "admin-token")


def _published(server, tmp_path, text="v1") -> dict:
    return _admin(server).publish(_plugin_zip(tmp_path, text), text)


# ------------------------------------------------------------------------------- serial and ID

def test_serial_is_readable_and_the_server_agrees(server, machine):
    iid = install.install_id()
    s = install.serial()
    assert s.startswith("CO-") and len(s) == 12 and not set(s[3:]) & set("ILOU")
    assert install.install_id() == iid and install.serial(iid) == s
    assert server[1].serial(iid) == s
    assert install.normalize_serial(s.lower().replace("-", " ")) == s
    assert install.normalize_serial("CO-0000-000O") == "CO-0000-0000"      # O typed for 0
    assert install.normalize_serial("hello") == ""


def test_a_copied_config_folder_gets_its_own_id(machine):
    first = install.install_id()
    machine.write_text("bbbb2222\n")            # same folder, another computer
    second = install.install_id()
    assert second != first and install.install_id() == second


def test_an_old_install_without_a_fingerprint_keeps_its_id(machine):
    from canopener.core import config
    os.makedirs(config.config_dir(), exist_ok=True)
    with open(os.path.join(config.config_dir(), install.ID_FILE), "w") as f:
        f.write("0123456789abcdef0123456789abcdef\n")
    assert install.install_id() == "0123456789abcdef0123456789abcdef"
    assert install.install_id() == "0123456789abcdef0123456789abcdef"


# ------------------------------------------------------------------------------------- invites

def test_invite_joins_and_installs_the_plugin(server, tmp_path, machine, pip):
    pack = _published(server, tmp_path)
    inv = _admin(server).invite("Dave's laptop", days=3)
    assert inv["link"] == f"{server[0]}/invite/{inv['code']}"
    st = team.join(inv["link"], name="ignored, the invite names it", runner=pip)
    assert st.state == team.APPROVED and st.changed and st.version == "v1" and st.name == "Dave's laptop"
    assert pip.installs() == [["install", "--quiet", "-e", team.plugin_dir()]]
    assert os.path.exists(os.path.join(team.plugin_dir(), "made_up_team_plugin", "__init__.py"))
    assert stat.S_IMODE(os.stat(team.state_path()).st_mode) == 0o600
    saved = team.load_state()
    assert saved["dist"] == "made-up-team-plugin" and saved["sha256"] == pack["sha256"]
    row = _admin(server).find(install.serial())
    assert row["status"] == "approved" and row["name"] == "Dave's laptop" and "key_sha256" not in row
    # nothing new: a check changes nothing
    again = team.check(runner=pip)
    assert again.ok and not again.changed and not again.update and len(pip.installs()) == 1


def test_an_invite_works_once_and_expires(server, tmp_path, machine, pip):
    inv = _admin(server).invite("one", days=0.01)
    team.join(inv["link"], runner=pip)
    team.save_state(None)
    with pytest.raises(team.TeamError, match="used already"):
        team.join(inv["link"], runner=pip)
    with pytest.raises(team.TeamError, match="isn't an invite link"):
        team.join("XXXX-YYYY", runner=pip)
    old = _admin(server).invite("late", days=0.01)
    path = tmp_path / "srv" / "team" / "invites.json"
    invites = json.loads(path.read_text())
    invites[old["id"]]["expires"] = time.time() - 1
    path.write_text(json.dumps(invites))
    with pytest.raises(team.TeamError, match="expired"):
        team.join(old["link"], runner=pip)
    listed = _admin(server).invites()
    assert {i["id"] for i in listed} >= {inv["id"], old["id"]} and all("code" not in i and "code_sha256" not in i
                                                                      for i in listed)
    _admin(server).uninvite(old["id"])
    assert old["id"] not in {i["id"] for i in _admin(server).invites()}


def test_the_invite_page_explains_what_to_do(server):
    with urllib.request.urlopen(f"{server[0]}/invite/ABCD-EFGH-JKMN", timeout=10) as r:
        assert "Help &rarr; Team access" in r.read().decode()


# ----------------------------------------------------------------------- requests and approval

def test_request_waits_then_approval_installs(server, tmp_path, machine, pip):
    _published(server, tmp_path)
    st = team.request(server[0], name="Shop PC")
    assert st.state == team.PENDING and install.serial() in st.message
    assert team.check(runner=pip).state == team.PENDING and not pip.calls
    _admin(server).set(install.serial(), status="approved")
    st = team.check(runner=pip)
    assert st.state == team.APPROVED and st.changed and len(pip.installs()) == 1


def test_requests_are_capped(server, machine):
    mod_team = server[1]
    for n in range(2):
        body = {"install": f"{n:032x}", "key": "k" * 40, "name": f"spam {n}"}
        req = urllib.request.Request(f"{server[0]}/api/team/requests", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
    with pytest.raises(team.TeamError, match="isn't taking requests"):
        team.request(server[0], "one too many")
    assert mod_team.serial("0" * 32) == "CO-0000-0000"


def test_admin_calls_need_the_admin_token(server):
    for method, path in (("GET", "/api/team/installs"), ("POST", "/api/team/invites"), ("GET", "/api/team/pack")):
        req = urllib.request.Request(server[0] + path, method=method, data=b"{}" if method == "POST" else None,
                                     headers={"Authorization": "Bearer up-token", "Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        assert e.value.code == 403
    with pytest.raises(team.TeamError, match="must be a zip"):
        _admin(server)._do("PUT", "/api/team/pack", raw=PYPROJECT.encode())
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("deep/er/pyproject.toml", PYPROJECT)
    with pytest.raises(team.TeamError, match="no pyproject.toml"):
        _admin(server)._do("PUT", "/api/team/pack", raw=buf.getvalue())


# --------------------------------------------------------------------------------------- updates

def test_a_new_build_waits_for_a_yes(server, tmp_path, machine, pip):
    _published(server, tmp_path, "v1")
    team.join(_admin(server).invite("x")["link"], runner=pip)
    _published(server, tmp_path, "v2")
    st = team.check(runner=pip)
    assert st.update == "v2" and not st.changed and len(pip.installs()) == 1
    st = team.install_update(runner=pip)
    assert st.changed and st.version == "v2" and len(pip.installs()) == 2
    assert "Made-up v2" in open(os.path.join(team.plugin_dir(), "made_up_team_plugin", "__init__.py")).read()
    assert _admin(server).find(install.serial())["pack_version"] == "v1"      # reported at the check before
    team.check(runner=pip)
    assert _admin(server).find(install.serial())["pack_version"] == "v2"


def test_a_bad_download_changes_nothing(server, tmp_path, machine, pip, monkeypatch):
    _published(server, tmp_path, "v1")
    team.join(_admin(server).invite("x")["link"], runner=pip)
    before = team.load_state()
    _published(server, tmp_path, "v2")
    real = team.http

    def tamper(method, url, body=None, headers=None, **kw):
        status, raw = real(method, url, body, headers, **kw)
        if kw.get("dest"):
            with open(kw["dest"], "ab") as f:
                f.write(b"junk")
        return status, raw
    monkeypatch.setattr(team, "http", tamper)
    st = team.install_update(runner=pip)
    assert not st.ok and "checksum" in st.message and team.load_state()["sha256"] == before["sha256"]
    assert len(pip.installs()) == 1


def test_an_unsafe_zip_is_refused(tmp_path):
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("pyproject.toml", PYPROJECT)
        z.writestr("../../escape.txt", "no")
    with pytest.raises(team.TeamError, match="unsafe path"):
        team._extract(str(path), str(tmp_path / "out"))
    assert not (tmp_path / "escape.txt").exists()


def test_a_plugin_already_installed_another_way_is_left_alone(server, tmp_path, machine, pip, monkeypatch):
    _published(server, tmp_path)
    monkeypatch.setattr(team, "_installed_elsewhere", lambda name: True)
    st = team.join(_admin(server).invite("x")["link"], runner=pip)
    assert not st.ok and "another way" in st.message and not pip.installs()


# ------------------------------------------------------------------------------------ withdrawal

@pytest.mark.parametrize("how", ["revoke", "forget"])
def test_withdrawing_removes_the_plugin(server, tmp_path, machine, pip, how):
    _published(server, tmp_path)
    team.join(_admin(server).invite("x")["link"], runner=pip)
    if how == "revoke":
        _admin(server).set(install.serial(), status="revoked")
    else:
        _admin(server).forget(install.serial())
    st = team.check(runner=pip)
    assert st.state == team.WITHDRAWN and st.changed and "restart" in st.message
    assert pip.uninstalls() == [["uninstall", "--yes", "made-up-team-plugin"]]
    assert not os.path.exists(team.plugin_dir())
    assert team.check(runner=pip).state == team.WITHDRAWN and len(pip.uninstalls()) == 1
    if how == "revoke":             # changed your mind: approving it again brings the plugin back
        _admin(server).set(install.serial(), status="approved")
        st = team.check(runner=pip)
        assert st.state == team.APPROVED and st.changed and len(pip.installs()) == 2


def test_another_computer_with_the_same_key_is_turned_away(server, tmp_path, machine, pip, monkeypatch):
    _published(server, tmp_path)
    team.join(_admin(server).invite("x")["link"], runner=pip)
    st = team.load_state()
    monkeypatch.setattr(install, "machine", lambda: "ffffffffffffffff")
    monkeypatch.setattr(install, "install_id", lambda: st["install"])      # a copy that kept the ID too
    assert team.check(runner=pip).state == team.WITHDRAWN and pip.uninstalls()


def test_offline_keeps_what_is_installed(server, tmp_path, machine, pip, monkeypatch):
    _published(server, tmp_path)
    team.join(_admin(server).invite("x")["link"], runner=pip)

    def down(*a, **kw):
        raise team.TeamError("Couldn't reach the server (no internet?)", offline=True)
    monkeypatch.setattr(team, "http", down)
    st = team.check(runner=pip)
    assert not st.ok and st.state == team.APPROVED and st.version == "v1" and not pip.uninstalls()
    assert os.path.exists(team.plugin_dir())


def test_leave_removes_everything(server, tmp_path, machine, pip):
    _published(server, tmp_path)
    team.join(_admin(server).invite("x")["link"], runner=pip)
    st = team.leave(runner=pip)
    assert st.state == team.NONE and st.changed and not os.path.exists(team.state_path())
    assert not os.path.exists(team.plugin_dir())


def test_https_is_required_except_locally():
    with pytest.raises(team.TeamError, match="https"):
        team.request("http://logs.example.com", "x")


# ------------------------------------------------------------------------------------ publishing

def test_make_bundle_from_a_folder_and_a_git_checkout(tmp_path):
    src = tmp_path / "plugin"
    (src / "pkg" / "__pycache__").mkdir(parents=True)
    (src / "pyproject.toml").write_text(PYPROJECT)
    (src / "pkg" / "__init__.py").write_text("")
    (src / "pkg" / "__pycache__" / "x.pyc").write_text("")
    out = tmp_path / "out"
    out.mkdir()
    path, version, _ = team.make_bundle(str(src), str(out), version="1.2")
    assert version == "1.2" and sorted(zipfile.ZipFile(path).namelist()) == ["pkg/__init__.py", "pyproject.toml"]
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for args in (["init", "-q"], ["add", "pyproject.toml", "pkg/__init__.py"], ["commit", "-qm", "one"]):
        subprocess.run(["git", "-C", str(src), *args], check=True, env=env)
    (src / "pyproject.toml").write_text(PYPROJECT + "# local edit\n")
    path, version, note = team.make_bundle(str(src), str(out))
    assert version and "Uncommitted" in note
    assert "local edit" not in zipfile.ZipFile(path).read("pyproject.toml").decode()
    with pytest.raises(team.TeamError, match="pyproject"):
        team.make_bundle(str(tmp_path), str(out))


def test_a_failed_uninstall_is_tried_again_at_the_next_check(server, tmp_path, machine, pip):
    _published(server, tmp_path)
    team.join(_admin(server).invite("x")["link"], runner=pip)
    _admin(server).set(install.serial(), status="revoked")

    def failing(argv, **kw):
        pip.calls.append(argv[3:])
        return subprocess.CompletedProcess(argv, 1, "", "pip broke")
    st = team.check(runner=failing)
    assert st.state == team.WITHDRAWN and "tries again" in st.message and not os.path.exists(team.plugin_dir())
    team.check(runner=pip)
    assert len(pip.uninstalls()) == 2 and "dist" not in team.load_state()


# ------------------------------------------------------------------------------------- desktop

def _pump(app, seconds, until=None):
    end = time.time() + seconds
    while time.time() < end and not (until and until()):
        app.processEvents()
        time.sleep(0.02)


def test_desktop_team_dialog_and_startup_withdrawal(server, tmp_path, machine, pip, monkeypatch):
    qt = pytest.importorskip("canopener.gui.qt", exc_type=ImportError)
    from canopener.core import startup_check, updates
    from canopener.gui.main_window import MainWindow
    from canopener.gui.team_dialog import TeamDialog
    app = qt.QtWidgets.QApplication.instance() or qt.QtWidgets.QApplication([])
    monkeypatch.setattr(team, "_pip", lambda args, python, runner: (pip(["py", "-m", "pip", *args]).returncode == 0,
                                                                    ""))       # the GUI uses the real runner
    _published(server, tmp_path)
    restarts = []
    dlg = TeamDialog(lambda: restarts.append(1))
    assert dlg.serial.text() == install.serial() and dlg.join_box.isVisibleTo(dlg)
    dlg.join()                                                  # nothing pasted
    assert "Paste the invite link" in dlg.details.text()
    dlg.link.setText(_admin(server).invite("Shop laptop")["link"])
    dlg.join()
    _pump(app, 10, lambda: not dlg._busy)
    assert dlg.headline.text() == "Approved." and "Shop laptop" in dlg.details.text()
    assert not dlg.join_box.isVisibleTo(dlg) and dlg.restart_btn.isVisibleTo(dlg) and pip.installs()
    dlg._restart()
    assert restarts == [1]

    # Revoked on the server: the next start removes the plugin without asking, and says so.
    _admin(server).set(install.serial(), status="revoked")
    monkeypatch.setattr(updates, "repo_root", lambda: None)
    told = []
    monkeypatch.setattr(qt.QtWidgets.QMessageBox, "information", lambda *a: told.append(a[1:]))
    w = MainWindow()
    found = startup_check.check(w.core_settings)
    assert found.withdrawn and not found.any and not os.path.exists(team.plugin_dir())
    w._show_startup_found(found)
    assert told and told[0][0] == "Team access withdrawn" and w._found_dialog is None
    w.close()


def test_startup_check_finds_everything_and_installs_only_when_told(server, tmp_path, machine, pip, monkeypatch):
    from canopener.core import plugin_repos, startup_check, updates
    from canopener.core.settings import Settings
    s = Settings(str(tmp_path / "settings.json"))
    plugin_repos.add_source(s, "someone/a-plugin")
    applied = []
    monkeypatch.setattr(updates, "repo_root", lambda: "/app")
    monkeypatch.setattr(updates, "check", lambda: updates.UpdateStatus(True, "new", latest="9.9", behind=2,
                                                                       commits=["Faster", "Fixed"]))
    monkeypatch.setattr(updates, "apply", lambda progress=None: applied.append("app") or (True, "App updated."))
    monkeypatch.setattr(plugin_repos, "check", lambda src, token: plugin_repos.PluginStatus(
        src.repo, src.path, True, "", True, behind=1, commits=["New screen"]))
    monkeypatch.setattr(plugin_repos, "update", lambda src, token, progress=None: applied.append(src.repo)
                        or (True, "Plugin updated."))
    monkeypatch.setattr(team, "_pip", lambda args, python, runner: (pip(["py", "-m", "pip", *args]).returncode == 0,
                                                                    ""))
    _published(server, tmp_path, "v1")
    team.join(_admin(server).invite("x")["link"])
    _published(server, tmp_path, "v2")

    off = startup_check.check(s, updates_too=False)
    assert off.team.update == "v2" and not off.app and not off.plugins
    found = startup_check.check(s)
    assert found.any and not applied and len(pip.installs()) == 1           # looked, installed nothing
    text = "\n".join(found.lines())
    assert "CANopener 9.9" in text and "someone/a-plugin" in text and "Team plugin v2 (you have v1)" in text
    ok, report = startup_check.apply(found)
    assert ok and applied == ["app", "someone/a-plugin"] and len(pip.installs()) == 2
    assert "App updated." in report and "Team plugin v2 installed" in report


def test_startup_check_offline_says_nothing(tmp_path, monkeypatch):
    from canopener.core import startup_check, updates
    from canopener.core.settings import Settings
    monkeypatch.setattr(updates, "repo_root", lambda: "/app")

    def boom(*a, **k):
        raise OSError("no network")
    monkeypatch.setattr(updates, "check", boom)
    found = startup_check.check(Settings(str(tmp_path / "s.json")))
    assert not found.any and found.team is None
