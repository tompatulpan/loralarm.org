# Deploying the loralarm service in production

This guide installs the loralarm Python service (`server/python/`) as a systemd-managed daemon on a dedicated host, consuming the ChirpStack MQTT feed and delivering ntfy/Pushover notifications, plus the FastAPI/PWA web layer behind a Cloudflare tunnel. It assumes the broker (Mosquitto) and ChirpStack already run elsewhere, e.g. on the `lora` box.

The same service can instead be installed on a Debian LXC through the scripts next to this guide (`README.md`, `install.sh`, `deploy.sh`): that flow syncs code by rsync from the dev machine and keeps config in `/etc/loralarm/`. This document is the dedicated-host flow: a git checkout under `/opt/loralarm` and a hand-built unit.

## Overview

| Piece | Location | Owner / mode |
|---|---|---|
| Code checkout | `/opt/loralarm/loralarm.org` | `loralarm:loralarm` |
| Virtualenv | `/opt/loralarm/loralarm.org/server/python/.venv` | `loralarm:loralarm` |
| Device config | `/opt/loralarm/loralarm.org/server/python/devices.json` | `tomas:loralarm` 640 (writable master kept in the admin user's home) |
| Secrets (env) | `/etc/loralarm.env` | `root:loralarm` 640 |
| MQTT password | `/etc/loralarm/mqtt.pass` | `loralarm:loralarm` 600 |
| Event database | `/var/lib/loralarm/loralarm.db` | `loralarm:loralarm` (created by the service, outside the checkout) |
| systemd unit | `/etc/systemd/system/loralarm.service` | `root:root` 644 |
| Cloudflare tunnel | cloudflared on another host, ingress → `http://<service-lan-ip>:8080` | |

Design rules this follows:

- The service runs as a **dedicated unprivileged user** (`loralarm`), not root, not your admin user.
- Secrets live in **one env file** referenced by `${ENV_VAR}` placeholders in `devices.json`, plus the broker password in its own 600 file; never in the repo or the unit file.
- Exactly **one instance per broker** (see Troubleshooting).
- The DB lives under `/var/lib/loralarm/` so a `git clean` or code sync can never touch it.
- The web app binds a **specific LAN address** (not `0.0.0.0`, not `127.0.0.1` when the tunnel runs elsewhere); the public path is HTTPS via Cloudflare only.

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
sudo -u loralarm .venv/bin/pip install -r requirements.txt -r requirements-web.txt
sudo -u loralarm .venv/bin/python3 -m unittest discover -s tests -t .
```

`requirements-web.txt` (fastapi/uvicorn/pywebpush) is needed only for the web layer; install it from the start so later `--web` flag changes need no extra step. All tests passing is the smoke test for the platform. (A number of skips is normal.)

## 4. Create the MQTT user on the broker

On the broker host, create a dedicated MQTT account for the service, e.g. with Mosquitto's password file and ACL (pattern-based read on `application/#` and `loralarm/down/+`, write on the downlink command topics as needed). Do not reuse a personal account: the Mosquitto log then tells you which component every connection belongs to.

Put the password on the service host in a dedicated file:

```bash
sudo install -o loralarm -g loralarm -m 600 /dev/null /etc/loralarm/mqtt.pass
sudo nano /etc/loralarm/mqtt.pass    # single line: the service mqtt password
```

Prefer `--pass-file` over `--pass` (visible in `ps`/history) or an env variable.

## 5. Device configuration

Deploy `devices.json` from the tracked template, keeping a writable master outside the read-only checkout:

```bash
# on your admin account
cp /opt/loralarm/loralarm.org/server/python/devices.example.json ~/devices.json
nano ~/devices.json          # edit devices, topics, limits, web_users
python3 -m json.tool ~/devices.json > /dev/null && echo valid
sudo install -o <admin> -g loralarm -m 640 ~/devices.json \
    /opt/loralarm/loralarm.org/server/python/devices.json
```

Notes:

- Devices are keyed by devEUI under `"devices"`; layering is `defaults` < `families[profile]` < `devices[devEUI]`. A device absent from the config still works: it falls back to defaults (default ntfy topic, pushover off/on per defaults) and logs a warning reminding you to add it.
- `web_users` entries invite-token login to the PWA; use `${LORALARM_TOKEN_<NAME>}` placeholders for the tokens.
- Use `${LORALARM_PUSHOVER_USER}` style placeholders for secrets; list what the file needs with:

```bash
grep -o '\${[A-Z_]*}' ~/devices.json | sort -u
```

- **Config changes hot-reload**: the service checks `devices.json`'s mtime on every uplink/watchdog tick and reloads it (including seeding/disabling `web_users`) without a restart. The routine for every future config change is just: edit `~/devices.json`, validate, `install`. A restart is needed only for env-file values, unit flags, or dependency changes.

## 6. Secrets file

```bash
sudo bash -c 'cat > /etc/loralarm.env <<EOF
LORALARM_PUSHOVER_USER=<pushover user key>
LORALARM_PUSHOVER_TOKEN=<pushover app token>
LORALARM_TOKEN_<NAME>=<web invite token>
LORALARM_VAPID_PRIVATE_KEY=<vapid private key>
LORALARM_VAPID_SUB=<push subject, e.g. mailto:>
EOF'
sudo chown root:loralarm /etc/loralarm.env
sudo chmod 640 /etc/loralarm.env
```

Only include the variables your `devices.json` actually references, plus the VAPID pair if Web Push is enabled. Never put secrets in the unit file or the repo. The VAPID keys are for the Web Push channel; generate once and keep stable (subscriptions break when the key changes).

## 7. Manual smoke test (before systemd)

Run once interactively to validate auth, config and notifications before handing over to systemd:

```bash
cd /opt/loralarm/loralarm.org/server/python
sudo -u loralarm bash -c 'set -a; . /etc/loralarm.env; set +a; \
  .venv/bin/python3 -m loralarm --mqtt <broker-ip> --user loralarm \
  --pass-file /etc/loralarm/mqtt.pass \
  --config devices.json --ntfy-url http://<broker-ip>:8075'
```

You should see `subscribed to application/+/device/+/event/up` and then quiet. Trigger a device, confirm the notification arrives, then Ctrl+C. Do not leave this running when the systemd service starts (same client ID, see Troubleshooting).

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
ExecStart=/opt/loralarm/loralarm.org/server/python/.venv/bin/python3 -m loralarm --mqtt <broker-ip> --user loralarm --pass-file /etc/loralarm/mqtt.pass --config devices.json --db /var/lib/loralarm/loralarm.db --ntfy-url http://<broker-ip>:8075 --web --web-host <service-lan-ip> --static ../../PWA/web
Restart=always
RestartSec=5

# hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/loralarm
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
EOF'
```

Flag notes:

- `--db /var/lib/loralarm/loralarm.db` is mandatory: the default is `./loralarm.db` in the code directory, which a code sync or `git clean` could delete. Create the directory first: `sudo mkdir -p /var/lib/loralarm && sudo chown loralarm:loralarm /var/lib/loralarm`.
- `--web-host <service-lan-ip>`: bind the specific LAN address when the Cloudflare tunnel (cloudflared) runs on another host and forwards to `http://<service-lan-ip>:8080`. Never bind `0.0.0.0`. If cloudflared ran on the same host, `127.0.0.1` (the default) would be correct instead.
- `--static ../../PWA/web` resolves relative to `WorkingDirectory`, i.e. the PWA frontend in the checkout.
- Do **not** use `--web-insecure-cookie`: the public path is HTTPS via Cloudflare, so the session cookie must stay Secure. That flag is only for plain-HTTP LAN serving.
- `ProtectSystem=strict` still permits the config hot-reload (reading `devices.json` needs no write exemption); only the DB path must be in `ReadWritePaths`.

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

## 9. Cloudflare tunnel

On the host already running cloudflared for your other hostnames, point only the alarm hostname at the service (leave the other ingress rules untouched):

```yaml
ingress:
  - hostname: alarms.loralarm.org
    service: http://<service-lan-ip>:8080
```

then `sudo systemctl restart cloudflared` there. Public reachability is HTTPS via Cloudflare only; the origin hop over the LAN is plain HTTP, which the web app's own token/session auth covers.

## 10. Acceptance checks

1. `systemctl is-active loralarm` prints `active`.
2. Journal shows `service running` and `subscribed to ...` once, then no repeating warnings.
3. A real device event produces the expected notification on the phone (ntfy and/or Pushover).
4. Web layer: `ss -tlnp | grep 8080` shows `<service-lan-ip>:8080`, and `curl -s http://<service-lan-ip>:8080/ | head -5` returns the PWA HTML (install `curl` if the minimal host lacks it).
5. `https://alarms.loralarm.org` loads the PWA login on the phone; the invite token logs in and the fleet view lists the real sensors.
6. `sudo systemctl restart loralarm` comes back active within seconds, and the events DB survives:

```bash
sqlite3 /var/lib/loralarm/loralarm.db 'select kind, count(*) from events group by kind;'
```

7. Reboot test (optional but recommended): the service comes up on its own and reconnects.

## Updating code later

The checkout is a normal git clone owned by `loralarm`:

```bash
cd /opt/loralarm/loralarm.org
sudo -u loralarm git fetch
sudo -u loralarm git log --oneline HEAD..origin/main    # review what's incoming
sudo -u loralarm git pull

cd server/python
sudo -u loralarm .venv/bin/pip install -r requirements.txt -r requirements-web.txt   # no-op if unchanged

sudo systemctl restart loralarm    # code does not hot-reload; devices.json does
sudo journalctl -u loralarm -n 20 --no-pager
```

`devices.json` is untracked, so a pull never touches it; config and DB live outside the checkout precisely so a code update cannot affect either.

## Troubleshooting

### Subscribe/disconnect loop every ~1 s

Signature in the journal:

```
subscribed to application/+/device/+/event/up ...
disconnected from <broker>: Unspecified error
```

repeating every second. The service uses a **fixed MQTT client ID** (`loralarm-service`) to get a persistent session (QoS 1, offline queueing). Two clients with the same client ID on one broker kick each other off in exactly this loop, **regardless of which MQTT user or machine they connect from** — the broker's uniqueness check is on the client ID alone. Typical cause: a dev instance still running somewhere against the same broker.

Diagnose on the broker:

```bash
sudo grep -E "already connected|New client" /var/log/mosquitto/mosquitto.log | tail -10
```

The source IPs in "New client connected from <ip>" name the duplicates. Fix: stop the other instance, or give it a different client ID (`--client-id loralarm-dev`) or a different broker. Only the production service may use the fixed ID. For passive watching from a dev machine, `mosquitto_sub` uses a random client ID and cannot collide.

### 502 from the tunnel hostname

The tunnel is up but nothing answers at its origin, in rough order:

1. `ss -tlnp | grep 8080` on the service host — empty means the web app is not running (unit missing `--web`, or web deps not installed).
2. If the listener shows `127.0.0.1:8080` but cloudflared runs on another host: the app refuses remote connections by default. Add `--web-host <service-lan-ip>` to the unit and restart.
3. If the listener is correct, check the cloudflared host's ingress rule actually points at `http://<service-lan-ip>:8080`, then restart cloudflared there. A stale 502 cached at the edge clears on a hard reload.

Note: minimal hosts often lack `curl`; `sudo apt install -y curl sqlite3 mosquitto-clients`, or test with
`python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/', timeout=3).status)"`.

### å ä ö display as blanks or garbage in nano

The session (or root via `sudo`) runs with `LANG=C` — a non-UTF-8 locale cannot render them. Fix on the service host:

```bash
sudo locale-gen sv_SE.UTF-8
sudo update-locale LANG=sv_SE.UTF-8
```

then **log out and back in** (locale is fixed per login; a reboot also does it). If `cat file | grep <text>` shows the characters correctly but `nano` still does not: you are in `sudo nano`, which resets the environment — edit your own files without sudo, or use `sudoedit` for root-owned files. Client side: PuTTY needs Window → Translation → Remote character set = UTF-8.

Related: `\u00e4`-style sequences in `devices.json` are valid JSON escapes (ä), not corruption — `json.tool` re-escapes non-ASCII by default; view decoded with `python3 -m json.tool --no-ensure-ascii`. To rewrite the file with real characters everywhere:

```bash
python3 -c "import json,io; d=json.load(open('/home/<admin>/devices.json',encoding='utf-8')); io.open('/home/<admin>/devices.json','w',encoding='utf-8').write(json.dumps(d,ensure_ascii=False,indent=2))"
```

### Connection refused with mosquitto_sub -h localhost

The broker is on another machine. Use `-h <broker-ip>`, not `localhost`, from the service host.

### Permission denied editing devices.json

The checkout directory is owned by `loralarm` and intentionally read-only for your admin user. Keep the writable master in your home directory and use the `sudo install` step (see §5). Never `chown` the checkout to your admin user.

### "device X is not in the config" warnings

Informational: the device alarms to the default ntfy topic with default settings. Add it under `"devices"` (key = devEUI) to give it its own topic/limits; the config hot-reload picks it up without a restart.

### Empty devices.json after a failed copy

Always verify before installing: `wc -c` and `python3 -m json.tool` on the source, and check the installed file's size. A zero-byte install makes the service fail at startup.

## Security notes

- Service user `loralarm` is a system account with no login shell.
- `/etc/loralarm.env` is `root:loralarm` 640; `/etc/loralarm/mqtt.pass` is `loralarm:loralarm` 600. Do the same for any file with baked-in secrets.
- MQTT passwords given on the command line (`--pass`) are visible in `ps` and shell history; prefer `--pass-file`.
- The unit runs with `ProtectSystem=strict` and friends; only `/var/lib/loralarm` is writable. Widen deliberately, not by habit.
- The web app binds a specific LAN address; it is reachable unencrypted only from the LAN. The public path is HTTPS via Cloudflare.
