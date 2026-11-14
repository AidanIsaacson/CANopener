"""Remote help (core.remote + the upload server's /api/remote) and profile uploads, against the
real server on a local port."""

import base64
import json
import os
import threading
import time

import pytest
from test_upload import _server_module

from canopener.core import config, remote, upload
from canopener.core.settings import Settings


@pytest.fixture
def server(tmp_path):
    mod = _server_module()
    cfg = mod.Config({"DATA_DIR": str(tmp_path / "srv"), "HOST": "127.0.0.1", "PORT": "0",
                      "UPLOAD_TOKENS": "up-token", "READ_TOKENS": "read-token", "REMOTE_TOKENS": "remote-token"})
    srv = mod.make_server(cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def settings(server):
    s = Settings()
    s.set("upload.url", server)
    upload.save_token("up-token")
    return s


def _wait(cond, seconds=10.0):
    end = time.time() + seconds
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_a_unit_opens_a_session_and_the_operator_works_it(server, settings):
    os.makedirs(os.path.join(config.logs_dir(), "20260101_capture"))
    with open(os.path.join(config.logs_dir(), "20260101_capture", "summary.txt"), "w") as f:
        f.write("hello from the field\n")
    link = remote.RemoteLink(settings, label="Unit 14", minutes=5, info=lambda: {"where": "yard"})
    link.start()
    assert _wait(lambda: link.state == "open")
    op = remote.Operator(server, "remote-token")
    sessions = op.sessions()
    assert len(sessions) == 1 and sessions[0]["label"] == "Unit 14" and sessions[0]["info"]["where"] == "yard"
    sid = sessions[0]["id"]
    status = op.send(sid, "status")
    assert status["ok"] and "file" in status["value"]["actions"]
    logs = op.send(sid, "logs")["value"]
    assert logs[0]["name"] == "20260101_capture" and logs[0]["files"] == {"summary.txt": 21}
    got = op.send(sid, "file", {"path": "20260101_capture/summary.txt"})["value"]
    assert base64.b64decode(got["base64"]) == b"hello from the field\n"
    outside = op.send(sid, "file", {"path": "../../etc/passwd"})
    assert not outside["ok"] and outside["refused"] and "outside" in outside["error"]
    unknown = op.send(sid, "format_disk")
    assert not unknown["ok"] and "no action called" in unknown["error"]
    assert [e.action for e in link.log][:3] == ["session", "status", "logs"] and not link.log[-1].ok
    link.stop()
    assert link.state.startswith("ended") and op.sessions() == []


def test_an_action_that_changes_something_needs_a_yes_at_the_unit(server, settings):
    done = []
    remote.register("test_clear", lambda args, link: done.append(args) or "cleared", reads_only=False,
                    describe=lambda args: f"clear the codes on module {args.get('module')}")
    try:
        asked = []
        link = remote.RemoteLink(settings, confirm=lambda text: asked.append(text) or False)
        assert link.run("test_clear", {"module": 3}) == {"ok": False, "refused": True,
                                                         "error": "the person at the unit said no"}
        assert asked == ["clear the codes on module 3"] and not done
        link.confirm = lambda text: True
        assert link.run("test_clear", {"module": 3}) == {"ok": True, "value": "cleared"} and done == [{"module": 3}]
        link.confirm = None                             # nobody at the unit
        refused = link.run("test_clear", {})
        assert refused["refused"] and "nobody at the unit" in refused["error"] and len(done) == 1
    finally:
        remote._actions.pop("test_clear", None)


def test_only_the_right_tokens_get_in(server, settings):
    with pytest.raises(upload.UploadError):
        remote.Operator(server, "up-token").sessions()          # a unit's token can't operate
    with pytest.raises(upload.UploadError):
        remote.Operator(server, "read-token").sessions()
    upload.save_token("read-token")                              # and a read token can't open a session
    link = remote.RemoteLink(settings)
    link.start()
    assert _wait(lambda: link.state.startswith("ended")) and "refused the token" in link.state
    status, raw = upload.http("GET", server + "/api/whoami", None, {"Authorization": "Bearer remote-token"})
    assert status == 200 and json.loads(raw)["can_remote"] is True


def test_session_ends_when_its_time_is_up(server, settings):
    link = remote.RemoteLink(settings, minutes=0.02)             # the server gives at least a minute
    link.start()
    assert _wait(lambda: link.state == "open")
    link.session["expires"] = time.time() - 1
    assert _wait(lambda: link.state == "ended: time is up", 40)


def test_profiles_upload_keeps_revisions_and_can_be_read_back(server, settings, tmp_path):
    path = tmp_path / "observed-yard-truck.json"
    doc = {"name": "Yard truck", "protocol": "j1939", "extends": "generic",
           "signals": [{"name": "Engine speed", "pgn": "0xFF50", "start": 2, "length": 2}],
           "observed": {"machine": {"make": "Acme"}, "sessions": []}}
    path.write_text(json.dumps(doc))
    first = upload.upload_profile(settings, str(path))
    key = first["key"]
    assert key == f"observed-yard-truck_{upload.device_id()[:6]}" and first["revision"] == 1 and first["signals"] == 1
    assert upload.upload_profile(settings, str(path))["unchanged"] is True
    doc["signals"].append({"name": "Lift", "id": "0x288", "start": 1, "bit": 2, "bits": 1})
    path.write_text(json.dumps(doc))
    assert upload.upload_profile(settings, str(path))["revision"] == 2

    def read(url):
        status, raw = upload.http("GET", server + url, None, {"Authorization": "Bearer read-token"})
        return status, json.loads(raw)
    status, listing = read("/api/profiles")
    assert status == 200 and listing["count"] == 1 and listing["profiles"][0]["machine"] == {"make": "Acme"}
    assert listing["profiles"][0]["observed"] is True and listing["profiles"][0]["signals"] == 2
    assert len(read(f"/api/profiles/{key}")[1]["signals"]) == 2
    assert len(read(f"/api/profiles/{key}?rev=1")[1]["signals"]) == 1
    assert read("/api/profiles?q=nothing-like-it")[1]["count"] == 0
    status, _ = upload.http("PUT", server + f"/api/profiles/{key}", b"{}", {"Authorization": "Bearer read-token",
                                                                             "Content-Type": "application/json"})
    assert status == 403
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]")
    with pytest.raises(upload.UploadError):
        upload.upload_profile(settings, str(bad))


