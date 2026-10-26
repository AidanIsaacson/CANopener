#!/bin/bash
# Bring the latest main branch into this branch (the Raspberry Pi / handheld branch).
#
#   scripts/sync-from-main.sh          # merge origin/main, then run lint + tests
#   scripts/sync-from-main.sh --push   # ... and push if everything passes
#
# A merge (not a rebase) so the branch's history stays as it was and anyone with a checkout of
# it can keep pulling. The handheld code is mostly new files (canopener/device/, core/measure.py,
# core/meters.py, ...), so merges are usually clean; where both sides changed the same lines git
# stops and lists the files: fix them, `git add` them, `git commit`, and run this again.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$branch" = main ]; then
    echo "On main: switch to the Pi branch first." >&2
    exit 2
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Uncommitted changes: commit or stash them first." >&2
    exit 2
fi
git fetch origin main
behind="$(git rev-list --count HEAD..origin/main)"
if [ "$behind" = 0 ]; then
    echo "Already up to date with main."
else
    echo "Merging $behind commit(s) from main into $branch"
    git merge --no-edit origin/main
fi
ruff check .
pytest -q
if [ "${1:-}" = "--push" ]; then
    git push origin "$branch"
fi
