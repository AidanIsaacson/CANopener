import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Keep tests away from the user's real ~/.config/canopener and reset profile overrides."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    from canopener.core import profiles
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
