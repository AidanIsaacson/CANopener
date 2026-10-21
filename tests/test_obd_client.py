"""OBD-II client driving the OBD simulator over the virtual bus (integration)."""

from canopener.core import connection
from canopener.core.obd import ObdClient


def _client():
    conn = connection.open_connection(connection.OBD_SIM)
    return conn, ObdClient(conn.session)


def test_reads_vin_dtcs_and_live():
    conn, c = _client()
    try:
        assert c.available
        assert c.read_vin() == "1HGCM82633A004352"          # multi-frame, exercises ISO-TP flow control
        dtcs = c.read_dtcs()
        assert dtcs["stored"] == ["P0133", "P0420"]
        assert dtcs["pending"] == ["P0171"]
        assert dtcs["permanent"] == []
        live = {r.name: (r.value, r.unit) for r in c.read_live()}
        assert live["Engine speed"][1] == "rpm"
        assert live["Control module voltage"] == (14.1, "V")
        assert "Vehicle speed" in live
    finally:
        c.close()
        conn.close()


def test_supported_pid_walk_spans_ranges():
    conn, c = _client()
    try:
        pids = set(c.supported_pids())
        assert {0x05, 0x0C, 0x0D, 0x11} <= pids          # first range
        assert 0x42 in pids                               # reached via the continuation bit
    finally:
        c.close()
        conn.close()


def test_listen_only_blocks_requests():
    conn = connection.open_connection(connection.OBD_SIM)
    conn.session.listen_only = True
    c = ObdClient(conn.session)
    try:
        assert not c.available
        assert c.query(0x01, 0x0C) is None               # send refused, nothing comes back
    finally:
        c.close()
        conn.close()
