"""core.live: the Live Data model shared by the desktop tab and the future on-device UI (no Qt)."""

import time

from canopener.core.live import LiveData


def test_sample_select_filter_and_minmax(sim_session):
    session, _sim = sim_session
    live = LiveData()
    deadline = time.time() + 3
    while time.time() < deadline and len(live.order) < 10:
        live.sample(session)
        time.sleep(0.1)
    assert len(live.order) >= 10
    assert live.sample(session) is False or len(live.order) >= 10     # True only when rows appear

    rpm = live.find_spn(190)
    assert rpm is not None and live.rows[rpm].value is not None
    assert live.find_spn(999999) is None

    # Selecting = the "picked" list (plotted on the desktop, big-value grid on the device).
    live.select(rpm)
    live.select(rpm)                                   # idempotent
    assert live.selected == [rpm]
    assert live.matches(rpm, only_selected=True)
    other = next(k for k in live.order if k != rpm)
    assert not live.matches(other, only_selected=True) and live.matches(other)
    live.select(rpm, False)
    assert live.selected == []

    # Text and group filters.
    assert live.matches(rpm, "engine speed") and live.matches(rpm, "190") and not live.matches(rpm, "zzz")
    group = live.rows[rpm].sig.group
    assert group in live.groups() and live.matches(rpm, group=group)
    assert not live.matches(rpm, group="no such group")

    # History, min / max, reset.
    time.sleep(0.6)
    live.sample(session)
    r = live.rows[rpm]
    assert len(r.points) >= 2 and r.lo <= r.value <= r.hi
    live.reset_minmax()
    assert r.lo == r.hi == r.value

    rows, order, selected = live.rows, live.order, live.selected
    live.clear()                                       # in place: views holding these stay valid
    assert rows is live.rows and order is live.order and selected is live.selected and not order


def test_load_history_from_a_log(tmp_path, sim_session):
    from canopener.core.connection import load_log_session
    session, _sim = sim_session
    path = tmp_path / "c.log"
    session.start_recording(str(path))
    time.sleep(1.5)
    session.stop_recording()
    offline, n, _markers = load_log_session(str(path))
    assert n > 50
    live = LiveData()
    live.load_history(offline)
    rpm = live.find_spn(190)
    assert rpm is not None and len(live.rows[rpm].points) >= 5
