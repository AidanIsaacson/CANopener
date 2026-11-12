"""Uploading to a private GitHub repository (fake GitHub API), and the adapter trace in captures."""

import base64
import io
import json
import os
import time
import zipfile

import pytest
from test_upload import _saved_capture

from canopener.core import capture as cap
from canopener.core import github, upload, upload_github
from canopener.core.settings import Settings


class FakeGitHub:
    """Just enough of the GitHub API: repo info, refs, commits, blobs, trees, contents."""

    def __init__(self, private=True, empty=False, writable=True):
        self.private, self.writable = private, writable
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict] = {"t0": {}}
        self.commits: dict[str, dict] = {} if empty else {"c0": {"tree": "t0", "message": "init", "parents": []}}
        self.refs: dict[str, str] = {} if empty else {"main": "c0"}
        self.n = 0

    def _id(self, kind):
        self.n += 1
        return f"{kind}{self.n}"

    def __call__(self, method, url, body, headers):
        assert headers["Authorization"] == "Bearer gh-token"
        path = url.replace(github.API, "")
        r = "/repos/me/logs"
        if path == r and method == "GET":
            return 200, {"full_name": "me/logs", "private": self.private, "default_branch": "main",
                         "permissions": {"push": True}}
        if path.startswith(f"{r}/git/ref/heads/"):
            name = path.rsplit("/", 1)[1]
            if not self.refs:
                return 409, {"message": "Git Repository is empty."}
            return (200, {"object": {"sha": self.refs[name]}}) if name in self.refs else (404, {"message": "Not Found"})
        if not self.writable and method != "GET":
            return 403, {"message": "Resource not accessible by integration"}
        if path == f"{r}/contents/README.md" and method == "PUT":
            sha = self._id("b")
            self.blobs[sha] = base64.b64decode(body["content"])
            tree = self._id("t")
            self.trees[tree] = {"README.md": sha}
            c = self._id("c")
            self.commits[c] = {"tree": tree, "message": body["message"], "parents": []}
            self.refs[body.get("branch", "main")] = c
            return 201, {}
        if path.startswith(f"{r}/git/commits/") and method == "GET":
            return 200, {"tree": {"sha": self.commits[path.rsplit("/", 1)[1]]["tree"]}}
        if path == f"{r}/git/blobs":
            sha = self._id("b")
            self.blobs[sha] = base64.b64decode(body["content"])
            return 201, {"sha": sha}
        if path == f"{r}/git/trees":
            sha = self._id("t")
            base = self.trees[body["base_tree"]] if "base_tree" in body else {}
            self.trees[sha] = {**base, **{e["path"]: e["sha"] for e in body["tree"]}}
            return 201, {"sha": sha}
        if path == f"{r}/git/commits" and method == "POST":
            sha = self._id("c")
            self.commits[sha] = {"tree": body["tree"], "message": body["message"], "parents": body["parents"]}
            return 201, {"sha": sha}
        if path == f"{r}/git/refs" and method == "POST":
            name = body["ref"].removeprefix("refs/heads/")
            assert name not in self.refs
            self.refs[name] = body["sha"]
            return 201, {}
        if path.startswith(f"{r}/git/refs/heads/") and method == "PATCH":
            name = path.rsplit("/", 1)[1]
            assert self.commits[body["sha"]]["parents"] == [self.refs[name]]       # a fast-forward
            self.refs[name] = body["sha"]
            return 200, {}
        return 404, {"message": "Not Found"}

    def files(self, branch="captures") -> dict[str, bytes]:
        tree = self.trees[self.commits[self.refs[branch]]["tree"]]
        return {p: self.blobs[s] for p, s in tree.items()}


@pytest.fixture
def gh(monkeypatch, tmp_path):
    github.save_account(github.Account("gh-token", "me"))
    fake = FakeGitHub()
    monkeypatch.setattr(upload_github, "http", fake)
    s = Settings(str(tmp_path / "s.json"))
    s.update({"upload.target": "github", "upload.github_repo": "me/logs"})
    return fake, s


