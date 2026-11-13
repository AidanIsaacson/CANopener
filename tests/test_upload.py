"""Machine details, the capture README, and uploading to deploy/upload-server (end to end)."""

import importlib.util
import json
import os
import threading
import time
import urllib.request
import zipfile

import pytest

from canopener.core import capture as cap
from canopener.core import identity, upload
from canopener.core.capture import Capture
from canopener.core.settings import Settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _server_module():
    spec = importlib.util.spec_from_file_location("upload_server", os.path.join(ROOT, "deploy", "upload-server",
                                                                                 "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(tmp_path):
    mod = _server_module()
    cfg = mod.Config({"DATA_DIR": str(tmp_path / "srv"), "HOST": "127.0.0.1", "PORT": "0",
                      "UPLOAD_TOKENS": "up-token", "READ_TOKENS": "read-token,both",
                      "PUBLIC_URL": "https://logs.example.com", "MAX_PART_MB": "1"})
    cfg.upload_tokens.append("both")
    srv = mod.make_server(cfg)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", cfg
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def settings(tmp_path, server):
    s = Settings(str(tmp_path / "settings.json"))
    s.set("upload.url", server[0])
    s.set("upload.part_mb", 0.125)            # 128 kB parts: a few for a small test capture
    upload.save_token("up-token")
    return s


def _get(url, token="read-token"):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _saved_capture(tmp_path, n_frames=200, size_lines=0) -> Capture:
    """A saved capture without a live bus: write a log, then save it (the report decodes it)."""
    c = Capture("Unit 14", "dies after 10 min", ["key on", "start"],
                machine={"make": "Acme", "model": "LiftMaster 40", "serial": "AB123", "hours": " 1234.5 "},
                question="PGN 0xFF12 doesn't parse")
    c.root = str(tmp_path / "logs")
    os.makedirs(c.root, exist_ok=True)
    c.started = 1_700_000_000.0
    c.folder = os.path.join(c.root, ".recording_x")
    os.makedirs(c.folder)
    with open(c.log_path, "w") as f:
        for i in range(n_frames):
            t = c.started + i * 0.01
            f.write(f"({t:.6f}) can0 18FEF100#FFFF{i % 256:02X}00FFFFFFFF\n")       # CCVS: decoded
            f.write(f"({t:.6f}) can0 18FF1227#0102030405060708\n")                  # proprietary: not
        for i in range(size_lines):             # bulk, to need several upload parts
            f.write(f"({c.started + 3 + i * 1e-4:.6f}) can0 18FF3427#{os.urandom(8).hex().upper()}\n")
    c.stopped = c.started + 3 + size_lines * 1e-4
    c.frames = 2 * n_frames + size_lines
    c.state = cap.STOPPED
    c.tag(0, c.started + 1)
    c.save()
    return c


def test_machine_details_round_trip_and_recent(tmp_path):
    c = _saved_capture(tmp_path)
    assert c.machine == {"make": "Acme", "model": "LiftMaster 40", "serial": "AB123", "hours": "1234.5"}
    assert c.machine_summary() == "Acme LiftMaster 40, SN AB123"
    r = Capture.load(c.folder)
    assert r.machine == c.machine and r.question == "PGN 0xFF12 doesn't parse"
    r.set_machine({"make": "Acme", "model": "LiftMaster 40", "serial": "AB124", "bogus": "x"}, question="")
    assert Capture.load(c.folder).machine == {"make": "Acme", "model": "LiftMaster 40", "serial": "AB124"}
    assert "AB124" in open(os.path.join(c.folder, "README.md")).read()      # README follows at once
    cap.remember_machine(c.machine)
    cap.remember_machine({"make": "Other", "model": "X"})
    cap.remember_machine({"make": "acme", "model": "liftmaster 40", "serial": "ab123", "unit": "7"})
    rec = cap.recent_machines()
    assert [m.get("make") for m in rec] == ["acme", "Other"] and rec[0]["unit"] == "7"
    f = c.fresh_copy()
    assert f.machine == c.machine and f.question == ""


def test_readme_lists_undecoded_traffic(tmp_path):
    c = _saved_capture(tmp_path)
    readme = open(os.path.join(c.folder, "README.md")).read()
    assert readme.startswith("# Unit 14")
    assert "| Model | LiftMaster 40 |" in readme and "## Customer concern" in readme
    assert "## What to look at\n\nPGN 0xFF12 doesn't parse" in readme
    und = readme.split("## Traffic nothing decoded")[1]
    assert "PGN 65298 (0xFF12)" in und and "`01 02 03 04 05 06 07 08`" in und
    assert "0xFEF1" not in und                              # vehicle speed decodes
    rows = open(os.path.join(c.folder, "traffic.csv")).read()
    assert "PGN 65265 (0xFEF1)" in rows and ",yes," in rows and ",no," in rows
    assert "- `traffic.csv`" in readme and "- `capture.log`" in readme


def test_identity_candidates_from_simulator(sim_session):
    session, _ = sim_session
    session.request(0xFEEB)
    deadline = time.time() + 5
    while time.time() < deadline and len(identity.candidates(session)) < 2:
        time.sleep(0.1)
    got = identity.candidates(session)
    eng = next(c for c in got if c.fields.get("make") == "DEUTZ")
    assert eng.fields["model"] == "TCD3.6 L4" and eng.fields["serial"] == "SIM00012345"
    assert any("Component ID" in line for line in identity.seen_lines(session))


def test_upload_end_to_end_in_parts_and_read_back(tmp_path, settings, server):
    base, _ = server
    c = _saved_capture(tmp_path, size_lines=60000)          # ~600 kB zipped: several parts
    q = upload.UploadQueue(settings)
    st = q.enqueue(c.folder)
    assert st.state == upload.QUEUED and q.waiting() == 1
    seen = []
    q.on_change = lambda folder: seen.append(q.status(folder).progress)
    assert q.run_once() == c.folder
    st = q.status(c.folder)
    assert st.state == upload.DONE and not st.stale, st
    assert st.key.endswith("_Unit_14_" + upload.device_id()[:6])
    assert st.url == f"https://logs.example.com/api/captures/{st.key}"
    assert any(p is not None and 0 < p < 1 for p in seen)     # progress moved part by part
    assert q.waiting() == 0 and q.run_once() is None

    status, body = _get(f"{base}/api/captures?q=liftmaster+ab123")
    lst = json.loads(body)
    assert status == 200 and lst["count"] == 1 and lst["captures"][0]["machine"]["serial"] == "AB123"
    status, body = _get(f"{base}/api/captures/{st.key}")
    m = json.loads(body)
    assert m["question"] == "PGN 0xFF12 doesn't parse" and m["revision"] == 1
    assert {f["name"] for f in m["files"]} >= {"README.md", "capture.log", "traffic.csv", "capture.json"}
    assert "upload.json" not in {f["name"] for f in m["files"]}
    status, body = _get(f"{base}/api/captures/{st.key}/files/README.md")
    assert status == 200 and b"# Unit 14" in body
    status, body = _get(f"{base}/api/captures/{st.key}/files/capture.log?max_bytes=100")
    assert status == 200 and len(body) == 100
    status, body = _get(f"{base}/api/captures/{st.key}/bundle.zip")
    assert status == 200 and body[:2] == b"PK"

    # relabelled after the upload: shown as changed; uploading again makes revision 2, same key
    Capture.load(c.folder).set_machine({"make": "Acme", "model": "LiftMaster 40", "serial": "AB123", "unit": "9"})
    assert q.status(c.folder).stale
    q.enqueue(c.folder)
    q.run_once()
    st2 = q.status(c.folder)
    assert st2.state == upload.DONE and st2.key == st.key and not st2.stale
    assert json.loads(_get(f"{base}/api/captures/{st.key}")[1])["revision"] == 2


def test_upload_tokens_are_separate(tmp_path, settings, server):
    base, _ = server
    assert upload.HttpUploader().check(settings).startswith("Connected")
    assert _get(f"{base}/api/captures", token="up-token")[0] == 403         # upload tokens can't read
    assert _get(f"{base}/api/captures", token="nope")[0] == 401
    assert _get(f"{base}/api/health", token="")[0] == 200
    upload.save_token("read-token")                                          # a read token can't upload
    with pytest.raises(upload.UploadError, match="can't upload"):
        upload.HttpUploader().check(settings)
    upload.save_token("wrong")
    with pytest.raises(upload.UploadError, match="refused the token") as e:
        upload.HttpUploader().check(settings)
    assert not e.value.retry


def test_resume_after_a_dropped_connection(tmp_path, settings, server, monkeypatch):
    c = _saved_capture(tmp_path, size_lines=60000)
    real = upload.http
    calls = {"parts": 0}

    def flaky(method, url, body=None, headers=None, timeout=60):
        if "/parts/" in url:
            calls["parts"] += 1
            if calls["parts"] == 3:
                raise upload.UploadError("Couldn't reach the server (no internet?): reset")
        return real(method, url, body, headers, timeout)
    monkeypatch.setattr(upload, "http", flaky)
    monkeypatch.setattr(upload, "PART_TRIES", 1)
    now = [1000.0]
    q = upload.UploadQueue(settings, clock=lambda: now[0])
    q.enqueue(c.folder)
    q.run_once()
    st = q.status(c.folder)
    assert st.state == upload.FAILED and "no internet" in st.text and "trying again" in st.text
    assert q.run_once() is None                         # not due yet (back-off)
    sent_before = calls["parts"]
    now[0] += upload.RETRY_FIRST + 1
    q.run_once()
    assert q.status(c.folder).state == upload.DONE
    st = upload.read_state(c.folder)
    parts = -(-st["size"] // (128 * 1024))
    assert parts >= 3 and calls["parts"] - sent_before == parts - 2     # resumed: parts 0 and 1 weren't resent


def test_https_required_and_not_configured(tmp_path, settings):
    c = _saved_capture(tmp_path)
    settings.set("upload.url", "http://logs.example.com")
    with pytest.raises(upload.UploadError, match="https://"):
        upload.HttpUploader().check(settings)
    upload.save_token("")
    q = upload.UploadQueue(settings)
    with pytest.raises(upload.UploadError, match="Nowhere to upload"):
        q.enqueue(c.folder)
    assert upload.after_save(q, settings, c.folder) is None


def test_server_refuses_unsafe_zip(tmp_path, server):
    mod = _server_module()
    store = mod.Store(mod.Config({"DATA_DIR": str(tmp_path / "srv2")}))
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../evil.txt", "x")
    with pytest.raises(mod.ApiError, match="unexpected file name"):
        store._extract(str(bad), str(tmp_path / "out"))
    assert not (tmp_path / "evil.txt").exists()


def test_plugin_uploader_registry(tmp_path):
    class Shelf(upload.Uploader):
        id, name = "shelf", "A folder"

        def upload(self, bundle, settings, progress=None):
            dest = tmp_path / "shelf" / os.path.basename(bundle.path)
            dest.parent.mkdir(exist_ok=True)
            dest.write_bytes(open(bundle.path, "rb").read())
            return {"key": bundle.key, "url": str(dest)}
    upload.register_uploader(Shelf())
    try:
        s = Settings(str(tmp_path / "s.json"))
        s.set("upload.target", "shelf")
        s.set("upload.on_save", True)
        c = _saved_capture(tmp_path)
        q = upload.UploadQueue(s)
        assert upload.after_save(q, s, c.folder).state == upload.QUEUED
        q.run_once()
        st = q.status(c.folder)
        assert st.state == upload.DONE and os.path.exists(st.url)
        with zipfile.ZipFile(st.url) as z:
            assert "README.md" in z.namelist() and "upload.json" not in z.namelist()
    finally:
        upload._uploaders.pop("shelf", None)


# ------------------------------------------------------------------ the server's housekeeping

def _call(method, url, token):
    req = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _uploaded(tmp_path, settings) -> str:
    c = _saved_capture(tmp_path)
    q = upload.UploadQueue(settings)
    q.enqueue(c.folder)
    q.run_once()
    st = q.status(c.folder)
    assert st.state == upload.DONE, st
    return st.key


def test_deleting_a_capture_needs_an_admin_token(tmp_path, settings, server):
    base, cfg = server
    key = _uploaded(tmp_path, settings)
    assert _call("DELETE", f"{base}/api/captures/{key}", "read-token")[0] == 403      # no admin tokens: nobody can
    assert _call("DELETE", f"{base}/api/captures/{key}", "both")[0] == 403
    cfg.admin_tokens.append("admin-token")
    assert _call("GET", f"{base}/api/whoami", "admin-token") == (200, {
        "can_upload": False, "can_read": False, "can_delete": True, "server": cfg.name, "version": "2"})
    assert _call("GET", f"{base}/api/captures", "admin-token")[0] == 403               # it can't read either
    assert _call("DELETE", f"{base}/api/captures/{key}", "admin-token") == (200, {"deleted": key})
    assert _call("GET", f"{base}/api/captures/{key}", "read-token")[0] == 404
    assert json.loads(_get(f"{base}/api/captures")[1])["count"] == 0
    assert not os.path.exists(os.path.join(cfg.data, "captures", key))
    assert _call("DELETE", f"{base}/api/captures/{key}", "admin-token")[0] == 404


def test_cleanup_expires_old_captures_and_unfinished_uploads(tmp_path, settings, server):
    base, cfg = server
    key = _uploaded(tmp_path, settings)
    store = _server_module().Store(cfg)             # a second view of the same folder
    stale = os.path.join(cfg.data, "incoming", "0" * 32)
    os.makedirs(stale)
    old = time.time() - 30 * 86400
    os.utime(stale, (old, old))
    store.cleanup()                                 # KEEP_DAYS is 0: captures are kept for ever
    assert not os.path.exists(stale)
    assert json.loads(_get(f"{base}/api/captures")[1])["count"] == 1
    cfg.keep_days = 14
    store.cleanup(now=time.time() + 13 * 86400)
    assert json.loads(_get(f"{base}/api/captures")[1])["count"] == 1
    store.cleanup(now=time.time() + 15 * 86400)
    assert json.loads(_get(f"{base}/api/captures")[1])["count"] == 0
    assert not os.path.exists(os.path.join(cfg.data, "captures", key))


def test_an_odd_token_is_refused_not_a_server_error(server):
    base, _ = server
    import http.client
    host, port = base.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    conn.putrequest("GET", "/api/whoami")
    conn.putheader("Authorization", b"Bearer caf\xc3\xa9-token")
    conn.endheaders()
    assert conn.getresponse().status == 401
    conn.close()


def test_a_silent_connection_is_dropped(tmp_path):
    import socket
    mod = _server_module()
    cfg = mod.Config({"DATA_DIR": str(tmp_path / "srv"), "HOST": "127.0.0.1", "PORT": "0", "REQUEST_TIMEOUT": "0.3"})
    srv = mod.make_server(cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(srv.server_address, timeout=5) as sock:
            sock.sendall(b"GET /api/health HTTP/1.1\r\n")        # ... and never the rest
            assert sock.recv(100) == b""                         # the server hung up
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_half_unpacked_revision_from_a_crash_is_cleared(tmp_path, settings, server):
    _base, cfg = server
    c = _saved_capture(tmp_path)
    key = upload.capture_key(c)
    junk = os.path.join(cfg.data, "captures", key, "rev-0001.tmp", "files")
    os.makedirs(junk)
    with open(os.path.join(junk, "leftover.txt"), "w") as f:
        f.write("from the crash")
    q = upload.UploadQueue(settings)
    q.enqueue(c.folder)
    q.run_once()
    assert q.status(c.folder).state == upload.DONE
    files = os.listdir(os.path.join(cfg.data, "captures", key, "rev-0001", "files"))
    assert "leftover.txt" not in files and "capture.log" in files


def test_a_plugin_can_provide_the_server_as_a_fallback(tmp_path, server, monkeypatch):
    base, _ = server
    monkeypatch.delenv("CANOPENER_UPLOAD_TOKEN", raising=False)
    s = Settings(str(tmp_path / "s.json"))
    s.set("upload.allow_http", True)
    u = upload.get_uploader("server")
    assert not u.configured(s) and u.describe(s) == "not set up"
    upload.set_default_server(lambda: (base, "up-token"))
    try:
        assert upload.target(s) is u and u.describe(s) == f"{base} (from a plugin)"
        assert "can upload" in u.check(s)
        assert s.get("upload.url") == "" and upload.load_token() == ""      # nothing written to the unit
        s.set("upload.url", "https://own.example")          # half a setup of its own: still the default pair
        assert u.server(s) == (base, "up-token", True)
        upload.save_token("own-token")                      # both: the unit's own wins
        assert u.server(s) == ("https://own.example", "own-token", False)
        upload.set_default_server(lambda: 1 / 0)            # a broken plugin doesn't break uploading
        assert u.server(s) == ("https://own.example", "own-token", False)
    finally:
        upload.set_default_server(None)
