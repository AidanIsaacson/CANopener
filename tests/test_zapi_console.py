"""The Zapi console mode (a bundled plugin): switching it on, listening, and both front-ends.

No serial ports needed: the console opens fake taps. The bytes are made up (the protocol isn't
known yet); they only check the plumbing.
"""

import os
import time

import pytest

from canopener import cli, plugins
from canopener.addons.zapi_console import console as zc
from canopener.addons.zapi_console import protocol
from canopener.core.settings import Settings
from canopener.serialline import port as sp
from canopener.serialline.port import Chunk

MODULE = "canopener.addons.zapi_console"


class FakePort:
    in_waiting = 0

    def read(self, n):
        time.sleep(0.01)
        return b""

    def close(self):
        pass


def fake_opener(made: list):
    def opener(lines, framing):
        s = sp.Sniffer([sp.Tap(line, FakePort(), device=port, framing=framing) for line, port in lines])
        made.append(s)
        return s
    return opener


@pytest.fixture
def settings():
    s = Settings()
    s.set("zapi.console_port", "/dev/fake0")
    s.set("zapi.controller_port", "/dev/fake1")
    s.set("zapi.framing", "4800 8E1")
    return s


@pytest.fixture
def plugin_on():
    """Load the bundled plugin for one test (and take its handheld tile away afterwards)."""
    Settings().set(plugins.BUNDLED_SETTING, ["zapi_console"])
    plugins.discover(refresh=True)
    yield
    from canopener.device import modes
    modes._modes.pop("zapi", None)
    Settings().set(plugins.BUNDLED_SETTING, [])
    plugins.discover(refresh=True)


def test_bundled_plugins_are_off_until_switched_on():
    st = Settings()
    assert plugins.bundled_enabled(st) == []
    assert "zapi_console" not in {p.id for p in plugins.discover(refresh=True)}
    plugins.set_bundled(st, "zapi_console", True)
    plugins.set_bundled(st, "zapi_console", True)            # once only
    assert st.get(plugins.BUNDLED_SETTING) == ["zapi_console"]
    found = {p.id: p for p in plugins.discover(refresh=True)}
    assert found["zapi_console"].module is not None and found["zapi_console"].name == "Zapi console"
    plugins.set_bundled(st, "zapi_console", False)
    assert "zapi_console" not in {p.id for p in plugins.discover(refresh=True)}
    with pytest.raises(ValueError):
        plugins.set_bundled(st, "no_such_plugin", True)


def test_cli_switches_it(capsys):
    assert cli.main(["plugins", "enable", "zapi_console"]) == 0
    assert plugins.bundled_enabled() == ["zapi_console"]
    assert cli.main(["plugins", "disable", "zapi_console"]) == 0
    assert plugins.bundled_enabled() == []
    assert cli.main(["plugins", "enable", "nope"]) == 2


def test_console_listens_joins_messages_and_analyzes(settings):
    made = []
    con = zc.Console(settings, opener=fake_opener(made))
    assert con.ports() == [("console", "/dev/fake0"), ("controller", "/dev/fake1")]
    assert str(con.framing) == "4800 8E1" and con.status() == "Not listening"
    assert con.start() == "" and con.listening and con.start() == ""
    s = made[0]
    t0 = time.time()
    for i in range(6):
        s._chunk(Chunk(t0 + i * 0.2, "console", b"\x02\x10"))
        s._chunk(Chunk(t0 + i * 0.2 + 0.001, "console", b"\x00\xEE"))     # the same message, two chunks
        s._chunk(Chunk(t0 + i * 0.2 + 0.05, "controller", b"\x06MAIN MENU\x00"))
    con.poll(now=t0 + 10)
    shown = con.recent()
    assert [m.message.line for m in shown] == ["console", "controller"] * 6
    assert shown[0].message.data == b"\x02\x10\x00\xEE" and shown[0].hex == "02 10 00 EE"
    assert shown[1].text == "MAIN MENU"                     # plain text, until a decoder knows better
    assert "Listening at 4800 8E1" in con.status() and "12 messages" in con.status()
    r = con.analyze()
    assert r.master == "console" and "MAIN MENU" in r.text
    con.stop()
    assert not con.listening and con.sniffer is None