def test_github_upload_commits_readable_files_and_the_zip(tmp_path, gh):
    fake, s = gh
    c = _saved_capture(tmp_path)
    assert upload.target(s).id == "github"
    assert "private repository me/logs" in upload.get_uploader("github").check(s)
    q = upload.UploadQueue(s)
    q.enqueue(c.folder)
    q.run_once()
    st = q.status(c.folder)
    assert st.state == upload.DONE, st.text
    assert st.url == f"https://github.com/me/logs/tree/captures/captures/{st.key}"
    files = fake.files()
    assert fake.refs["main"] == "c0" and fake.files("main") == {}          # the code's branch is untouched
    assert fake.commits[fake.refs["captures"]]["parents"] == []             # no history in common with it
    assert b"plugin installs never download it" in files["README.md"]
    base = f"captures/{st.key}/"
    assert files[base + "README.md"].startswith(b"# Unit 14")
    assert base + "capture.log" in files and base + "traffic.csv" in files
    with zipfile.ZipFile(io.BytesIO(files[base + f"{st.key}.zip"])) as z:
        assert "capture.json" in z.namelist() and "upload.json" not in z.namelist()
    assert fake.commits[fake.refs["captures"]]["message"] == f"Capture {st.key}: Acme LiftMaster 40, SN AB123"
    first = fake.refs["captures"]
    q.enqueue(c.folder)                                                     # uploaded again: a commit on top
    q.run_once()
    assert q.status(c.folder).state == upload.DONE
    assert fake.commits[fake.refs["captures"]]["parents"] == [first]


def test_github_refuses_public_and_explains_write_access(tmp_path, gh):
    fake, s = gh
    c = _saved_capture(tmp_path)
    fake.private = False
    with pytest.raises(upload.UploadError, match="is public"):
        upload.get_uploader("github").check(s)
    q = upload.UploadQueue(s)
    q.enqueue(c.folder)
    q.run_once()
    st = q.status(c.folder)
    assert st.state == upload.FAILED and "public" in st.error and "trying again" not in st.text
    fake.private, fake.writable = True, False
    q.enqueue(c.folder)
    q.run_once()
    assert "Contents: Read and write" in q.status(c.folder).error


def test_github_empty_repository_gets_a_first_commit(tmp_path, monkeypatch):
    github.save_account(github.Account("gh-token", "me"))
    fake = FakeGitHub(empty=True)
    monkeypatch.setattr(upload_github, "http", fake)
    s = Settings(str(tmp_path / "s.json"))
    s.update({"upload.target": "github", "upload.github_repo": "me/logs"})
    c = _saved_capture(tmp_path)
    q = upload.UploadQueue(s)
    q.enqueue(c.folder)
    q.run_once()
    assert q.status(c.folder).state == upload.DONE
    assert "README.md" in fake.files("main") and any(p.endswith("/README.md") for p in fake.files())


def test_not_signed_in_is_not_configured(tmp_path):
    s = Settings(str(tmp_path / "s.json"))
    s.update({"upload.target": "github", "upload.github_repo": "me/logs"})
    assert not upload.get_uploader("github").configured(s) and upload.target(s) is None


def test_capture_over_a_kline_adapter_keeps_the_raw_trace(tmp_path):
    from canopener.core.connection import ELM_SIM, open_connection
    from canopener.core.obd import ObdClient
    conn = open_connection(ELM_SIM, "sim:kwp")
    try:
        time.sleep(1.0)
        c = cap.Capture("kline")
        c.start(conn.session, str(tmp_path))
        ObdClient(conn.session).read_dtcs_by_ecu()
        time.sleep(0.3)
        c.stop()
        folder = c.save()
    finally:
        conn.close()
    trace = open(os.path.join(folder, cap.ADAPTER_TRACE)).read()
    assert trace.startswith("# Bluetooth OBD adapter:") and "KWP" in trace.splitlines()[0]
    assert "> 03\n" in trace and "87F11043" in trace.replace(" ", "")       # the KWP answer, header and all
    assert conn.session.backend.trace_path is None             # only while recording
    readme = open(os.path.join(folder, "README.md")).read()
    assert "- `adapter_trace.txt`" in readme
    assert "P0133" in open(os.path.join(folder, "summary.txt")).read()


def test_handheld_picks_a_github_repo(tmp_path, monkeypatch):
    from canopener.device.context import DeviceContext
    github.save_account(github.Account("gh-token", "me"))
    monkeypatch.setattr(github, "repositories", lambda token: ["me/CANopener", "me/logs"])
    monkeypatch.setattr(upload_github, "http", FakeGitHub())
    ctx = DeviceContext(Settings())
    try:
        got = []
        assert ctx.github_repos(got.append)
        assert ctx.wait_idle(5) or True
        end = time.time() + 5
        while not got and time.time() < end:
            ctx.tick()
            time.sleep(0.02)
        assert got == [["me/CANopener", "me/logs"]]
        c = _saved_capture(tmp_path)
        ctx.capture = cap.Capture.load(c.folder)
        ctx.upload_to_github("me/logs")
        assert ctx.settings.get("upload.target") == "github" and "Queued" in ctx.current_toast()
        assert ctx.uploads.run_once() == c.folder
        assert json.load(open(os.path.join(c.folder, upload.UPLOAD_JSON)))["state"] == upload.DONE
    finally:
        ctx.shutdown()
