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


def test_detect_bitrate_probes_listen_only(tmp_path):
    calls = []

    def runner(steps):
        calls.append(steps)
        FakeBackend.current = int(steps[1][7])
        return True, "ok"

    found, log = interfaces.detect_bitrate("can0", listen_secs=0.2, runner=runner, open_backend=FakeBackend,
                                           sys_net=str(tmp_path))
    assert found == 500000
    assert all(s[1][9] == "on" for s in calls)          # every probe in listen-only
    assert [int(s[1][7]) for s in calls] == [250000, 500000]
    assert "error frame" in log[0]


def test_detect_bitrate_stops_without_listen_only(tmp_path):
    found, log = interfaces.detect_bitrate("can0", runner=lambda steps: (False, "not supported"),
                                           open_backend=FakeBackend, sys_net=str(tmp_path))
    assert found is None and "listen-only" in log[0]


def fake_sys_net(tmp_path, **ifaces):
    """/sys/class/net with CAN interfaces: name=True for a parent device (gs_usb), False for none (slcan)."""
    for name, has_device in ifaces.items():
        d = tmp_path / name
        d.mkdir()
        (d / "type").write_text(f"{interfaces.ARPHRD_CAN}\n")
        (d / "operstate").write_text("up\n")
        (d / "flags").write_text("0x1\n")
        if has_device:
            (d / "device").mkdir()
    return str(tmp_path)


def test_slcan_is_a_can_interface_without_a_parent_device(tmp_path):
    sys_net = fake_sys_net(tmp_path, can0=False, can1=True, vcan0=False)
    assert interfaces.is_slcan("can0", sys_net)
    assert not interfaces.is_slcan("can1", sys_net)       # gs_usb: has /sys/class/net/can1/device
    assert not interfaces.is_slcan("vcan0", sys_net)
    assert not interfaces.is_slcan("can9", sys_net)       # not there at all
    found = {i.name: i.slcan for i in interfaces.list_interfaces(sys_net, details=False)}
    assert found == {"can0": True, "can1": False, "vcan0": False}


def test_slcan_refuses_listen_only(tmp_path, monkeypatch):
    # In listen-only the CANable's slcan firmware never opens: refuse rather than come up deaf.
    sys_net = fake_sys_net(tmp_path, can0=False)
    ran = []
    monkeypatch.setattr(interfaces, "run_privileged", lambda steps, **kw: (ran.append(steps) or (True, "ok")))
    ok, msg = interfaces.bring_up("can0", 250000, listen_only=True, sys_net=sys_net)
    assert not ok and "listen-only" in msg and "candleLight" in msg and ran == []
    assert interfaces.bring_up("can0", 250000, listen_only=False, sys_net=sys_net) == (True, "ok")
    assert ran[0][1][-2:] == ["listen-only", "off"]

    found, log = interfaces.detect_bitrate("can0", runner=lambda steps: ran.append(steps) or (True, "ok"),
                                           open_backend=FakeBackend, sys_net=sys_net)
    assert found is None and "candleLight" in log[0] and len(ran) == 1   # nothing probed


def test_needs_setup_when_down_or_set_differently():
    up = interfaces.CanInterface
    assert interfaces.needs_setup(up("can0", "down"), 250000, False)
    # Left by a stopped signal finder: listen-only, at the bitrate it was trying.
    assert interfaces.needs_setup(up("can0", "up", 1000000, True), 250000, False)
    assert interfaces.needs_setup(up("can0", "up", 250000, True), 250000, False)
    assert interfaces.needs_setup(up("can0", "up", 250000, False), 250000, True)
    assert interfaces.needs_setup(up("can0", "up", 500000, False), 250000, False)
    assert not interfaces.needs_setup(up("can0", "up", 250000, False), 250000, False)
    assert not interfaces.needs_setup(up("can0", "up", 500000, False), None, False)   # Auto: any bitrate
    assert not interfaces.needs_setup(up("can0", "up"), 250000, False)              # details unknown
    assert not interfaces.needs_setup(up("vcan0", "down"), 250000, True)


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


def _fake_usb(root, name, vid, pid, product, tty=None):
    dev = root / "devices" / name
    dev.mkdir(parents=True)
    for attr, val in (("idVendor", vid), ("idProduct", pid), ("product", product)):
        (dev / attr).write_text(val + "\n")
    bus = root / "bus"
    bus.mkdir(exist_ok=True)
    (bus / name).symlink_to(dev)
    if tty:
        intf = dev / f"{name}:1.0"
        intf.mkdir()
        (root / "tty" / tty).mkdir(parents=True)
        (root / "tty" / tty / "device").symlink_to(intf)


def test_usb_adapters_recognises_canable_firmwares(tmp_path):
    (tmp_path / "tty").mkdir()
    _fake_usb(tmp_path, "1-1", "1d50", "606f", "canable gs_usb")
    _fake_usb(tmp_path, "1-2", "16d0", "117e", "CANable2 b158aa7", tty="ttyACM0")
    _fake_usb(tmp_path, "1-3", "0483", "df11", "STM32  BOOTLOADER")
    _fake_usb(tmp_path, "1-4", "046d", "c52b", "USB Receiver")              # a mouse: ignored
    _fake_usb(tmp_path, "1-5", "0483", "5740", "MKS CANable V2.0 Pro", tty="ttyACM1")  # clone ID
    found = interfaces.usb_adapters(str(tmp_path / "bus"), str(tmp_path / "tty"))
    assert [(a.kind, a.tty) for a in found] == [
        ("gs_usb", ""), ("slcan", "/dev/ttyACM0"), ("dfu", ""), ("slcan", "/dev/ttyACM1")]
    hint = interfaces.no_adapter_hint(str(tmp_path / "bus"), str(tmp_path / "tty"))
    assert "slcan firmware" in hint and "/dev/ttyACM0" in hint and "DFU" in hint
    assert "No CAN adapter found" in interfaces.no_adapter_hint(str(tmp_path / "none"), str(tmp_path / "none"))


def test_slcan_udev_rule_starts_the_attach_service():
    import re
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy", "linux")
    rules = open(os.path.join(here, "70-canopener.rules")).read()
    unit = open(os.path.join(here, "canopener-slcan@.service")).read()
    # The rule's USB ID matches the one the app recognises, and starts the unit that exists.
    for vid, pid in interfaces.USB_SLCAN:
        assert f'ATTRS{{idVendor}}=="{vid}", ATTRS{{idProduct}}=="{pid}"' in rules
    assert re.findall(r'SYSTEMD_WANTS}\+="([^"@]+)@', rules) == ["canopener-slcan"] * 2
    assert "ID_MM_DEVICE_IGNORE" in rules
    # slcand attaches only: no -s/-o, so CANopener sets the bitrate and listen-only via ip link.
    exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert exec_line.split() == ["ExecStart=/usr/bin/slcand", "-F", "/dev/%I"]
