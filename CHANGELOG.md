# Changelog

What changed in each version, newest first. Versions are git tags (`v0.3.2`): to put a unit back
on an older one, `git checkout v0.3.1` in its checkout.

## 0.3.2 (2026-11-12)

- **Settings shared by two programs are no longer lost.** The handheld UI and `canopener-cli`
  can both change `settings.json`: a save now writes only what that program changed, and a
  running app notices the other's changes within about a second. Before, a running app wrote its
  old copy back over anything the command line had changed (`canopener-cli upload --setup`).
- **Upload server (version 2):**
  - a connection that goes quiet is dropped after `REQUEST_TIMEOUT` (120 s) instead of holding
    a thread for ever;
  - unfinished uploads are cleaned up every hour, not only when the server starts;
  - `DELETE /api/captures/<key>` with a token from the new `ADMIN_TOKENS`, and `KEEP_DAYS` to
    expire old captures (both off by default);
  - a token with unusual characters gets a 401 instead of a server error;
  - a revision left half unpacked by a crash no longer mixes into the next upload;
  - the log shows the caller's address from `X-Forwarded-For`;
  - the README covers running behind a proxy you already have (Nginx Proxy Manager and the
    like) and the data folder's owner.
- **Plugins can provide the upload server.** `core.upload.set_default_server` lets a private
  plugin hand every unit the server's address and upload token; a unit's own still win.
- `canopener-cli upload` shows small captures in kB and no longer leaves its progress line stuck
  to the result (or in a log file).
- CI also tests Python 3.13, which the Raspberry Pi runs.
- Removed `legacy/` (the first Tkinter scripts). They're in the git history before this version.
