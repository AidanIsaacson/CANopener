# Upload server

Receives saved captures from CANopener (desktop app, handheld or `canopener-cli upload`) and
serves them back, so a capture recorded at the machine can be read somewhere else a minute later:
by you, or by an AI assistant you point at it ("I just uploaded a log from the LiftMaster, serial
AB123: the data doesn't parse, take a look").

It's one standard-library Python file (`server.py`, Python 3.10+, no database). This folder holds
everything needed to run it in Docker behind nginx and a Cloudflare tunnel:

| File | What it is |
|---|---|
| `server.py` | the server |
| `Dockerfile`, `compose.yaml` | the container (`docker compose up -d --build`); captures go in `./data` |
| `env.example` | its settings: copy to `.env` and fill in the tokens |
| `nginx-site.conf.example` | the nginx site between cloudflared and the container |

What the app sends is a capture folder: the CAN log, the summary, the live-data spreadsheet,
`traffic.csv`, the machine details and a `README.md` describing all of it (docs/USAGE.md, "Uploading
captures"). The app side is `canopener/core/upload.py`.

## Setting it up

The path a request takes: **app → Cloudflare (HTTPS) → cloudflared on the server → nginx
(127.0.0.1:8088) → container (127.0.0.1:8087)**. Nothing listens on a public port.

1. **Tokens.** Make one upload token per unit (desktop, handheld) and one read token for each
   reader (you; an AI assistant's environment), each with
   `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. **Container.** Copy this folder to the server, then `cp env.example .env`, put the tokens in
   `UPLOAD_TOKENS` / `READ_TOKENS` (comma-separated) and set `PUBLIC_URL` to the tunnel's hostname.
   Run `docker compose up -d --build`. Check it: `curl http://127.0.0.1:8087/api/health`.
3. **nginx.** Install `nginx-site.conf.example` as a site, with `server_name` changed to the
   hostname, then reload nginx. Check it: `curl http://127.0.0.1:8088/api/health`.
4. **Cloudflare tunnel.** Add a public hostname to the tunnel (for example `logs.example.com`)
   with the service `http://127.0.0.1:8088`. In a cloudflared `config.yml` that's:

   ```yaml
   ingress:
     - hostname: logs.example.com
       service: http://127.0.0.1:8088
     - service: http_status:404
   ```

   Don't put a Cloudflare Access login page in front of `/api`: the app and the readers can't
   click through a login page. The tokens are what protects it. (An Access policy with a
   *service token* works, but then every client needs that token too.)
5. **Check from outside:** `curl https://logs.example.com/api/health`, then with a token:
   `curl -H "Authorization: Bearer $TOKEN" https://logs.example.com/api/whoami`.
6. **Point the app at it.** Desktop: **Log > Upload settings…**, then enter the address and
   an upload token and press **Test**. Handheld or headless: `canopener-cli upload --setup`.

Updating: copy the new `server.py` over and run `docker compose up -d --build`. Captures in
`./data` are kept. Back up `./data` like any other folder.

## Reading captures

Every reader call needs `Authorization: Bearer <a READ token>`.

```bash
B=https://logs.example.com; H="Authorization: Bearer $READ_TOKEN"
curl -sH "$H" "$B/api/captures?limit=10"                 # newest uploads first
curl -sH "$H" "$B/api/captures?q=liftmaster+ab123"       # search machine, serial, name, concern
curl -sH "$H" "$B/api/captures/<key>"                    # one capture: manifest + its files
curl -sH "$H" "$B/api/captures/<key>/files/README.md"    # start here: machine, concern, undecoded traffic
curl -sH "$H" "$B/api/captures/<key>/files/capture.log?max_bytes=20000"   # the first 20 kB of the log
curl -sH "$H" -o capture.zip "$B/api/captures/<key>/bundle.zip"           # everything
```

**From an AI assistant's cloud environment** (Claude Code on the web, for example), the
environment needs two things: the tunnel's hostname in its allowed network hosts, and a
read token stored as a network secret for that host, sent as `Authorization: Bearer`. The
assistant can then use the calls above. Give it a read token, never an upload token, so it can't
add or change anything.

## API

All JSON. Errors are `{"error": "..."}` with a 4xx / 5xx status. Tokens go in
`Authorization: Bearer <token>`. An upload token can only upload, a read token can only read (put
one token in both lists for both).

| Call | Token | Body / query | Answer |
|---|---|---|---|
| `GET /api/health` | none | | `{ok, version}` |
| `GET /api/whoami` | any | | `{can_upload, can_read, server, version}` |
| `POST /api/uploads` | upload | `{key, size, sha256, part_size, manifest}` | `201 {upload_id, part_size, parts, received: [part numbers]}` |
| `GET /api/uploads/<id>` | upload | | same as above |
| `PUT /api/uploads/<id>/parts/<n>` | upload | the part's bytes; header `X-Content-SHA256` | same as above |
| `POST /api/uploads/<id>/complete` | upload | `{}` | `{key, revision, url, files}` |
| `GET /api/captures` | read | `q` (words, all must match), `since` (Unix time), `limit` (50) | `{count, captures: [summary, ...]}` newest first |
| `GET /api/captures/<key>` | read | | the manifest, plus `files`, `revisions`, `links` |
| `GET /api/captures/<key>/files/<name>` | read | `max_bytes`: only the first N bytes | the file (`X-File-Size` has its full size) |
| `GET /api/captures/<key>/bundle.zip` | read | | the zip as uploaded |

**Uploading** is in parts, so no request is bigger than `part_size` (the server clamps it to 64 kB
to `MAX_PART_MB`, 64 MB by default; Cloudflare refuses requests over 100 MB):

1. `POST /api/uploads` with the bundle's `key` (letters, digits, `.`, `-`, `_`; up to 160), its
   `size` and `sha256`, and a `manifest` (what the index shows: machine, concern, question, times,
   frame counts; see `manifest()` in `canopener/core/upload.py`). Posting the same key, size and
   checksum again returns the same `upload_id` and the parts already received, which is how a
   dropped upload resumes.
2. `PUT` each missing part `n` (0-based; every part is `part_size` bytes but the last).
3. `POST .../complete`: the server joins the parts, checks the checksum, unpacks the zip (flat file
   names only, nothing that would land outside its folder) and indexes it.

The same `key` uploaded again (the capture was relabelled or trimmed) becomes a new **revision**
of that capture. The latest one is served, and the last `KEEP_REVISIONS` earlier ones are kept on
disk. Unfinished uploads are deleted after `INCOMING_DAYS`.

**Keys** are made by the app: `<start time>_<name or machine>_<unit id>`, for example
`20261112_140210_Unit_14_3fa9c1`. The last part is a random ID for the installation that sent it
(`~/.config/canopener/device-id`).

## On disk

```text
data/index.json                         every capture's summary, newest upload first
data/captures/<key>/rev-0001/           bundle.zip, manifest.json, files/ (the unpacked capture)
data/incoming/<upload id>/              parts of an upload still in progress
```

To delete a capture, remove its `data/captures/<key>` folder and its entry in `data/index.json`
(stop the container first, or it may write the index back).
