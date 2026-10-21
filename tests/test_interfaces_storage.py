import csv
import os

from canopener.can.frame import CanFrame
from canopener.core import interfaces, storage


def test_setup_steps_can_and_vcan(tmp_path):
    steps = interfaces.setup_steps("can0", 500000, listen_only=True)
    assert [s[1:4] for s in steps] == [["link", "set", "can0"]] * 3
    # The critical config no longer bundles restart-ms (it fails on adapters that lack it).
    assert steps[1][4:] == ["type", "can", "bitrate", "500000", "listen-only", "on"]
    assert not any("restart-ms" in s for s in steps)
    (tmp_path / "vcan0").mkdir()
    assert [s[0] for s in interfaces.setup_steps("vcan0", 0, sys_net=str(tmp_path))] == \
        ["modprobe", interfaces._ip()]          # already exists: no 'ip link add'


def test_restart_ms_is_optional_and_does_not_fail_bringup(monkeypatch):
    # Simulate an adapter that rejects restart-ms (as root, so steps run directly).
    calls = []

    class R:
        def __init__(self, rc, err=""):
            self.returncode, self.stderr, self.stdout = rc, err, ""

    def fake_run(argv, **kw):
        calls.append(argv)
        if "restart-ms" in argv:
            return R(1, "Device does not support restart from Bus Off")
        return R(0)

    monkeypatch.setattr(interfaces.os, "geteuid", lambda: 0)
    monkeypatch.setattr(interfaces.subprocess, "run", fake_run)
    ok, msg = interfaces.bring_up("can0", 250000, listen_only=False)
    assert ok, msg                               # restart-ms failure is tolerated
    assert any("restart-ms" in c for c in calls) # but it was attempted
    assert any(c[-1] == "up" for c in calls)     # and the interface was still brought up


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


def test_export_items_copies_selected_captures_and_files(tmp_path):
    cap1 = tmp_path / "logs" / "20260101_cap"
    (cap1 / "sub").mkdir(parents=True)
    (cap1 / "capture.log").write_text("x")
    (cap1 / "sub" / "deep.txt").write_text("y")
    report = tmp_path / "reports" / "r.txt"
    report.parent.mkdir()
    report.write_text("z")
    (tmp_path / "logs" / "not_selected.log").write_text("nope")
    seen = []
    dest, n = storage.export_items([str(cap1), str(report)], str(tmp_path / "usb"),
                                   progress=lambda d, t: seen.append((d, t)))
    assert n == 3 and seen[-1][0] == seen[-1][1] == 3
    assert os.path.exists(os.path.join(dest, "20260101_cap", "sub", "deep.txt"))
    assert os.path.exists(os.path.join(dest, "r.txt"))
    assert not os.path.exists(os.path.join(dest, "not_selected.log"))
    assert storage.export_size([str(cap1), str(report)]) == 3


def test_removable_only_trusts_mnt_for_usb_devices(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/sdb1 /media/me/STICK vfat rw 0 0\n"
                      "/dev/sdc1 /mnt/usb vfat rw 0 0\n"
                      "/dev/nvme0n1p3 /mnt/data ext4 rw 0 0\n"
                      "/dev/sdb1 /media/me/STICK2 vfat rw 0 0\n")
    drives = storage.removable_drives(str(mounts), removable=lambda dev: dev == "/dev/sdc1")
    assert [(d.device, d.path) for d in drives] == [("/dev/sdb1", "/media/me/STICK"), ("/dev/sdc1", "/mnt/usb")]


def test_is_removable_reads_sysfs(tmp_path):
    usb = tmp_path / "devices" / "pci0000:00" / "usb1" / "1-1" / "block" / "sdb"
    (usb / "sdb1").mkdir(parents=True)
    (usb / "sdb1" / "partition").write_text("1")
    nvme = tmp_path / "devices" / "pci0000:00" / "nvme" / "nvme0n1"
    (nvme / "nvme0n1p1").mkdir(parents=True)
    (nvme / "nvme0n1p1" / "partition").write_text("1")
    (nvme / "removable").write_text("0")
    sysb = tmp_path / "class_block"
    sysb.mkdir()
    os.symlink(usb / "sdb1", sysb / "sdb1")
    os.symlink(nvme / "nvme0n1p1", sysb / "nvme0n1p1")
    assert storage.is_removable("/dev/sdb1", str(sysb))
    assert not storage.is_removable("/dev/nvme0n1p1", str(sysb))
    assert storage._parent_disk("/dev/sdb1").startswith("/dev/")


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


def test_sudoers_content_is_scoped_and_safe():
    c = interfaces.sudoers_content("tech")
    assert "NOPASSWD" in c and "ip link set can[0-9] *" in c
    assert "tech ALL=(root)" in c
    # Scoped to the ip-link alias only: no blanket grant, no shell.
    assert "ALL=(ALL) ALL" not in c and "/bin/sh" not in c and "(ALL : ALL)" not in c


def test_sudoers_content_rejects_bad_user():
    import pytest
    for bad in ("root; rm -rf /", "a b", "", "x" * 40):
        with pytest.raises(ValueError):
            interfaces.sudoers_content(bad)
