"""The profile-driven simulator, decoded back through a Session: a round trip of every feature."""

import time

from canopener.core import profiles
from canopener.core.connection import PROFILE_SIM, open_connection
from canopener.j1939.dtc import Dtc
from canopener.j1939.signals import set_profile_signals

DOC = {
    "name": "Exerciser",
    "detect": {"addresses": [0, "0xE4", "0xEC"], "min_match": 2},
    "sources": {"0xE4": "Gateway (test)", "0xEC": "Lever module (test)"},
    "spns": {"520001": "Test OEM code"},
    "signals": [
        {"name": "Speed", "pgn": "0xF004", "start": 3, "length": 2, "scale": 0.125, "unit": "rpm",
         "low": 700, "high": 2500, "group": "Engine"},
        {"name": "Mode", "pgn": "0xFF40", "sa": "0xE4", "start": 0, "bit": 0, "bits": 3,
         "enum": {"0": "Idle", "1": "Drive", "2": "Lift"}, "group": "Truck"},
        {"name": "Seat", "pgn": "0xFF40", "sa": "0xE4", "start": 0, "bit": 3, "bits": 1, "group": "Truck"},
        {"name": "Tilt", "pgn": "0xFF40", "sa": "0xE4", "start": 1, "bit": 4, "bits": 12, "scale": 0.1,
         "unit": "°", "group": "Truck"},
        {"name": "Torque", "pgn": "0xFF41", "sa": "0xEC", "start": 0, "length": 2, "signed": True, "unit": "Nm"},
        {"name": "Far byte", "pgn": "0xFF42", "sa": "0xE4", "start": 20, "length": 1, "unit": "%",
         "low": 0, "high": 100},
    ],
}


def _connect():
    p = profiles.parse_profile(DOC, "exerciser")
    profiles.apply(p)
    conn = open_connection(PROFILE_SIM, profile=p, sim_seed=3)
    conn.simulator.active_codes = [(0xE4, Dtc(520001, 5, 2))]
    return p, conn


def test_profile_simulator_round_trip():
    p, conn = _connect()
    s = conn.session
    try:
        deadline = time.time() + 4
        while time.time() < deadline and not s.latest_for(0xFF42):
            time.sleep(0.05)
        by = {sig.name: sig for sig in p.signals}
        speed = by["Speed"].decode(s.latest_for(0xF004)[0][2])
        assert 700 * 0.9 <= speed <= 2500 * 1.1
        frame = dict((sa, d) for sa, _, d in s.latest_for(0xFF40))[0xE4]
        assert by["Mode"].text(by["Mode"].decode(frame)) in {"Idle", "Drive", "Lift"}
        assert by["Seat"].decode(frame) in (0.0, 1.0)
        assert by["Tilt"].decode(frame) is not None
        assert by["Torque"].decode(dict((sa, d) for sa, _, d in s.latest_for(0xFF41))[0xEC]) is not None
        far = s.latest_for(0xFF42)[0][2]              # 21 bytes: arrived as a multi-packet BAM
        assert len(far) >= 21 and by["Far byte"].decode(far) is not None
        time.sleep(1.2)
        assert (0xFECA, 0xE4, 520001, 5) in s.dtc_hist
        assert [m.profile.id for m in profiles.detect(s.known_ecus(), [p])] == ["exerciser"]
    finally:
        conn.close()
        profiles.apply(None)
        set_profile_signals([])


def test_profile_simulator_answers_requests():
    p, conn = _connect()
    s = conn.session
    try:
        time.sleep(0.3)
        t0 = time.time()
        assert s.request(0xFF41, 0xEC)
        time.sleep(0.4)
        assert any(k == (0xFF41, 0xEC) for k in s.snapshot_since(t0))
    finally:
        conn.close()
        profiles.apply(None)
