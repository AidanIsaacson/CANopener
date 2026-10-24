"""Self-update from the app's git checkout, against real throwaway git repos."""

import subprocess

import pytest

from canopener.core import updates


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": str(cwd)})


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def repos(tmp_path):
    """origin (bare, 'GitHub'), install (the user's checkout) and dev (where updates come from)."""
    origin, dev, install = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "install"
    git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("clone", str(origin), str(dev), cwd=tmp_path)
    write(dev / "canopener" / "__init__.py", '__version__ = "0.1.4"\n')
    write(dev / "pyproject.toml", "[project]\n")
    git("add", "-A", cwd=dev)
    git("commit", "-m", "first", cwd=dev)
    git("push", "origin", "HEAD:main", cwd=dev)
    git("clone", str(origin), str(install), cwd=tmp_path)
    return dev, install


def release(dev, version, extra=None, msg="new"):
    write(dev / "canopener" / "__init__.py", f'__version__ = "{version}"\n')
    for rel, text in (extra or {}).items():
        write(dev / rel, text)
    git("add", "-A", cwd=dev)
    git("commit", "-m", msg, cwd=dev)
    git("push", "origin", "HEAD:main", cwd=dev)


def test_up_to_date_then_update_available(repos):
    dev, install = repos
    s = updates.check(root=str(install))
    assert s.ok and not s.available and "up to date" in s.message
    release(dev, "0.1.5", msg="OBD-II freeze frame fixes")
    s = updates.check(root=str(install))
    assert s.available and s.behind == 1 and s.latest == "0.1.5" and s.commits == ["OBD-II freeze frame fixes"]
    assert "0.1.5" in s.message and not s.installer_changed and not s.deps_changed


def test_apply_pulls_and_reinstalls_only_when_needed(repos):
    dev, install = repos
    release(dev, "0.1.5", {"deploy/linux/70-canopener.rules": "rule\n"})
    pip_calls = []
    real_run = subprocess.run

    def runner(argv, **kw):
        if "pip" in argv:
            pip_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, **kw)
    ok, msg = updates.apply(root=str(install), runner=runner)
    assert ok and '"0.1.5"' in (install / "canopener" / "__init__.py").read_text()
    assert pip_calls == [] and "installer" in msg                  # deploy/ changed: run the installer once
    release(dev, "0.1.6", {"pyproject.toml": "[project]\ndependencies = []\n"})
    ok, msg = updates.apply(root=str(install), runner=runner)
    assert ok and len(pip_calls) == 1 and pip_calls[0][-1].endswith("[gui]") and "installer" not in msg


def test_local_commits_or_conflicting_edits_stop_it(repos):
    dev, install = repos
    write(install / "canopener" / "__init__.py", '__version__ = "0.1.4-mine"\n')    # an uncommitted edit
    release(dev, "0.1.5")
    s = updates.check(root=str(install))
    assert s.available and s.dirty == ["canopener/__init__.py"]
    ok, msg = updates.apply(root=str(install))
    assert not ok and "didn't download" in msg
    assert "0.1.4-mine" in (install / "canopener" / "__init__.py").read_text()      # left alone
    git("checkout", "--", ".", cwd=install)
    write(install / "local.txt", "x")
    git("add", "-A", cwd=install)
    git("commit", "-m", "mine", cwd=install)
    s = updates.check(root=str(install))
    assert s.ahead == 1 and "can't update automatically" in s.message


def test_not_a_git_install_and_offline(tmp_path, repos, monkeypatch):
    monkeypatch.setattr(updates, "repo_root", lambda: None)
    s = updates.check()
    assert not s.ok and "wasn't installed from git" in s.message
    assert updates.apply() == (False, "Not a git install: can't update in place.")
    _, install = repos
    git("remote", "set-url", "origin", str(tmp_path / "nowhere.git"), cwd=install)
    s = updates.check(root=str(install))
    assert not s.ok and "Couldn't reach GitHub" in s.message
