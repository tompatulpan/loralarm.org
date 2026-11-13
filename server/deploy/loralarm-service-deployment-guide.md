# Deploying the loralarm service in production

This guide installs the loralarm Python service (`server/python/`) as a systemd-managed daemon on a dedicated host, consuming the ChirpStack MQTT feed and delivering ntfy/Pushover notifications. It assumes the broker (Mosquitto) and ChirpStack already run elsewhere, e.g. on the `lora` box.

The same service can instead be installed on a Debian LXC through the scripts next to this guide (`README.md`, `install.sh`, `deploy.sh`): that flow syncs code by rsync from the dev machine and keeps config in `/etc/loralarm/`, DB in `/var/lib/loralarm`. This document is the dedicated-host flow: a git checkout under `/opt/loralarm` and a hand-built unit.

## Overview

| Piece | Location | Owner / mode |
|---|---|---|
| Code checkout | `/opt/loralarm/loralarm.org` | `loralarm:loralarm` |
| Virtualenv | `/opt/loralarm/loralarm.org/server/python/.venv` | `loralarm:loralarm` |
| Device config | `/opt/loralarm/loralarm.org/server/python/devices.json` | `tomas:loralarm` 640 (writable master kept in the admin user's home) |
| Secrets | `/etc/loralarm.env` | `root:loralarm` 640 |
| systemd unit | `/etc/systemd/system/loralarm.service` | `root:root` 644 |
| Event database | `loralarm.db` in the service working directory | created by the service |

Design rules this follows:

- The service runs as a **dedicated unprivileged user** (`loralarm`), not root, not your admin user.
- Secrets live in **one env file** referenced by `${ENV_VAR}` placeholders in `devices.json`, never in the repo or the unit file.
- Exactly **one instance per broker** (see Troubleshooting).

## 1. Create the service user

```bash
sudo adduser --system --group --home /opt/loralarm --shell /usr/sbin/nologin loralarm
```

## 2. Clone the code as the service user

The whole `/opt/loralarm` tree is owned by `loralarm`, so do the clone as that user (your admin account cannot write there, and that is intentional):

```bash
sudo mkdir -p /opt/loralarm
cd /opt/loralarm
sudo -u loralarm git clone https://github.com/tompatulpan/loralarm.org.git
```

## 3. Build the venv and run the tests

```bash
cd /opt/loralarm/loralarm.org/server/python
sudo -u loralarm python3 -m venv .venv
sudo -u loralarm .venv/bin/pip install -r requirements.txt
sudo -u loralarm .venv/bin/python3 -m unittest discover -s tests -t .
```

All tests passing is the smoke test for the platform. (A number of skips is normal.)

## 4. Create the MQTT user on the broker

On the broker host, create a dedicated MQTT account for the service, e.g. with Mosquitto's password file and ACL (pattern-based read on `application/#` and `loralarm/down/+`, write on the downlink command topics as needed). Do not reuse a personal account: the Mosquitto log then tells you which component every connection belongs to.

## 5. Device configuration

Deploy `devices.json` from the tracked template, keeping a writable master outside the read-only checkout:

```bash
# on your admin account
cp /opt/loralarm/loralarm.org/server/python/devices.example.json ~/devices.json
nano ~/devices.json          # edit devices, topics, limits
python3 -m json.tool ~/devices.json > /dev/null && echo valid
sudo install -o <admin> -g loralarm -m 640 ~/devices.json \
    /opt/loralarm/loralarm.org/server/python/devices.json
```

Notes:

- Devices are keyed by devEUI under `"devices"`; layering is `defaults` < `families[profile]` < `devices[devEUI]`. A device absent from the config still works: it falls back to defaults (default ntfy topic, pushover off/on per defaults) and logs a warning reminding you to add it.
- Use `${LORALARM_PUSHOVER_USER}` style placeholders for secrets; list what the file needs with:

```bash
grep -o '\${[A-Z_]*}' ~/devices.json | sort -u
```

Every future config change is: edit `~/devices.json`, validate, `install`, then `sudo systemctl restart loralarm`.

## 6. Secrets file

```bash
sudo bash -c 'cat > /etc/loralarm.env <<EOF
LORALARM_MQTT_PASSWORD=<service mqtt password>
LORALARM_PUSHOVER_USER=<pushover user key>
LORALARM_PUSHOVER_TOKEN=<pushover app token>
EOF'
sudo chown root:loralarm /etc/loralarm.env
sudo chmod 640 /etc/loralarm.env
```

Only include the variables your `devices.json` actually references, plus the MQTT password. Never put secrets in the unit file or the repo.

## 7. Manual smoke test (before systemd)

Run once interactively to validate auth, config and notifications before handing over to systemd:

```bash
cd /opt/loralarm/loralarm.org/server/python
sudo -u loralarm bash -c 'set -a; . /etc/loralarm.env; set +a; \
  .venv/bin/python3 -m loralarm --mqtt <broker-ip> --user loralarm \
  --config devices.json --ntfy-url http://<broker-ip>:8075'
```

You should see `subscribed to application/+/device/+/event/up` and then quiet. Trigger a device, confirm the notification arrives, then Ctrl+C.

## 8. systemd unit

Create `/etc/systemd/system/loralarm.service` (a heredoc avoids paste-mangling of the `[Section]` headers):

```bash
sudo bash -c 'cat > /etc/systemd/system/loralarm.service <<EOF
[Unit]
Description=loralarm service (LoRaWAN alarms from ChirpStack MQTT)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=loralarm
Group=loralarm
WorkingDirectory=/opt/loralarm/loralarm.org/server/python
EnvironmentFile=/etc/loralarm.env
ExecStart=/opt/loralarm/loralarm.org/server/python/.venv/bin/python3 -m loralarm --mqtt <broker-ip> --user loralarm --config devices.json --ntfy-url http://<broker-ip>:8075
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF'
```

Verify the first line is exactly `[Unit]` (`cat` the file), then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now loralarm.service
systemctl status loralarm --no-pager
sudo journalctl -u loralarm -n 20 --no-pager
```

From now on the service has no terminal; its output lives in the journal:

```bash
sudo journalctl -u loralarm -f      # live tail
sudo journalctl -u loralarm -n 50 --no-pager
```

## 9. Acceptance checks

1. `systemctl is-active loralarm` prints `active`.
2. Journal shows `service running` and `subscribed to ...` once, then no repeating warnings.
3. A real device event produces the expected notification on the phone (ntfy and/or Pushover).
4. `sudo systemctl restart loralarm` comes back active within seconds.
5. Reboot test (optional but recommended): the service comes up on its own and reconnects.

## Troubleshooting

### Subscribe/disconnect loop every ~1 s

Signature in the journal:

```
subscribed to application/+/device/+/event/up ...
disconnected from <broker>: Unspecified error
```

repeating every second. The service uses a **fixed MQTT client ID** (`loralarm-service`) to get a persistent session (QoS 1, offline queueing). Two clients with the same client ID on one broker kick each other off in exactly this loop, regardless of which MQTT user they authenticate with. Typical cause: a dev instance still running somewhere against the same broker.

Diagnose on the broker:

```bash
sudo grep -E "already connected|New client" /var/log/mosquitto/mosquitto.log | tail -10
```

The source IPs in "New client connected from <ip>" name the duplicates. Fix: stop the other instance, or point it at a different broker / give it a different client ID. Only the production service may use the fixed ID.

### Connection refused with mosquitto_sub -h localhost

The broker is on another machine. Use `-h <broker-ip>`, not `localhost`, from the service host.

### Permission denied editing devices.json

The checkout directory is owned by `loralarm` and intentionally read-only for your admin user. Keep the writable master in your home directory and use the `sudo install` step (see §5). Never `chown` the checkout to your admin user.

### "device X is not in the config" warnings

Informational: the device alarms to the default ntfy topic with default settings. Add it under `"devices"` (key = devEUI) to give it its own topic/limits, then restart.

### Empty devices.json after a failed copy

Always verify before installing: `wc -c` and `python3 -m json.tool` on the source, and check the installed file's size. A zero-byte install makes the service fail at startup.

## Security notes

- Service user `loralarm` is a system account with no login shell.
- `/etc/loralarm.env` is `root:loralarm` 640: only root and the service user can read secrets. Do the same for any file with baked-in secrets.
- MQTT passwords given on the command line (`--pass`) are visible in `ps` and shell history; prefer `--pass-file`, the env file, or at minimum clean the history afterwards.