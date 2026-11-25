#!/bin/bash
# Runs rc-loop.sh in a tmux session called "rc", so you can look at it (and sign in the first time):
#   docker exec -it claude-remote tmux attach -t rc        (Ctrl+B then D leaves it running)
# The container stops when that session ends, and Docker starts it again (restart: unless-stopped).
export PATH="$HOME/.local/bin:$PATH"
tmux new-session -d -s rc /usr/local/bin/rc-loop.sh
while tmux has-session -t rc 2>/dev/null; do sleep 30; done