def test_console_says_why_it_cant_listen(settings):
    empty = Settings()
    empty.set("zapi.console_port", "")
    empty.set("zapi.controller_port", "")
    con = zc.Console(empty)
    assert "No port set" in con.start() and con.state == zc.FAILED and con.status() == con.error
    missing = zc.Console(settings)                          # the real opener, a port that isn't there
    assert "/dev/fake0" in missing.start() and missing.state == zc.FAILED
    bad = zc.Console(settings, opener=lambda lines, framing: (_ for _ in ()).throw(sp.SerialError("busy")))
    assert bad.start() == "busy" and bad.status() == "busy"


def test_a_decoder_names_its_finding_and_stays_unconfirmed():
    msg = protocol.Message("controller", 0.0, 0.0, b"\x06\x01")
    d = protocol.Decoder("made-up ack", 1, lambda m: m.data[:1] == b"\x06", lambda m: "acknowledged")
    protocol.DECODERS.append(d)
    try:
        assert protocol.describe(msg) == "acknowledged ?"
    finally:
        protocol.DECODERS.remove(d)
    assert protocol.describe(msg) == ""
    assert protocol.DECODERS == []          # nothing is known yet: findings go in with their evidence


def test_handheld_mode_listens_and_the_log_records_the_console(plugin_on, settings, tmp_path):
    pygame = pytest.importorskip("pygame")
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    pygame.display.init()
    pygame.font.init()
    from canopener.addons.zapi_console import shared_console
    from canopener.device.app import DeviceApp
    from canopener.device.context import DeviceContext
    from canopener.device.modes import shown
    ctx = DeviceContext(settings)
    try:
        app = DeviceApp(ctx, (800, 480), encoder=False)
        assert "zapi" in [m.id for m in shown(ctx)]
        app.open_mode("zapi")
        screen = app.top
        assert screen.title == "Zapi console" and screen.setup
        app.step([])                                         # the setup page draws
        made = []
        shared_console(ctx)._opener = fake_opener(made)
        screen.toggle_listen()
        assert ctx.serial is made[0] and not screen.setup
        app.step([])
        assert ctx.start_log()
        t0 = time.time()
        made[0]._chunk(Chunk(t0, "console", b"\x02\x10\x00\xEE"))
        made[0]._chunk(Chunk(t0 + 0.05, "controller", b"\x06OK"))
        app.step([])
        screen.con.poll(now=t0 + 5)
        assert len(screen.con.recent()) == 2
        app.step([])                                         # the message list draws
        ctx.stop_log()
        folder = ctx.capture.save(str(tmp_path))
        assert ctx.capture.serial_bytes == 7
        assert os.path.exists(os.path.join(folder, "serial.log"))
        screen.toggle_listen()
        assert ctx.serial is None and not screen.con.listening
    finally:
        ctx.shutdown()
        pygame.quit()


def test_desktop_tab_shows_messages_and_the_analysis(plugin_on, settings):
    qt = pytest.importorskip("canopener.gui.qt", exc_type=ImportError)
    app = qt.QtWidgets.QApplication.instance() or qt.QtWidgets.QApplication([])
    from canopener.addons.zapi_console.gui import ZapiConsoleTab
    from canopener.gui.main_window import MainWindow
    w = MainWindow()
    try:
        tab = next(t for t in (w.tabs.widget(i) for i in range(w.tabs.count())) if isinstance(t, ZapiConsoleTab))
        assert tab.port_boxes["zapi.console_port"].currentText() == "/dev/fake0"
        made = []
        tab.con._opener = fake_opener(made)
        tab.toggle_listen()
        assert w.ctx.serial is made[0] and tab.listen.text() == "Stop"
        t0 = time.time()
        for i in range(3):
            made[0]._chunk(Chunk(t0 + i, "console", b"\x02\x10\x00\xEE"))
            made[0]._chunk(Chunk(t0 + i + 0.05, "controller", b"\x06READY"))
        tab.con.poll(now=t0 + 10)
        tab.refresh()
        app.processEvents()
        assert tab.table.rowCount() == 6 and tab.table.item(1, 3).text() == "READY"
        tab.analyze()
        assert "Message kinds" in tab.report.toPlainText()
        tab.port_boxes["zapi.controller_port"].setCurrentText("/dev/other")
        assert Settings().get("zapi.controller_port") == "/dev/other"
        tab.toggle_listen()
        assert w.ctx.serial is None and tab.listen.text() == "Listen"
    finally:
        w.close()
