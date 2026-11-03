"""GitHub device-flow sign-in (fake GitHub) and plugin installs / updates (real throwaway git repos)."""

import os
import stat
import subprocess
import sys

import pytest

from canopener.core import github, plugin_repos
from canopener.core.settings import Settings

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}


# --- sign-in ---------------------------------------------------------------------------------

class FakeGitHub:
    def __init__(self, token_replies):
        self.token_replies = list(token_replies)
        self.calls = []

    def __call__(self, method, url, data=None, headers=None):
        self.calls.append((method, url, data, headers))
        if url == github.DEVICE_CODE_URL:
            return 200, {"device_code": "dev123", "user_code": "WDJB-MJHT", "interval": 5,
                         "verification_uri": "https://github.com/login/device", "expires_in": 900}
        if url == github.TOKEN_URL:
            return 200, self.token_replies.pop(0)
        if url.endswith("/user"):
            return (200, {"login": "tester"}) if headers.get("Authorization") == "Bearer ghu_ok" else (401, {})
        if "/user/installations?" in url:
            return 200, {"installations": [{"id": 7}]}
        if "/user/installations/7/repositories" in url:
            page = int(url.rsplit("page=", 1)[1])
            n = 100 if page == 1 else 3
            return 200, {"repositories": [{"full_name": f"me/repo{page}-{i}"} for i in range(n)]}
        return 404, {"message": "Not Found"}


def test_device_flow_waits_slows_down_and_returns_the_token(monkeypatch):
    fake = FakeGitHub([{"error": "authorization_pending"}, {"error": "slow_down", "interval": 10},
                       {"access_token": "ghu_ok", "token_type": "bearer"}])
    monkeypatch.setattr(github, "http", fake)
    slept = []
    dc = github.start_sign_in("Iv1.abc", clock=lambda: 0)
    assert dc.user_code == "WDJB-MJHT" and fake.calls[0][2] == {"client_id": "Iv1.abc"}
    token = github.wait_for_token("Iv1.abc", dc, sleep=slept.append, clock=lambda: 1)
    assert token == "ghu_ok" and dc.interval == 10
    assert sum(slept) == 5 + 5 + 10
    assert fake.calls[1][2]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"


@pytest.mark.parametrize("reply,text", [({"error": "access_denied"}, "cancelled"),
                                        ({"error": "expired_token"}, "expired")])
def test_device_flow_errors_are_worded_for_the_user(monkeypatch, reply, text):
    monkeypatch.setattr(github, "http", FakeGitHub([reply]))
    dc = github.start_sign_in("Iv1.abc", clock=lambda: 0)
    with pytest.raises(github.GitHubError, match=text):
        github.wait_for_token("Iv1.abc", dc, sleep=lambda s: None, clock=lambda: 1)


def test_cancel_stops_waiting_and_no_client_id_refuses(monkeypatch):
    monkeypatch.setattr(github, "http", FakeGitHub([]))
    dc = github.start_sign_in("Iv1.abc", clock=lambda: 0)
    assert github.wait_for_token("Iv1.abc", dc, cancelled=lambda: True) is None
    with pytest.raises(github.GitHubError, match="client ID"):
        github.start_sign_in("")


def test_client_id_from_env_then_settings(monkeypatch):
    s = Settings()
    assert github.client_id(s) == github.DEFAULT_CLIENT_ID
    s.set("github.client_id", "Iv1.settings")
    assert github.client_id(s) == "Iv1.settings"
    monkeypatch.setenv("CANOPENER_GITHUB_CLIENT_ID", "Iv1.env")
    assert github.client_id(s) == "Iv1.env"


def test_token_is_saved_private_and_forgotten():
    assert github.load_account() is None
    github.save_account(github.Account("ghu_ok", "tester"))
    assert stat.S_IMODE(os.stat(github.token_path()).st_mode) == 0o600
    assert github.load_account().login == "tester"
    assert github.sign_out() and github.load_account() is None and not github.sign_out()


