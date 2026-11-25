#!/bin/bash
# Auto-deploy for the upload server: keeps a server's copy of this folder in step with `main`.
#
# Run it from cron every minute (README, "How changes reach the server"). Every tick it carries out
# requests left in $BASE/requests (see below); every $EVERY minutes it fetches main into a
# dedicated checkout and, when deploy/upload-server/ changed since the last deploy, copies the code
# files into the live folder, rebuilds the container, checks /api/health and, if that fails, puts
# the previous files back and rebuilds again. .env, data/, compose.override.yaml and anything else
# not listed in FILES are never touched. Everything it does goes to $BASE/deploy.log.
#
# Settings come from $BASE/auto-deploy.conf (shell syntax), next to this script:
#   LIVE=/srv/canopener-upload        the folder docker compose runs in (.env lives there)
#   REPO_URL=https://github.com/<owner>/CANopener.git
#   BRANCH=main
#   HEALTH=http://127.0.0.1:8087/api/health
#   EVERY=10                          minutes between checks of the branch
#   OWNER=                            chown the copied files to this (uid:gid), or leave empty
#
# Requests: a file $BASE/requests/<name>.req whose first word is one of
#   deploy    check the branch now and deploy if anything changed (also retries a failed commit)
#   rebuild   docker compose up -d --build, with the health check and roll-back
#   restart   docker compose restart
#   logs      the container's last 300 log lines
#   status    docker compose ps, the deployed commit and the health check
# The answer goes to <name>.out next to it and the .req is removed. Nothing else is run, whatever
# the file says, so a user (or a container) that may only write to requests/ can ask for these
# and nothing more.
#
# Usage: auto-deploy.sh           a cron tick
#        auto-deploy.sh --now     check the branch now (as the 'deploy' request)
set -u
BASE="$(cd "$(dirname "$0")" && pwd)"
LIVE=""; REPO_URL=""; BRANCH=main; HEALTH=http://127.0.0.1:8087/api/health; EVERY=10; OWNER=""
# shellcheck source=/dev/null
[ -f "$BASE/auto-deploy.conf" ] && . "$BASE/auto-deploy.conf"
REPO="$BASE/repo"
LOG="$BASE/deploy.log"
REQ="$BASE/requests"
FILES="server.py Dockerfile compose.yaml env.example README.md nginx-site.conf.example tunnel"
SRC=deploy/upload-server

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
die() { log "$*"; exit 1; }

[ -n "$LIVE" ] && [ -d "$LIVE" ] || die "LIVE isn't set (or isn't a folder) in $BASE/auto-deploy.conf"
exec 9> "$BASE/.lock"
flock -n 9 || exit 0                      # the last run is still going
mkdir -p "$REQ"

# Keep the log from growing for ever.
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 1048576 ]; then
    tail -n 3000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

compose() { (cd "$LIVE" && timeout 900 docker compose "$@") >> "$LOG" 2>&1; }

healthy() {   # answering, and still answering 15 s later (a server that crashes on start fails this)
    for _ in $(seq 1 18); do
        sleep 5
        if curl -fsS --max-time 4 "$HEALTH" > /dev/null 2>&1; then
            sleep 15
            curl -fsS --max-time 4 "$HEALTH" > /dev/null 2>&1
            return
        fi
    done
    return 1
}

deployed() { cat "$LIVE/.deployed-commit" 2>/dev/null || echo none; }

# Copy the code files of commit $1 into the live folder.
install_files() {
    local stage="$BASE/stage"
    rm -rf "$stage" && mkdir -p "$stage"
    git -C "$REPO" archive "$1" "$SRC" | tar -x -C "$stage" || return 1
    for f in $FILES; do
        [ -e "$stage/$SRC/$f" ] || continue
        rm -rf "${LIVE:?}/$f"
        cp -a "$stage/$SRC/$f" "$LIVE/$f"
        [ -n "$OWNER" ] && chown -R "$OWNER" "$LIVE/$f"
    done
    rm -rf "$stage"
}

# Save the live code files, so a failed deploy can put them back.
save_previous() {
    rm -rf "$BASE/previous" && mkdir -p "$BASE/previous"
    for f in $FILES; do
        [ -e "$LIVE/$f" ] && cp -a "$LIVE/$f" "$BASE/previous/$f"
    done
    deployed > "$BASE/previous/.commit"
}

restore_previous() {
    for f in $FILES; do
        [ -e "$BASE/previous/$f" ] || continue
        rm -rf "${LIVE:?}/$f"
        cp -a "$BASE/previous/$f" "$LIVE/$f"
    done
}

