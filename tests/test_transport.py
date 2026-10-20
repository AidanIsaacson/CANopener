from canopener.j1939.constants import PGN_TPCM
from canopener.j1939.ids import parse_id
from canopener.j1939.transport import (
    TP_CTS,
    TP_EOM_ACK,
    TransportReceiver,
    bam_header,
    rts_header,
    split_packets,
)

PAYLOAD = bytes(range(40))          # 6 packets
PGN = 0xFECB


def make():
    sent = []
    rx = TransportReceiver(0xF9, lambda cid, d: sent.append((cid, bytes(d))) or True)
    return rx, sent


def test_bam_reassembly_sends_nothing():
    rx, sent = make()
    rx.on_cm(0.0, 0x00, 0xFF, bam_header(PGN, PAYLOAD))
    out = [rx.on_dt(0.01 * i, 0x00, 0xFF, pkt) for i, pkt in enumerate(split_packets(PAYLOAD))]
    assert out[-1] == (PGN, PAYLOAD) and all(o is None for o in out[:-1])
    assert sent == []


def test_rts_cts_flow_with_window():
    rx, sent = make()
    rx.on_cm(0.0, 0x00, 0xF9, rts_header(PGN, PAYLOAD, max_per_cts=2))
    pkts = split_packets(PAYLOAD)
    done = None
    for i, pkt in enumerate(pkts):
        done = rx.on_dt(0.01 * i, 0x00, 0xF9, pkt) or done
    assert done == (PGN, PAYLOAD)
    ctrl = [d[0] for _, d in sent]
    assert ctrl == [TP_CTS, TP_CTS, TP_CTS, TP_EOM_ACK]
    assert [d[2] for _, d in sent[:3]] == [1, 3, 5]       # next packet numbers asked for
    j = parse_id(sent[0][0])
    assert (j.pgn, j.sa, j.da, j.priority) == (PGN_TPCM, 0xF9, 0x00, 7)


def test_rts_to_someone_else_is_reassembled_silently():
    rx, sent = make()
    rx.on_cm(0.0, 0x00, 0x27, rts_header(PGN, PAYLOAD))
    for pkt in split_packets(PAYLOAD):
        done = rx.on_dt(0.0, 0x00, 0x27, pkt)
    assert done == (PGN, PAYLOAD) and sent == []


def test_respond_false_for_log_decoding():
    rx, sent = make()
    rx.respond = False
    rx.on_cm(0.0, 0x00, 0xF9, rts_header(PGN, PAYLOAD))
    for pkt in split_packets(PAYLOAD):
        done = rx.on_dt(0.0, 0x00, 0xF9, pkt)
    assert done == (PGN, PAYLOAD) and sent == []


def test_stale_session_expires():
    rx, _ = make()
    rx.on_cm(0.0, 0x00, 0xFF, bam_header(PGN, PAYLOAD))
    rx.on_dt(0.0, 0x00, 0xFF, split_packets(PAYLOAD)[0])
    rx.on_cm(10.0, 0x01, 0xFF, bam_header(PGN, PAYLOAD))    # unrelated, much later
    assert (0x00, 0xFF) not in rx.sessions and rx.dropped == 1


def test_abort_drops_session():
    rx, _ = make()
    rx.on_cm(0.0, 0x00, 0xF9, rts_header(PGN, PAYLOAD))
    rx.on_cm(0.1, 0x00, 0xF9, bytes([0xFF, 1, 0xFF, 0xFF, 0xFF, 0xCB, 0xFE, 0x00]))
    assert not rx.sessions
