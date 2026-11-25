#!/bin/bash
# Keeps `claude remote-control` running: installs Claude Code into the home folder the first time
# (it updates itself there), asks for a sign-in when there isn't one, and starts Remote Control
# again whenever it stops (it gives up after about 10 minutes without a connection).
export PATH="$HOME/.local/bin:$PATH"
cd /work || exit 1
while true; do
    if ! command -v claude > /dev/null; then
        echo "Installing Claude Code..."
        curl -fsSL https://claude.ai/install.sh | bash || { echo "install failed; trying again in 5 min"; sleep 300; continue; }
    fi
    if ! claude auth status > /dev/null 2>&1; then
        echo "Claude Code isn't signed in. Sign in with the claude.ai account that should own these sessions:"
        claude auth login || { sleep 60; continue; }
    fi
    echo "$(date '+%F %T') starting Remote Control (${RC_NAME:-upload server})"
    claude remote-control --name "${RC_NAME:-upload server}" --permission-mode "${RC_PERMISSION_MODE:-default}"
    echo "$(date '+%F %T') Remote Control stopped (exit $?); starting it again in 30 s"
    sleep 30
done
