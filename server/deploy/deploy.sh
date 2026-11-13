#!/bin/sh
# Sync updated code from the development machine to the LXC and restart the
# service. Run from the repo root on the dev machine:
#   sh server/deploy/deploy.sh <container-host>
set -eu

TARGET=${1:?usage: deploy.sh <container-host>}
test -d server/python/loralarm || { echo "run from the repo root"; exit 1; }

rsync -a --delete \
    --exclude .venv --exclude __pycache__ --exclude loralarm.db \
    --exclude tests --exclude devices.json \
    server/python/ "$TARGET:/opt/loralarm/python/"
rsync -a --delete PWA/web/ "$TARGET:/opt/loralarm/web/"

ssh "$TARGET" 'chown -R loralarm:loralarm /opt/loralarm && systemctl restart loralarm'
echo "restarted. Journal: ssh '"$TARGET"' journalctl -u loralarm -f"