# Rebuild and check; on failure put the previous files back and rebuild again. $1: what for the log.
rebuild_or_roll_back() {
    if compose up -d --build && healthy; then
        return 0
    fi
    log "FAILED: $1 isn't healthy; rolling back to $(cat "$BASE/previous/.commit" 2>/dev/null)"
    restore_previous
    if compose up -d --build && healthy; then
        log "rolled back; the server is healthy again"
    else
        log "ROLL-BACK FAILED TOO: the server is down. Look at 'docker compose logs' in $LIVE"
    fi
    return 1
}

check_branch() {   # $1 = force (retry a commit that failed before)
    if [ ! -d "$REPO/.git" ]; then
        [ -n "$REPO_URL" ] || die "REPO_URL isn't set in $BASE/auto-deploy.conf"
        timeout 300 git clone -q --branch "$BRANCH" "$REPO_URL" "$REPO" >> "$LOG" 2>&1 || die "git clone failed"
        log "cloned $REPO_URL ($BRANCH)"
    fi
    timeout 120 git -C "$REPO" fetch -q origin "$BRANCH" >> "$LOG" 2>&1 || die "git fetch failed"
    git -C "$REPO" merge -q --ff-only "origin/$BRANCH" >> "$LOG" 2>&1 || die "the checkout can't fast-forward to origin/$BRANCH"
    local new cur
    new="$(git -C "$REPO" rev-parse HEAD)"
    cur="$(deployed)"
    [ "$new" = "$cur" ] && return 0
    if [ "$cur" != none ] && git -C "$REPO" cat-file -e "$cur^{commit}" 2>/dev/null \
            && git -C "$REPO" diff --quiet "$cur" "$new" -- "$SRC"; then
        echo "$new" > "$LIVE/.deployed-commit"     # main moved, but not this folder
        return 0
    fi
    if [ "$1" != force ] && [ "$(cat "$BASE/failed-commit" 2>/dev/null)" = "$new" ]; then
        return 0                                    # tried and failed; wait for a new commit or a 'deploy' request
    fi
    log "deploying ${new:0:7} ($(git -C "$REPO" log -1 --format=%s "$new")) over ${cur:0:7}"
    save_previous
    install_files "$new" || { log "FAILED: couldn't unpack ${new:0:7}"; restore_previous; return 1; }
    if rebuild_or_roll_back "${new:0:7}"; then
        echo "$new" > "$LIVE/.deployed-commit"
        rm -f "$BASE/failed-commit"
        log "deployed ${new:0:7}: healthy"
    else
        echo "$new" > "$BASE/failed-commit"
        return 1
    fi
}

answer() {   # $1 = the request's name, $2 = its first word
    local out="$REQ/$1.out"
    log "request $1: $2"
    case "$2" in
        deploy)  check_branch force; echo "deployed commit: $(deployed)" > "$out"; tail -n 20 "$LOG" >> "$out" ;;
        rebuild) save_previous
                 if rebuild_or_roll_back "rebuild"; then echo "rebuilt: healthy" > "$out"
                 else echo "rebuild failed and was rolled back" > "$out"; fi
                 tail -n 20 "$LOG" >> "$out" ;;
        restart) (cd "$LIVE" && timeout 300 docker compose restart) > "$out" 2>&1
                 if healthy; then echo "healthy" >> "$out"; else echo "NOT healthy" >> "$out"; fi ;;
        logs)    (cd "$LIVE" && timeout 60 docker compose logs --no-color --tail 300) > "$out" 2>&1 ;;
        status)  { (cd "$LIVE" && timeout 60 docker compose ps); echo "deployed commit: $(deployed)"
                   echo "failed commit: $(cat "$BASE/failed-commit" 2>/dev/null || echo none)"
                   curl -sS --max-time 5 "$HEALTH"; } > "$out" 2>&1 ;;
        *)       echo "unknown request '$2' (deploy, rebuild, restart, logs, status)" > "$out" ;;
    esac
    [ -n "$OWNER" ] && chown "$OWNER" "$out"
}

for f in "$REQ"/*.req; do
    [ -f "$f" ] || continue
    name="$(basename "$f" .req)"
    word="$(head -c 64 "$f" | tr -cs 'a-z' ' ' | awk '{print $1}')"
    rm -f "$f"
    case "$name" in *[!A-Za-z0-9._-]*) log "request with an odd name ignored"; continue ;; esac
    answer "$name" "$word"
done

if [ "${1:-}" = "--now" ]; then
    check_branch force
elif [ $(( 10#$(date +%M) % EVERY )) -eq 0 ]; then
    check_branch normal
fi
exit 0
