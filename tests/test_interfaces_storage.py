import csv
import os

from canopener.can.frame import CanFrame
from canopener.core import interfaces, storage


def test_setup_steps_can_and_vcan(tmp_path):
    steps = interfaces.setup_steps("can0", 500000, listen_only=True)
    assert [s[1:4] for s in steps] == [["link", "set", "can0"]] * 3
    assert steps[1][4:] == ["type", "can", "bitrate", "500000", "listen-only", "on", "restart-ms", "100"]
    (tmp_path / "vcan0").mkdir()
    assert [s[0] for s in interfaces.setup_steps("vcan0", 0, sys_net=str(tmp_path))] == \
        ["modprobe", interfaces._ip()]          # already exists: no 'ip link add'


def test_setup_rejects_bad_names():
    import pytest
    with pytest.raises(ValueError):
        interfaces.setup_steps("can0; rm -rf /", 250000)


class FakeBackend:
    """Delivers good frames only at the 'right' bitrate, error frames otherwise."""

    current = None

    def __init__(self, name):
        self.n = 0

    def open(self):
        pass

    def close(self):
        pass

    def recv(self):
        self.n += 1
        if FakeBackend.current == 500000:
            return CanFrame(0x18FECA00, b"\x00" * 8)
        return CanFrame(0x80, b"", error="bus error") if self.n % 2 else None


def test_detect_bitrate_probes_listen_only():
    calls = []

    def runner(steps):
        calls.append(steps)
        FakeBackend.current = int(steps[1][7])
        return True, "ok"

    found, log = interfaces.detect_bitrate("can0", listen_secs=0.2, runner=runner, open_backend=FakeBackend)
    assert found == 500000
    assert all(s[1][9] == "on" for s in calls)          # every probe in listen-only
    assert [int(s[1][7]) for s in calls] == [250000, 500000]
    assert "error frame" in log[0]


def test_detect_bitrate_stops_without_listen_only():
    found, log = interfaces.detect_bitrate("can0", runner=lambda steps: (False, "not supported"),
                                           open_backend=FakeBackend)
    assert found is None and "listen-only" in log[0]


def test_removable_drives(tmp_path):
    usb = tmp_path / "media" / "pi" / "MY STICK"
    usb.mkdir(parents=True)
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/mmcblk0p2 / ext4 rw 0 0\nproc /proc proc rw 0 0\n"
                      "/dev/sda1 /media/pi/MY\\040STICK vfat rw 0 0\n")
    drives = storage.removable_drives(str(mounts))
    assert [(d.label, d.path) for d in drives] == [("MY STICK", "/media/pi/MY STICK")]


def test_export_to_copies_logs_and_reports(tmp_path):
    src = tmp_path / "data"
    (src / "logs").mkdir(parents=True)
    (src / "reports" / "sub").mkdir(parents=True)
    (src / "logs" / "a.log").write_text("x")
    (src / "reports" / "sub" / "r.txt").write_text("y")
    dest, n = storage.export_to(str(tmp_path / "usb"), [str(src / "logs"), str(src / "reports")])
    assert n == 2
    assert os.path.exists(os.path.join(dest, "logs", "a.log"))
    assert os.path.exists(os.path.join(dest, "reports", "sub", "r.txt"))


def test_write_csv(tmp_path):
    frames = [CanFrame(0x18FECA00, b"\x00\xff\x00\x00\x00\x00\xff\xff", True, 1_700_000_000.5),
              CanFrame(0x18EA00F9, b"\xca\xfe\x00", True, 1_700_000_001.0, tx=True),
              CanFrame(0x123, b"\x01", False, 1_700_000_002.0),
              CanFrame(0x40, b"", True, 0, error="bus-off")]
    path = tmp_path / "f.csv"
    assert storage.write_csv(str(path), frames) == 3
    rows = list(csv.DictReader(open(path)))
    assert rows[0]["pgn"] == "0FECA" and rows[0]["sa_name"] == "Engine #1" and rows[0]["da"] == ""
    assert rows[1]["dir"] == "TX" and rows[1]["da"] == "00" and rows[1]["pgn_name"] == "Request"
    assert rows[2]["can_id"] == "123" and rows[2]["pgn"] == ""
