import os

from canopener import desktop


def test_install_and_uninstall(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setattr(desktop, "_desktop_dir", lambda: str(tmp_path / "Desktop"))
    done = desktop.install(desktop_shortcut=True)
    assert len(done) == 3 and all(os.path.exists(p) for p in done)
    text = open(desktop.paths()["menu"]).read()
    assert "-m canopener" in text and "Icon=" in text and "StartupWMClass=canopener" in text
    assert os.access(desktop.paths()["shortcut"], os.X_OK)
    assert sorted(desktop.uninstall()) == sorted(done)
    assert not any(os.path.exists(p) for p in done)