def test_api_whoami_repositories_and_expired_sign_in(monkeypatch):
    monkeypatch.setattr(github, "http", FakeGitHub([]))
    assert github.whoami("ghu_ok") == "tester"
    names = github.repositories("ghu_ok")
    assert len(names) == 103 and names[0] == "me/repo1-0"
    with pytest.raises(github.SignInNeeded):
        github.whoami("ghu_revoked")


def test_git_env_carries_the_token_only_in_the_environment():
    assert github.git_env(None) == {}
    env = github.git_env("ghu_ok")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ") and "ghu_ok" not in env["GIT_CONFIG_VALUE_0"]


# --- installs and updates ------------------------------------------------------------------------

def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env={**GIT_ENV, "HOME": str(cwd)})


def commit(dev, files, msg):
    for rel, text in files.items():
        p = dev / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git("add", "-A", cwd=dev)
    git("commit", "-m", msg, cwd=dev)
    git("push", "origin", "HEAD:main", cwd=dev)


@pytest.fixture
def plugin_origin(tmp_path):
    origin, dev = tmp_path / "origin.git", tmp_path / "dev"
    git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("clone", str(origin), str(dev), cwd=tmp_path)
    commit(dev, {"pyproject.toml": "[project]\nname='p'\n", "p/__init__.py": ""}, "first")
    return origin, dev


class Pip:
    """Runs git for real, records pip instead of running it."""

    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, argv, **kw):
        if argv[:3] == [sys.executable, "-m", "pip"]:
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0 if self.ok else 1, "", "" if self.ok else "boom")
        return subprocess.run(argv, **kw)


def test_add_install_check_update(tmp_path, plugin_origin):
    origin, dev = plugin_origin
    s = Settings()
    src = plugin_repos.add_source(s, "https://github.com/Me/Plugin.git", dir=str(tmp_path / "Plugin"))
    assert src.repo == "Me/Plugin" and plugin_repos.sources(s)[0].path == str(tmp_path / "Plugin")
    src.url = str(origin)
    assert plugin_repos.check(src).message.startswith("Not installed")

    pip = Pip()
    ok, msg = plugin_repos.install(src, "ghu_ok", runner=pip)
    assert ok and "Restart" in msg and pip.calls[0][-2:] == ["-e", src.path]
    assert "extraheader" not in (tmp_path / "Plugin" / ".git" / "config").read_text()   # token not saved

    assert plugin_repos.check(src).message == "Up to date."
    commit(dev, {"p/new.py": "x = 1\n"}, "Add a thing")
    st = plugin_repos.check(src)
    assert st.available and st.commits == ["Add a thing"]
    ok, msg = plugin_repos.update(src, runner=pip)
    assert ok and (tmp_path / "Plugin" / "p" / "new.py").exists() and len(pip.calls) == 1   # no pip: deps same
    commit(dev, {"pyproject.toml": "[project]\nname='p'\nversion='2'\n"}, "deps")
    ok, msg = plugin_repos.update(src, runner=pip)
    assert ok and len(pip.calls) == 2
    ok, msg = plugin_repos.update(src, runner=pip)
    assert ok and "up to date" in msg


def test_a_zip_copy_is_moved_aside_not_deleted(tmp_path, plugin_origin):
    origin, _ = plugin_origin
    old = tmp_path / "Plugin"
    old.mkdir()
    (old / "mine.txt").write_text("keep me")
    src = plugin_repos.Source("me/Plugin", str(old), str(origin))
    assert "moves it aside" in plugin_repos.check(src).message
    ok, msg = plugin_repos.install(src, runner=Pip())
    assert ok and "kept as" in msg
    aside = [p for p in tmp_path.iterdir() if p.name.startswith("Plugin.old-")]
    assert len(aside) == 1 and (aside[0] / "mine.txt").read_text() == "keep me"
    assert (old / ".git").exists()


