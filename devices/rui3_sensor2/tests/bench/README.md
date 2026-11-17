# Bench automation for the connection-supervision tests

Runs the stages of [`../../BENCH_TEST_PLAN.md`](../../BENCH_TEST_PLAN.md) against
the real node: waits for the firmware's exact log lines on the serial port,
drives the gateway, reboots the node, verifies frames server-side and writes
`report.md` + `timeline.json` per stage. The checklist file stays the manual
reference; this is the runner.

## Files

| File | Purpose |
|---|---|
| `bench.py` | The harness: `monitor`, `flash`, `probe`, `sendint`, `gw`, `server`, `stage-a`, `stage-b1`, `stage-b2`, `stage-c` |
| `bench_config.example.json` | Copy to `bench_config.json` and edit (port, DevEUI, gateway, mqtt/chirpstack) |
| `selfcheck.py` | Offline validation: pattern↔firmware strings, flash substitution, config mode |
| `dryrun_stage_a.py` | Offline dry-run of full stage-a through the real Runner logic, scripted serial log |

## Setup

```bash
cp bench_config.example.json bench_config.json       # then edit
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # pyserial + paho-mqtt
.venv/bin/python3 selfcheck.py && .venv/bin/python3 dryrun_stage_a.py   # verify the harness itself
.venv/bin/python3 bench.py server --seconds 320       # live probe check: wait for one heartbeat
```

In this setup, run everything through `.venv/bin/python3` (system pip is
PEP-668 blocked). `server --seconds <t>` connects the MQTT probe and waits for
one uplink event (SENDINT=300 → allow ~320 s); it prints the fCnt, proving the
broker credentials and ACL work before a stage run.

What stays out of git (`devices/rui3_sensor2/.gitignore`, updated for the
python harness): `bench_config.json` (real ports, DevEUI, broker credentials),
`tests/bench/logs/` (run reports), `.venv/` and `__pycache__/`.

What each config knob does (all optional ones degrade gracefully):

- `serial_port`, `baud`, `dev_eui` — the bench node over USB.
- `gateway` — how the node's network path is taken down/up:
  - `type: "tasmota"` (recommended for stage-c): a Tasmota mains switch on the
    LAN powering the gateway — `ip` (+ `password` if set), switched via its HTTP
    API. `on_wait_s` (default 90 s) covers the gateway boot after ON so recovery
    times compare fairly with a service-stop method; `off_wait_s` default 5 s.
    The switch must power ONLY the gateway — ChirpStack must stay reachable for
    the arrival checks.
  - `type: "shell"`: `off_cmd`/`on_cmd` shell strings, e.g. stop the packet
    forwarder on the gateway host over ssh, or `chirpstack-gateway-bridge` on
    the ChirpStack host (join-accepts/ACKs then never reach the node).
  - `type: "manual"` (or nothing configured): interactive prompt — fine for
    stages A/B; stage-c refuses to run unattended.
- `chirpstack.base_url/email/password` — ChirpStack **v4** REST API only; gives
  frame arrival (`lastSeenAt`) and FCntUp (B1.5 continuity, C4 new-session reset).
  This bench runs against ChirpStack v3, whose REST API is gone — use `mqtt`.
- `mqtt.host/port/user/password` — preferred probe: the ChirpStack MQTT
  integration (the broker the service publishes to, e.g. Mosquitto on the
  ChirpStack host). Subscribe-only on `application/+/device/<eui>/event/up`;
  every uplink is seen the moment it arrives, with its exact fCnt, so it covers
  arrival checks AND B1.5 continuity on v3 and v4 alike. Needs
  `pip install paho-mqtt` (the bench venv `tests/bench/.venv` has it).
  No probe at all → serial-only pass/fail, server checks WARN. Probe order:
  mqtt > chirpstack > db.
- `loralarm_db` — path to loralarm.db; fallback arrival check when run on the
  server host.
- `arduino.fqbn` — needed for `flash`; taken from `../../sketch.yaml`.
- `factor` — scales the SHORT stage-a/b timeouts only (harness debugging,
  never for results). stage-c always prompts/fails unless it is 1.0.

## Running the plan

```bash
python3 bench.py flash --mode test        # A1: config.h switched, built, flashed, file restored
python3 bench.py stage-a                 # A2..A9 (~30 min, watched automatically)
python3 bench.py flash --mode production  # A10
python3 bench.py stage-b1                 # power-cycle in coverage (AT+BOOT or manual)
python3 bench.py stage-b2                 # power-cycle out of coverage (outcome U or C recorded)
python3 bench.py stage-c                  # 25 h soak, unattended; gateway cmds required
python3 bench.py monitor                  # plain timestamped logging (plan step P2)
```

Output per run: `logs/<timestamp>/raw.log` (timestamped, `####` markers at
harness actions), `report.md` (step-by-step PASS/FAIL table + recorded values),
`timeline.json` (machine-readable). Stage-a records `status_n`; the two
stop-and-report cases (AS1: link check reports OK with the gateway off,
AS2: `join() returned false` without join attempts) abort the stage as FAIL
with the reason.

`flash` never leaves the working tree dirty: it copies `config.h`, flips the
three defines, verifies by reading back, uploads via arduino-cli, and restores
the copy in `finally` — your local edits (e.g. current bench-settings) survive.
`selfcheck.py` prints which mode the working-tree `config.h` currently holds.

## Not automated

- P1 (bench power above the critical threshold), P3/P4 (host tests, compile),
  P5 (ChirpStack profile/codec), P6 (`AT+SENDINT=300` — the harness checks the
  boot log value and fails the run early if it is 0 but has no AT setter).
- C-opt (the 48 h run) — re-run stage-c ideas; ask if wanted.
- Verifying an actual ntfy/PWA notification — server-side only checks arrival.
