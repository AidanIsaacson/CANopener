"""Uploading captures to a private GitHub repository with the device's GitHub sign-in.

For when there's no upload server (or no laptop): the handheld already signs in to GitHub with no
keyboard (``core.github``, approved on a phone), so it can commit a saved capture to a private
repository of yours. Captures go on their own branch (``upload.github_branch``, "captures"),
started with no history in common with the code, so the repository can be one that's also a plugin
source (``core.plugin_repos`` fetches only its own branch). One upload is one commit, under
``captures/<key>/``:

* the files worth reading straight away (``README.md``, ``summary.txt``, ``capture.json``,
  ``markers.csv``, ``traffic.csv``, ``adapter_trace.txt``, and ``capture.log`` when it's small),
* ``<key>.zip``: the whole folder, as the upload server would get it.

It refuses a public repository: captures hold serial numbers, customers and VINs. The sign-in
needs write access: the CANopener GitHub App's permission "Contents: Read and write" and the App
installed on that repository (docs/USAGE.md, "Machine details and uploading captures").

Git Data API (blobs, a tree, a commit, then moving the branch), so files up to GitHub's 100 MB
limit work and nothing is cloned. Standard library only; the token goes in the Authorization
header of these API calls only.
"""

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from . import github
from .upload import USER_AGENT, Bundle, Uploader, UploadError, register_uploader

READABLE = ("README.md", "summary.txt", "capture.json", "markers.csv", "traffic.csv", "adapter_trace.txt",
            "report.txt")
SMALL_LOG = 20 * 1024 * 1024        # capture.log is committed as a file too when it's this small
MAX_FILE = 95 * 1024 * 1024         # GitHub refuses files over 100 MB
TIMEOUT = 120
BRANCH_README = (b"# CANopener captures\n\nUploaded by CANopener: one folder per capture under captures/, "
                 b"each with a README.md.\nThis branch has no history in common with the others: plugin "
                 b"installs never download it.\n")


def _urllib_http(method: str, url: str, body: dict | None, headers: dict) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT, **({"Content-Type": "application/json"} if data else {}), **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except (urllib.error.URLError, OSError) as e:
        raise UploadError(f"Couldn't reach GitHub (no internet?): {getattr(e, 'reason', e)}") from e
    try:
        return status, json.loads(raw.decode() or "null")
    except ValueError:
        return status, None


http = _urllib_http


def _token() -> str:
    acct = github.load_account()
    return acct.token if acct else ""


