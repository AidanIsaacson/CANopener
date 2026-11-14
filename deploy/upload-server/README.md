# Upload server

Receives saved captures from CANopener (desktop app, handheld or `canopener-cli upload`) and
serves them back, so a capture recorded at the machine can be read somewhere else a minute later:
by you, or by an AI assistant you point at it ("I just uploaded a log from the LiftMaster, serial
AB123: the data doesn't parse, take a look").

It's one standard-library Python file (`server.py`, Python 3.10+, no database). This folder holds
everything needed to run it in Docker behind a reverse proxy (nginx and a Cloudflare tunnel, or a
proxy you already run such as Nginx Proxy Manager):

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
   Make the data folder and give it to the container's user, which is not root:
   `mkdir -p data && sudo chown 10001 data`. Then run `docker compose up -d --build`.
   Check it: `curl http://127.0.0.1:8087/api/health`.
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

### Behind a proxy you already run (no tunnel)

If the server already has a reverse proxy on ports 80/443 with its own certificates (Nginx Proxy
Manager, Caddy, Traefik, plain nginx), use that instead of steps 3 and 4:

1. **DNS:** point a hostname at the server like your other sites. Use a dash, not an underscore
   (`can-logs`, not `can_logs`): Let's Encrypt won't issue a certificate for a name with an
   underscore.
2. **Let the proxy reach the container.** `compose.yaml` publishes it on `127.0.0.1:8087` only. A
   proxy running on the host itself can use that. A proxy in its own container (on Docker's
   default bridge) can't, so publish the port on the bridge's address as well, in a
   `compose.override.yaml` next to `compose.yaml`:

   ```yaml
   services:
     canopener-upload:
       ports:
         - "172.17.0.1:8087:8080"    # docker0: this host and its containers, not the LAN
   ```

   Then forward to `http://172.17.0.1:8087`.
3. **The proxy host:** HTTPS with a certificate, plain http redirected to https ("Force SSL"), no
   login page or access list, and the settings from `nginx-site.conf.example` (in Nginx Proxy
   Manager: the **Advanced** tab):

   ```nginx
   client_max_body_size 80m;
   proxy_request_buffering off;
   proxy_buffering off;
   proxy_read_timeout 300s;
   proxy_send_timeout 300s;
   ```
4. Carry on with step 5.

On a server without systemd or Python (Unraid, a NAS), make the tokens with
`openssl rand -base64 32 | tr '+/' '-_' | tr -d '='`.

## Deleting captures and keeping the disk from filling

Nothing is deleted unless you ask for it:

- **One capture:** put a token in `ADMIN_TOKENS` (a separate one: it can delete, and nothing
  else), then `curl -X DELETE -H "Authorization: Bearer $ADMIN_TOKEN" $B/api/captures/<key>`.
  With `ADMIN_TOKENS` empty (the default) nothing can delete over the network.
- **Old captures:** set `KEEP_DAYS=90` and captures are deleted 90 days after their last upload.
  The default, `0`, keeps them for ever.
- Unfinished uploads are deleted after `INCOMING_DAYS`. This and `KEEP_DAYS` are checked when the
  server starts and every hour after.

## Machine profiles

Units also send in the profiles made on them (the handheld does it when one is saved, if
**upload.profiles** is on; `canopener-cli profiles --upload ID` or `--upload all` from a computer).
They are kept as `data/profiles/<name>_<unit>/rev-NNNN.json`, a new revision for every change, so
profiles for the same kind of machine from different units can be compared and merged by hand.
Read them back with a read token: `GET /api/profiles` (`?q=` searches name, machine, unit) and
`GET /api/profiles/<key>` (`?rev=N` for an older one).

## Remote help

A unit on a phone hotspot can't be reached from outside, but it can call out. **System > Remote
help > Start** on the handheld opens a session here and keeps asking for work; someone holding a
token from `REMOTE_TOKENS` lists the sessions and sends commands (`canopener-cli remote list`,
`remote send state`, `remote shot screen.png`, `remote send request --args '{"requests": ["0100"]}'`).

The server only passes the messages along and keeps them in memory: nothing about a session is
written to disk, and it ends when the unit stops it or its time is up (`REMOTE_MAX_MINUTES`). The
unit decides what it will do: it has a fixed list of actions (its state, a screenshot, its logs
and profiles, fault codes, adapter commands, requests to the vehicle), reads run straight away and
are shown on its screen, and anything that could change the vehicle waits for a Yes from the
person holding it. A remote token is still as good as standing next to the unit: give it only to
someone you'd hand the unit to. A unit's upload token can open sessions but can't operate one.

## SSH tunnel (optional, for hands-on help)

Remote help covers looking and asking. For a real login to a unit in the field, the handheld can
also open a reverse SSH tunnel (**System > Remote help > Open an SSH tunnel**; it warns first).
That needs the `canopener-tunnel` container and one TCP port reachable from the internet:

1. Forward a high TCP port of your choice (the default here is **47822**) from the router to
   this server. Not port 22, and never to the server's own SSH.
2. In `.env`: `TUNNEL_HOST=` the public name or address units should dial, and `TUNNEL_PORT=` if
   it isn't 47822.
3. `mkdir -p tunnel-hostkeys data/tunnel && sudo chown 10001 data/tunnel` (the upload server writes
   the units' keys there, and it doesn't run as root), then
   `docker compose --profile tunnel up -d --build`.

What a unit can do there is one thing. Its key (made on the unit, enrolled through its upload
token) is written to `data/tunnel/authorized_keys` as
`restrict,port-forwarding,permitlisten="127.0.0.1:<its port>"`: no shell, no passwords, no other
forwarding, and the tunnel's far end listens on this server's own loopback only (the container
uses the host's network for that). So to reach a unit you must already be able to log in to this
server, and then to the unit itself:

```bash
ssh -J you@this-server -p 22201 user@127.0.0.1     # 22201: the unit's port (remote help's "state" shows it)
```

Ports are handed out from `TUNNEL_FIRST_LISTEN_PORT` (22201), one per unit, up to
`TUNNEL_MAX_UNITS` (16). To cut a unit off, delete its entry from `data/tunnel/units.json` and
its line from `data/tunnel/authorized_keys`.

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
`Authorization: Bearer <token>`. An upload token can only upload (captures, profiles, and opening a
remote-help session for itself), a read token can only read, an admin token can only delete, a
remote token can only operate remote-help sessions (put one token in more than one list for more).

| Call | Token | Body / query | Answer |
|---|---|---|---|
| `GET /api/health` | none | | `{ok, version}` |
| `GET /api/whoami` | any | | `{can_upload, can_read, can_delete, server, version}` |
| `POST /api/uploads` | upload | `{key, size, sha256, part_size, manifest}` | `201 {upload_id, part_size, parts, received: [part numbers]}` |
| `GET /api/uploads/<id>` | upload | | same as above |
| `PUT /api/uploads/<id>/parts/<n>` | upload | the part's bytes; header `X-Content-SHA256` | same as above |
| `POST /api/uploads/<id>/complete` | upload | `{}` | `{key, revision, url, files}` |
| `GET /api/captures` | read | `q` (words, all must match), `since` (Unix time), `limit` (50) | `{count, captures: [summary, ...]}` newest first |
| `GET /api/captures/<key>` | read | | the manifest, plus `files`, `revisions`, `links` |
| `GET /api/captures/<key>/files/<name>` | read | `max_bytes`: only the first N bytes | the file (`X-File-Size` has its full size) |
| `GET /api/captures/<key>/bundle.zip` | read | | the zip as uploaded |
| `DELETE /api/captures/<key>` | admin | | `{deleted}`: the capture and all its revisions are gone |
| `PUT /api/profiles/<key>` | upload | the profile (a JSON object with a `name`); header `X-Device` | `{key, revision, ...}`, or the same with `unchanged` |
| `GET /api/profiles` | read | `q` | `{count, profiles: [...]}` newest first |
| `GET /api/profiles/<key>` | read | `rev` | the profile as uploaded |
| `POST /api/remote/tunnel` | upload | `{device, public_key}` (ssh-ed25519) | `{host, port, user, listen_port}`; 404 when no tunnel is set up |
| `POST /api/remote/sessions` | upload | `{device, label, minutes, info}` | `201` the session (`id`, `expires`) |
| `GET /api/remote/sessions/<id>/next` | upload | `wait` (seconds, up to 25) | `{command}`: the next one, or null |
| `POST /api/remote/sessions/<id>/results/<cmd>` | upload | the result (JSON, up to 12 MB) | `{ok}` |
| `GET /api/remote/sessions` | remote | | `{sessions: [...]}` |
| `POST /api/remote/sessions/<id>/commands` | remote | `{action, args}` | `201 {id}` |
| `GET /api/remote/sessions/<id>/results/<cmd>` | remote | `wait` | `{result}`: the unit's answer, or null |
| `DELETE /api/remote/sessions/<id>` | upload or remote | | `{closed}` |

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
data/profiles/index.json                every profile's summary
data/profiles/<key>/rev-0001.json       each version of a profile, as uploaded
```

To delete a capture by hand instead of with `DELETE`, remove its `data/captures/<key>` folder and
its entry in `data/index.json` (stop the container first, or it may write the index back).