def test_someone_elses_checkout_and_local_commits_are_left_alone(tmp_path, plugin_origin):
    origin, dev = plugin_origin
    other = tmp_path / "other.git"
    git("init", "--bare", "-b", "main", str(other), cwd=tmp_path)
    path = tmp_path / "Plugin"
    git("clone", str(other), str(path), cwd=tmp_path)
    st = plugin_repos.check(plugin_repos.Source("me/Plugin", str(path), str(origin)))
    assert not st.ok and "something else" in st.message

    path2 = tmp_path / "Plugin2"
    src = plugin_repos.Source("me/Plugin", str(path2), str(origin))
    assert plugin_repos.install(src, runner=Pip())[0]
    (path2 / "local.txt").write_text("mine")
    git("add", "-A", cwd=path2)
    git("commit", "-m", "local edit", cwd=path2)
    commit(dev, {"p/new.py": ""}, "upstream")
    st = plugin_repos.check(src)
    assert st.ahead == 1 and st.behind == 1 and not st.available
    ok, msg = plugin_repos.update(src, runner=Pip())
    assert not ok and "changes of its own" in msg.replace("change(s)", "changes")


def test_pip_failure_and_bad_names_are_reported(tmp_path, plugin_origin):
    origin, _ = plugin_origin
    src = plugin_repos.Source("me/Plugin", str(tmp_path / "P"), str(origin))
    ok, msg = plugin_repos.install(src, runner=Pip(ok=False))
    assert not ok and "pip couldn't install" in msg and "boom" in msg
    with pytest.raises(ValueError):
        plugin_repos.add_source(Settings(), "not a repo")
    s = Settings()
    plugin_repos.add_source(s, "me/a")
    plugin_repos.add_source(s, "me/b")
    plugin_repos.remove_source(s, "ME/A")
    assert [x.repo for x in plugin_repos.sources(s)] == ["me/b"]


def test_private_repo_without_sign_in_says_how_to_fix_it(tmp_path):
    src = plugin_repos.Source("me/Plugin", str(tmp_path / "P"), str(tmp_path / "missing.git"))
    ok, msg = plugin_repos.install(src, runner=Pip())
    assert not ok and "Couldn't download" in msg


# --- the handheld screens ------------------------------------------------------------------------

def test_handheld_plugins_and_sign_in_screens(monkeypatch, tmp_path):
    pygame = pytest.importorskip("pygame")
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    pygame.display.init()
    pygame.font.init()
    from canopener.device.app import DeviceApp
    from canopener.device.context import DeviceContext
    from canopener.device.screens.plugins import PluginsScreen, SignInScreen
    fake = FakeGitHub([{"error": "authorization_pending"}, {"access_token": "ghu_ok"}])
    monkeypatch.setattr(github, "http", fake)
    monkeypatch.setattr(github, "wait_for_token",
                        lambda cid, dc, cancelled: github.poll_once(cid, dc) or github.poll_once(cid, dc))
    ctx = DeviceContext(Settings())
    try:
        app = DeviceApp(ctx, (800, 480), encoder=False)
        app.step([])
        plugin_repos.add_source(ctx.settings, "me/Plugin", str(tmp_path / "P"))
        app.push(PluginsScreen(app))
        app.step([])
        labels = [r.label for r in app.top.rows.refresh()]
        assert "Sign in to GitHub" in labels and "me/Plugin" in labels
        screen = SignInScreen(app, client_id="Iv1.abc")
        app.push(screen)
        screen._thread.join(5)
        app.step([])
        assert screen.done_as == "tester" and github.load_account().token == "ghu_ok"
        app.pop(screen)
        app.step([])
        assert "Sign out" in [r.label for r in app.top.rows.refresh()]
        bad = SignInScreen(app, client_id="")
        app.push(bad)
        bad._thread.join(5)
        app.step([])
        assert "client ID" in bad.error
    finally:
        ctx.shutdown()
        pygame.quit()
