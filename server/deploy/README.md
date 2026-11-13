# server/deploy/ — loralarm on a Debian host

Runs the loralarm service natively: a systemd unit, a virtualenv, SQLite in a
file. No Docker inside the container.

For a dedicated host installed by hand (git checkout under /opt/loralarm, manual unit) see loralarm-service-deployment-guide.md in this directory — same service, different layout, including the web/PWA layer, Cloudflare tunnel exposure and the git-based update workflow.

| File | Role |
|---|---|
| `loralarm.service` | systemd unit: runs as system user `loralarm`, DB under `/var/lib/loralarm`, web on `0.0.0.0:8080` |
| `loralarm.env.example` | template for `/etc/loralarm/loralarm.env` (all `${ENV}` placeholders of `devices.json`) |
| `install.sh` | run once **inside** the LXC as root: packages, venv, user, config placeholders, unit |
| `deploy.sh` | run on the dev machine for later updates: rsync code + frontend, restart |

Layout on the container:

```
/opt/loralarm/python/   loralarm package, families/, examples/ (from the dev PC)
/opt/loralarm/web/      PWA frontend (from ../../PWA/web)
/opt/loralarm/.venv     virtualenv (requirements.txt + requirements-web.txt)
/etc/loralarm/          devices.json, mqtt.pass (600), loralarm.env (640)
/var/lib/loralarm/      loralarm.db
```

## Install

1. **Sync the code** from the dev machine (repo root):

   ```bash
   rsync -a --delete --exclude .venv --exclude __pycache__ \
       --exclude loralarm.db --exclude tests --exclude devices.json \
       server/python/ <root@lxc>:/opt/loralarm/python/
   rsync -a --delete PWA/web/ <root@lxc>:/opt/loralarm/web/
   ```

2. **Copy the config**:

   ```bash
   ssh <root@lxc> 'mkdir -p /etc/loralarm'
   scp server/python/devices.json       <root@lxc>:/etc/loralarm/devices.json
   scp server/deploy/loralarm.env.example <root@lxc>:/etc/loralarm/loralarm.env
   ```

   On the container, fill in `/etc/loralarm/loralarm.env` — every `${...}`
   name in `devices.json` must exist as a variable there — and put the broker
   password in `/etc/loralarm/mqtt.pass` (`chmod 600`). No secrets on the
   command line or in the unit.

3. **Run the installer** (scans `/opt/loralarm`, installs packages
   including `tzdata`, creates the `loralarm` system user, builds the venv,
   registers the unit):

   ```bash
   scp server/deploy/* <root@lxc>:/root/loralarm-deploy/
   ssh <root@lxc> sh /root/loralarm-deploy/install.sh
   ```

4. **Start and watch**:

   ```bash
   ssh <root@lxc> systemctl enable --now loralarm
   ssh <root@lxc> journalctl -u loralarm -f
   ```

   Expected: MQTT connect to 192.168.2.26, config load, one console line per
   live uplink.

5. **Verify** from the container:

   ```bash
   # broker credentials work from here (also proves the firewall path):
   mosquitto_sub -h 192.168.2.26 -u "$LORALARM_MQTT_USER" -P "$(cat /etc/loralarm/mqtt.pass)" \
       -t 'application/+/device/+/event/up' -C 1 -W 60
   # web app answers:
   curl -s http://127.0.0.1:8080/ | head
   # alarms arriving:
   sqlite3 /var/lib/loralarm/loralarm.db 'select kind, count(*) from events group by kind;'
   ```

6. **Use it**: open `http://<lxc-ip>:8080` and log in with a `web_users`
   token. Over plain LAN HTTP the status page and fleet list work; push
   notifications and install-to-home-screen require HTTPS
   (`PWA/docs/pwa-design.md`, §9).

## Updating code later

```bash
sh server/deploy/deploy.sh <root@lxc>   # from the repo root on the dev machine
```

## Backup

`vzdump` of the container covers code and config in one image; the events DB
warrants its own schedule (sqlite file copy while the service can be briefly
paused, or the `.backup` shell command):

```bash
sqlite3 /var/lib/loralarm/loralarm.db ".backup '/backup/loralarm.db'"
```

## Operational notes

- A fresh install starts with an empty events DB: alarm states, `last_values`
  and tilt baselines are re-learned from the next uplinks, `device_armed`
  starts armed for everyone.
- Code syncs use `--delete`; the DB lives under `/var/lib/loralarm` and
  config under `/etc/loralarm` precisely so a re-sync cannot touch either.
- The unit hardens the service (`ProtectSystem=strict`); writable paths are
  listed in `loralarm.service` — widen deliberately there.
- The service holds a persistent MQTT session (`loralarm-service` client id):
  with the broker's QoS and ChirpStack's integration QoS ≥ 1, uplinks queue
  while the container is down. Run exactly one instance per client id.
