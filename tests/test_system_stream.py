"""System info helpers (sysfs fakes) and the OBD-II live poller against the car simulator."""

import time

from canopener.core import system
from canopener.core.connection import OBD_SIM, open_connection
from canopener.core.live import ObdLiveData
from canopener.core.obd import ObdClient
from canopener.core.obd_stream import ObdStreamer


def test_backlight_read_and_set(tmp_path):
    assert system.brightness(str(tmp_path)) is None
    assert system.set_brightness(0.5, str(tmp_path))[0] is False
    bl = tmp_path / "10-0045"
    bl.mkdir()
    (bl / "max_brightness").write_text("255\n")
    (bl / "brightness").write_text("255\n")
    assert system.brightness(str(tmp_path)) == 1.0
    assert system.set_brightness(0.5, str(tmp_path)) == (True, "Brightness 50%")
    assert (bl / "brightness").read_text() == "128"
    system.set_brightness(0.0, str(tmp_path))                 # never fully off
    assert (bl / "brightness").read_text() == "13"


def test_temperature_uptime_and_formatting(tmp_path):
    z = tmp_path / "thermal_zone0"
    z.mkdir()
    (z / "temp").write_text("48312\n")
    assert system.cpu_temp(str(tmp_path)) == 48.312
    up = tmp_path / "uptime"
    up.write_text("93784.21 12345.6\n")
    assert system.uptime(str(up)) == 93784.21
    assert system.fmt_duration(93784.21) == "1d 2h 03m" and system.fmt_duration(None) == "n/a"
    assert system.fmt_bytes(5 * 1024 ** 3) == "5 GB"
    free, total = system.disk_free(str(tmp_path))
    assert 0 < free <= total


def test_obd_streamer_keeps_the_car_talking():
    conn = open_connection(OBD_SIM, sim_seed=1)
    try:
        client = ObdClient(conn.session)
        todo = client.live_pids()[:4]
        assert todo
        live = ObdLiveData()
        st = ObdStreamer(client)
        st.start(todo)
        end = time.time() + 5
        while time.time() < end and len(live.order) < len(todo):
            live.sample(conn.session)
            time.sleep(0.05)
        st.stop()
        assert len(live.order) == len(todo) and not st.running
    finally:
        conn.close()