class GitHubUploader(Uploader):
    id = "github"
    name = "GitHub (a private repository)"

    def __init__(self, token_fn=_token):
        self.token_fn = token_fn

    def configured(self, settings) -> bool:
        return bool(self.token_fn()) and "/" in str(settings.get("upload.github_repo") or "")

    def describe(self, settings) -> str:
        repo = settings.get("upload.github_repo") or ""
        return f"github.com/{repo}" if repo else "GitHub (no repository picked)"

    # --- API ---
    def _api(self, method: str, path: str, body: dict | None = None, ok=(200, 201)) -> object:
        token = self.token_fn()
        if not token:
            raise UploadError("Not signed in to GitHub (System > Software > Plugins > Sign in).", retry=False)
        status, js = http(method, github.API + path, body, {"Authorization": f"Bearer {token}"})
        if status in ok:
            return js
        msg = (js or {}).get("message") if isinstance(js, dict) else ""
        if status == 401:
            raise UploadError("GitHub didn't accept the sign-in (expired or revoked): sign in again.", retry=False)
        if status in (403, 404) and "rate limit" not in (msg or "").lower():
            raise UploadError(f"GitHub refused ({msg or status}). The sign-in needs write access to the "
                              "repository: in the CANopener GitHub App's settings give it Contents: Read and "
                              "write, accept the new permission, and install it on that repository.", retry=False)
        if status in (409, 422):
            raise UploadError(f"GitHub refused: {msg or status}", retry=True)
        raise UploadError(f"GitHub error: {msg or f'HTTP {status}'}")

    def _repo(self, settings) -> str:
        repo = str(settings.get("upload.github_repo") or "").strip().strip("/")
        if repo.count("/") != 1:
            raise UploadError("No GitHub repository picked for uploads.", retry=False)
        return "/".join(urllib.parse.quote(p, safe="") for p in repo.split("/"))

    def check(self, settings) -> str:
        repo = self._repo(settings)
        info = self._api("GET", f"/repos/{repo}")
        if not info.get("private"):
            raise UploadError(f"{info.get('full_name', repo)} is public: captures hold serial numbers and "
                              "customers, so pick a private repository.", retry=False)
        if not (info.get("permissions") or {}).get("push", True):
            raise UploadError("The sign-in can read that repository but not write to it.", retry=False)
        return f"Uploads go to the private repository {info.get('full_name', repo)}."

    def _branch_head(self, repo: str, branch: str) -> str | None:
        status, js = http("GET", f"{github.API}/repos/{repo}/git/ref/heads/{urllib.parse.quote(branch)}", None,
                          {"Authorization": f"Bearer {self.token_fn()}"})
        if status == 200:
            return js["object"]["sha"]
        if status in (404, 409):           # an empty repository: no branch yet
            return None
        self._api("GET", f"/repos/{repo}/git/ref/heads/{urllib.parse.quote(branch)}")     # raises the right error
        return None

    def upload(self, bundle: Bundle, settings, progress=None) -> dict:
        repo = self._repo(settings)
        info = self._api("GET", f"/repos/{repo}")
        if not info.get("private"):
            raise UploadError(f"{info.get('full_name', repo)} is public: pick a private repository for uploads.",
                              retry=False)
        default = info.get("default_branch") or "main"
        branch = str(settings.get("upload.github_branch") or "").strip() or default
        files = self._files(bundle, bundle.folder)
        total = sum(os.path.getsize(p) for _, p in files) or 1
        sent = 0
        if progress:
            progress(0, total)
        head = self._branch_head(repo, branch)
        if head is None and self._branch_head(repo, default) is None:
            # An empty repository takes no blobs: the Contents API makes its first commit.
            self._api("PUT", f"/repos/{repo}/contents/README.md", {
                "message": "CANopener captures",
                "content": base64.b64encode(BRANCH_README).decode(), "branch": default})
            head = self._branch_head(repo, branch) if branch == default else None
        tree = []
        if head is None:                    # a new branch, with no history of its own (not the code's)
            readme = self._api("POST", f"/repos/{repo}/git/blobs",
                               {"content": base64.b64encode(BRANCH_README).decode(), "encoding": "base64"})
            tree.append({"path": "README.md", "mode": "100644", "type": "blob", "sha": readme["sha"]})
        for name, path in files:
            with open(path, "rb") as f:
                blob = self._api("POST", f"/repos/{repo}/git/blobs",
                                 {"content": base64.b64encode(f.read()).decode(), "encoding": "base64"})
            tree.append({"path": f"captures/{bundle.key}/{name}", "mode": "100644", "type": "blob",
                         "sha": blob["sha"]})
            sent += os.path.getsize(path)
            if progress:
                progress(sent, total)
        body = {"tree": tree}
        if head is not None:
            body["base_tree"] = self._api("GET", f"/repos/{repo}/git/commits/{head}")["tree"]["sha"]
        new_tree = self._api("POST", f"/repos/{repo}/git/trees", body)["sha"]
        summary = bundle.manifest.get("machine_summary") or bundle.manifest.get("name") or bundle.key
        commit = self._api("POST", f"/repos/{repo}/git/commits", {
            "message": f"Capture {bundle.key}: {summary}", "tree": new_tree,
            "parents": [head] if head else []})["sha"]
        if head is None:
            self._api("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
        else:
            self._api("PATCH", f"/repos/{repo}/git/refs/heads/{urllib.parse.quote(branch)}", {"sha": commit})
        full = info.get("full_name", repo)
        return {"key": bundle.key, "url": f"https://github.com/{full}/tree/{branch}/captures/{bundle.key}"}

    @staticmethod
    def _files(bundle: Bundle, src: str) -> list[tuple[str, str]]:
        if bundle.size > MAX_FILE:
            raise UploadError(f"This capture is {bundle.size / 1048576:.0f} MB zipped, more than GitHub takes in "
                              "one file (100 MB): trim it first (More > Trim).", retry=False)
        out = []
        for name in READABLE + ("capture.log",):
            p = os.path.join(src, name)
            if src and os.path.isfile(p) and (name != "capture.log" or os.path.getsize(p) <= SMALL_LOG):
                out.append((name, p))
        out.append((f"{bundle.key}.zip", bundle.path))
        return out


register_uploader(GitHubUploader())
