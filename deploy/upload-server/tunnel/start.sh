#!/bin/sh
# Host keys live in /hostkeys (a volume) so units keep recognising this server after a rebuild.
set -e
[ -f /hostkeys/ssh_host_ed25519_key ] || ssh-keygen -q -t ed25519 -N "" -f /hostkeys/ssh_host_ed25519_key
sed -i "s/^Port .*/Port ${TUNNEL_PORT:-47822}/" /etc/ssh/sshd_config
exec /usr/sbin/sshd -D -e
