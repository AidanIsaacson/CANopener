import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Keep tests away from the user's real ~/.config/canopener and reset profile overrides."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))     # ~/canopener (captures, logs) too
    try:
        # The GUI's QSettings("CANopener", "CANopener") is NativeFormat, which only follows
        # setPath for NativeFormat (XDG_CONFIG_HOME is read once per process). Point it here too.
        from canopener.gui.qt import QtCore
        QtCore.QSettings.setPath(QtCore.QSettings.Format.NativeFormat, QtCore.QSettings.Scope.UserScope,
                                 str(tmp_path / "qsettings"))
    except ImportError:             # no Qt: GUI tests are skipped anyway
        pass
    from canopener.core import profiles, sound
    monkeypatch.setattr(sound, "player", lambda: None)     # no beeping from the speaker while testing
    profiles.apply(None)
    yield
    profiles.apply(None)


@pytest.fixture
def sim_session():
    """A live Session connected to the simulator over an in-process virtual bus."""
    from canopener.core.connection import SIMULATOR, open_connection
    conn = open_connection(SIMULATOR, sim_seed=1)
    yield conn.session, conn.simulator
    conn.close()


@pytest.fixture
def fake_bt(tmp_path, monkeypatch):
    """core.bluetooth talking to tests/fake_bluetoothctl.py instead of BlueZ. Returns a function
    giving the stand-in's state (powered, paired, pin) as a dict, and one to change it."""
    import json
    import sys

    from canopener.core import bluetooth
    state = tmp_path / "bt_state.json"
    state.write_text(json.dumps({"powered": True, "paired": [], "pin": "1234"}))
    script = tmp_path / "bluetoothctl"
    fake = os.path.join(os.path.dirname(__file__), "fake_bluetoothctl.py")
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    script.chmod(0o755)
    monkeypatch.setenv("FAKE_BT_STATE", str(state))
    monkeypatch.setattr(bluetooth, "BLUETOOTHCTL", str(script))

    class Fake:
        @staticmethod
        def state() -> dict:
            return json.loads(state.read_text())

        @staticmethod
        def set(**kw):
            st = json.loads(state.read_text())
            st.update(kw)
            state.write_text(json.dumps(st))
    return Fake
