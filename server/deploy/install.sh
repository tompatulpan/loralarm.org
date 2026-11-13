#!/bin/sh
# Install the loralarm service inside a Debian LXC (native systemd, no Docker).
# Run once as root after the code has been rsynced to /opt/loralarm (see README).
# Idempotent: safe to re-run after a code sync (use deploy.sh for that).
set -eu

PYTHON=/opt/loralarm/python
WEB=/opt/loralarm/web
LIB=/var/lib/loralarm
CONF=/etc/loralarm
HERE=$(dirname "$0")

# --- system user and directories ------------------------------------------
id -u loralarm >/dev/null 2>&1 || \
    useradd --system --home-dir /opt/loralarm --shell /usr/sbin/nologin loralarm

mkdir -p "$LIB" "$CONF"
chown loralarm:loralarm "$LIB"

# --- packages --------------------------------------------------------------
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv tzdata mosquitto-clients sqlite3 ca-certificates

# --- code check and venv ---------------------------------------------------
test -d "$PYTHON/loralarm" || {
    echo "ERROR: $PYTHON/loralarm missing - rsync the code first (README step 1)" >&2
    exit 1
}
test -f "$WEB/index.html" || {
    echo "ERROR: $WEB missing - the PWA frontend must be synced too" >&2
    exit 1
}
chown -R loralarm:loralarm /opt/loralarm

python3 -m venv /opt/loralarm/.venv
/opt/loralarm/.venv/bin/pip install --quiet --upgrade pip
/opt/loralarm/.venv/bin/pip install --quiet \
    -r "$PYTHON/requirements.txt" -r "$PYTHON/requirements-web.txt"
chown -R loralarm:loralarm /opt/loralarm/.venv

# --- config files (empty placeholders; fill by hand, they hold secrets) ----
if [ ! -f "$CONF/devices.json" ]; then
    install -o loralarm -g loralarm -m 640 /dev/null "$CONF/devices.json"
    echo "NOTE: $CONF/devices.json is empty - copy the real one in (README step 2)"
fi
if [ ! -f "$CONF/mqtt.pass" ]; then
    install -o loralarm -g loralarm -m 600 /dev/null "$CONF/mqtt.pass"
    echo "NOTE: $CONF/mqtt.pass is empty - put the broker password in it"
fi
if [ ! -f "$CONF/loralarm.env" ]; then
    install -o root -g loralarm -m 640 "$HERE/loralarm.env.example" "$CONF/loralarm.env"
    echo "NOTE: $CONF/loralarm.env has placeholders - fill in the real values"
fi

# --- systemd unit ----------------------------------------------------------
install -m 644 "$HERE/loralarm.service" /etc/systemd/system/loralarm.service
systemctl daemon-reload

echo
echo "Next: edit $CONF/devices.json, mqtt.pass and loralarm.env, then:"
echo "  systemctl enable --now loralarm"
echo "  journalctl -u loralarm -f"