def test_handheld_remote_help_end_to_end(server, settings, tmp_path):
    """System > Remote help on the handheld (headless): the operator sees its state and screen,
    reads codes from the simulated thin K-line computer, and a request that isn't a plain read
    waits for a yes on the unit."""
    pygame = pytest.importorskip("pygame")
    from canopener.core.connection import ELM_SIM
    from canopener.device import input as inp
    from canopener.device import remote_actions
    from canopener.device.app import DeviceApp
    from canopener.device.context import DeviceContext
    from canopener.device.screens.base import ConfirmDialog
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    pygame.display.init()
    pygame.font.init()
    for k, v in {"show_simulators": True, "can.source": ELM_SIM, "elm.sim_vehicle": "thin-kline"}.items():
        settings.set(k, v)
    ctx = DeviceContext(settings)
    app = DeviceApp(ctx, (800, 480), encoder=False)
    try:
        app.step([])
        ctx.connect()
        assert ctx.wait_idle(15)
        assert ctx.remote_start(5) == ""
        assert _wait(lambda: ctx.remote.state == "open")
        op = remote.Operator(server, "remote-token")
        sid = op.sessions()[0]["id"]
        results = {}

        def ask(name, action, args=None):
            results[name] = op.send(sid, action, args, wait=30)
        state = op.send(sid, "state")["value"]
        assert state["live"] and state["protocol"] == "obd2" and "thin-kline" in state["connection"]
        shot = op.send(sid, "screenshot")["value"]
        assert base64.b64decode(shot["png_base64"])[:4] == b"\x89PNG" and shot["size"] == [800, 480]
        codes = op.send(sid, "codes")["value"]
        assert codes["thin"] and not any(codes["obd"][next(iter(codes["obd"]))].values())
        assert op.send(sid, "adapter", {"commands": ["ATRV", "ATPP 0C SV 23"]})["value"][1]["refused"]
        read = op.send(sid, "request", {"requests": ["0100", "03"]})["value"]
        assert read[0]["lines"][-1].startswith("486B1041") and not isinstance(app.top, ConfirmDialog)
        # a clear (Mode 04) is not a read: it waits for the person at the unit
        t = threading.Thread(target=ask, args=("clear", "request", {"requests": ["04"]}))
        t.start()
        assert _wait(lambda: (app.step([]) or True) and isinstance(app.top, ConfirmDialog))
        assert "Remote helper" in app.top.title and "04" in app.top.text
        app.action(inp.SELECT)                              # the default: No
        t.join(timeout=20)
        assert results["clear"]["refused"] and "said no" in results["clear"]["error"]
        assert "04" not in ctx.connection.simulator.adapter.commands       # it never reached the vehicle
        assert remote_actions.ASK_TIMEOUT == 60.0
        # the page shows what was done
        from canopener.device.screens.settings import SettingsScreen
        page = SettingsScreen(app)
        page.section = "Remote help"
        labels = [r.label for r in page._rows()]
        assert "Stop remote help" in labels and "screenshot" in labels and "What it has done" in labels
        ctx.remote_stop()
        assert op.sessions() == []
    finally:
        ctx.shutdown()
        pygame.quit()


