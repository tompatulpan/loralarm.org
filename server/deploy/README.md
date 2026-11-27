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

## Web Push (notifications in the PWA)

Push is code-complete and stays off until the instance has a VAPID keypair in
its env file. Two prerequisites live outside the repo: **HTTPS** (the
Cloudflare tunnel — browsers allow service workers and push only on secure
origins, LAN addresses included) and the app **installed on the phone**
(iOS 16.4+: Share → "Lägg till på hemskärmen"; without the install iOS receives
no web push at all).

1. **Generate the keypair once**, on the host, with the service venv:

   ```bash
   ssh <root@lxc> 'cd /opt/loralarm/python && \
       sudo -u loralarm /opt/loralarm/.venv/bin/python3 -m loralarm.vapid'
   ```

   Paste the three `LORALARM_VAPID_*` lines into `/etc/loralarm/loralarm.env`,
   replacing the commented placeholders. **On a live instance keep the key it
   already has**: a new pair invalidates the registration of every installed
   phone (the app re-subscribes only when someone re-enables push).
   `LORALARM_VAPID_PUBLIC_KEY` may be omitted — `/api/push/key` derives it from
   the private key, and the PWA subscribes with exactly that.

2. **Verify what the service will read** (exit 0 = usable, problems on stdout):

   ```bash
   ssh <root@lxc> 'cd /opt/loralarm/python && sudo -u loralarm \
       /opt/loralarm/.venv/bin/python3 -m loralarm.vapid --check \
       --env-file /etc/loralarm/loralarm.env'
   ```

   It reports a key that does not belong to its own public key, a missing
   private key, and a `LORALARM_VAPID_SUB` a push service would reject.

3. **Restart** — env files are read at startup, unlike `devices.json`:

   ```bash
   ssh <root@lxc> systemctl restart loralarm
   ssh <root@lxc> journalctl -u loralarm -n 20 --no-pager
   ```

   The journal must not say `Web Push has no VAPID private key`.

4. **Subscribe on the phone**: open the HTTPS hostname, log in, *Notiser* →
   "Aktivera push på denna enhet" (allow the permission prompt), then
   "Skicka test-push". The notification should arrive with the app in the
   background or closed.

5. **Confirm delivery server-side**:

   ```bash
   sqlite3 /var/lib/loralarm/loralarm.db \
     'select ts, status, http_code from webpush_log order by id desc limit 5;'
   ```

   `sent` = a push service accepted it; `pruned` (404/410) = that browser
   endpoint was gone and has been deleted (re-enable push on that phone);
   `failed` with 401/403 = the subscription was made with a different (rotated)
   VAPID key. `push_ack` holds the service-worker receipt, written once the
   notification is displayed.

From then on every alarm transition reaches the subscribers of the device: the
fire notification is `high` urgency with `tag = dev_eui:kind`, so the clear
replaces it silently. Two log lines mean a device reaches nobody: `has no
followers; alarm not pushed` (mute toggle under *Push per enhet*) and a test
push reporting no endpoint (nothing subscribed yet on this browser).

The settings page is the quickest diagnosis: *Push är inte påslagen på servern*
means the service runs without a usable key (steps 2–3), *Push ej aktiverad*
means this browser has no subscription yet (step 4), and both green plus a
failing test push point at the delivery log (step 5).

Two device-side situations produce the same silence and the same empty ack
list, so the log alone cannot separate them:

- **Focus / Do Not Disturb without the app allowed** suppresses the push on
  iOS: the notification never appears *and* the service worker never runs, so
  no `push_ack` arrives either. Check this first — it costs nothing. Settings →
  Focus → the active mode → Apps → allow LoRaLarm (the web-app name), then send
  a test push. An alarm that must wake someone still cannot rely on this: Web
  Push cannot request the Time Sensitive/Critical interruption levels
  (`PWA/docs/pwa-design.md`, §7), which is why Pushover stays for that.
- **A dead installation** (app deleted or its storage cleared) looks identical
  from the server: `webpush_log` keeps showing `sent 201` — Apple answers for a
  subscription long after the installation is gone — and no receipt ever comes
  back. Re-enable push on that device; it registers a new endpoint. The service
  reports endpoints with deliveries but no receipt for 30 days as
  `status='stale'` in `webpush_log` and in the journal.

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