def test_tunnel_enrols_its_key_and_builds_a_locked_down_ssh_command(tmp_path):
    """The unit's key is enrolled through the upload token; on the server it may only listen on
    its own port. (ssh itself isn't run here.)"""
    from canopener.core import tunnel
    mod = _server_module()
    cfg = mod.Config({"DATA_DIR": str(tmp_path / "srv"), "HOST": "127.0.0.1", "PORT": "0", "UPLOAD_TOKENS": "up-token",
                      "READ_TOKENS": "read-token", "TUNNEL_HOST": "logs.example.com", "TUNNEL_MAX_UNITS": "1"})
    srv = mod.make_server(cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        s = Settings()
        s.set("upload.url", f"http://127.0.0.1:{srv.server_address[1]}")
        upload.save_token("up-token")
        pub = tunnel.ensure_key()
        assert pub.startswith("ssh-ed25519 ") and tunnel.ensure_key() == pub

        class FakeProc:
            stderr = None

            def __init__(self, argv, **kw):
                FakeProc.argv = argv
                self.done = threading.Event()

            def poll(self):
                return 0 if self.done.is_set() else None

            def wait(self, timeout=None):
                self.done.wait(timeout)
                return 0

            def terminate(self):
                self.done.set()
        t = tunnel.Tunnel(s, popen=FakeProc)
        t.start()
        assert _wait(lambda: t.state == "open", 8) and t.running
        argv = FakeProc.argv
        assert argv[:3] == ["ssh", "-N", "-T"] and argv[-1] == "tunnel@logs.example.com"
        assert "127.0.0.1:22201:127.0.0.1:22" in argv and "BatchMode=yes" in argv and "47822" in argv
        assert t.grant["listen_port"] == 22201 and "22201" in t.where()
        keys = (tmp_path / "srv" / "tunnel" / "authorized_keys").read_text()
        assert keys.startswith('restrict,port-forwarding,permitlisten="127.0.0.1:22201" ' + pub)
        t.stop()
        assert _wait(lambda: not t.running) and t.state == "ended: stopped here"

        def post(body, token="up-token"):
            return upload.http("POST", s.get("upload.url") + "/api/remote/tunnel", json.dumps(body).encode(),
                               {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        bad = {"device": upload.device_id(), "public_key": 'ssh-ed25519 AAAA" command="sh'}
        assert post(bad)[0] == 400                                          # nothing but a plain key gets in
        assert post({"device": upload.device_id(), "public_key": pub}, "read-token")[0] == 403
        assert post({"device": "ab" * 8, "public_key": pub})[0] == 429       # TUNNEL_MAX_UNITS
    finally:
        srv.shutdown()
        srv.server_close()
