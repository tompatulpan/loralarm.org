#!/usr/bin/env python3
"""Bench automation for devices/rui3_sensor2 connection supervision.

Automates the stages of BENCH_TEST_PLAN.md: waits for the expected firmware
log lines on the serial port, drives the gateway (shell command or manual
prompt), reboots the node (AT+BOOT over serial or manual prompt), verifies
frames server-side (ChirpStack REST API; optional loralarm sqlite fallback)
and writes a results report per stage (report.md + timeline.json + raw log).

Usage (copy bench_config.example.json to bench_config.json and edit):

    python3 bench.py monitor                     # P2: timestamped logging only
    python3 bench.py flash --mode test           # A1: config.h test values + upload
    python3 bench.py stage-a                     # A2..A9   (~30 min)
    python3 bench.py flash --mode production     # A10: restore values + upload
    python3 bench.py stage-b1                    # power-cycle in coverage
    python3 bench.py stage-b2                    # power-cycle out of coverage
    python3 bench.py stage-c                     # 25 h soak (needs gw commands)

Without ChirpStack credentials the server-side checks become WARN and the
serial log alone decides pass/fail. Gateway steps use gateway.off_cmd/on_cmd
from the config; if absent you get an interactive prompt (fine for A/B;
stage-c needs the commands to run unattended overnight).

Requires: python3 + pyserial (`pip install pyserial`). flash needs arduino-cli.
Run stages A/B from a terminal (manual fallbacks prompt); stage-c is meant
for an unattended overnight run.

Exit code: 0 pass, 1 fail, 130 interrupted.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover
    sys.exit("pyserial missing: pip install pyserial")

HERE = Path(__file__).resolve().parent
SKETCH_DIR = HERE.parent.parent  # devices/rui3_sensor2
CONFIG_H = SKETCH_DIR / "config.h"

# --- config.h values for `flash` (must match the BENCH_TEST_PLAN A1/A10)
VALUES = {
    "test": {"REJOIN_CHECK_INTERVAL_MS": "60000UL", "REJOIN_RETRY_TICKS": "10", "LINK_CHECK_TICKS": "10"},
    "production": {"REJOIN_CHECK_INTERVAL_MS": "3600000UL", "REJOIN_RETRY_TICKS": "24", "LINK_CHECK_TICKS": "24"},
}

# ---------------------------------------------------------------- log patterns
# Serial strings exactly as the firmware prints them (lorawan.cpp / uplink.cpp).

BOOT_BANNER = r"LoraLarm\.org RAKwireless RUI3 Node"
BOOT_MODE = r"<BOOT MODE>"  # RUI3 DFU mode — AT+BOOT was sent by mistake; needs a power-cycle
JOINED = r"LoRaWan OTAA - joined!"
JOIN_FAIL = r"LoRaWan OTAA - join fail!"
JOIN_STARTED = r"Join started — retry every 255 s, up to 255 attempts"
ALREADY_JOINED_CACHED = r"Already joined \(cached session\) — skipping join setup"
JOIN_REFUSED = r"join\(\) returned false"
LINK_POLICY = r"Link policy: ADR=off, TXP=0 \(~16 dBm EIRP\), DR=\d+"
LINK_CHECK_DUE = r"Link check due: next uplink goes out confirmed"
LINK_CHECK_SEND = r"Sending packet over LoRaWAN .*confirmed link check"
LINK_CHECK_FAILED = r"Link check FAILED \(status (\d+)\)"
LINK_CHECK_OK = r"Link check OK: network answered"
FORCING_REJOIN = r"Forcing a fresh join \(up to 255 attempts\)"
STILL_NOT_JOINED = r"Still not joined after (\d+) min — starting a new join series"
TX_SUCCESSFUL = r"Transmission successful - ready for sleep"
NOT_JOINED_SKIP = r"Not joined, skip sending"
SEND_INTERVAL_FOUND = r"Send interval found (\d+)"
SEND_CONFIRMED_FAILED_EVT = r"SEND_CONFIRMED_FAILED"  # stack's own +EVT line


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------- serial logger


class SerialLog:
    """Reader thread: timestamps lines, writes the log, keeps a time-indexed buffer."""

    def __init__(self, port: str, baud: int, logfile: Path):
        self.ser = serial.Serial(port, baud, timeout=0.2)
        self.fh = logfile.open("a", encoding="utf-8", errors="replace")
        self.lines: list[tuple[datetime, str]] = []
        self.lock = threading.Lock()
        self.stop_flag = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        buf = b""
        while not self.stop_flag:
            chunk = self.ser.read(4096)
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").rstrip("\r")
                with self.lock:
                    self.lines.append((utcnow(), line))
                self.fh.write(f"{iso(self.lines[-1][0])} {line}\n")
                self.fh.flush()

    def write(self, data: str):
        self.ser.write(data.encode("utf-8"))

    def close(self):
        self.stop_flag = True
        self.thread.join(timeout=1.0)
        self.fh.close()
        self.ser.close()


# ---------------------------------------------------------------- server probes


class ChirpStack:
    """Minimal ChirpStack v4 internal REST client: arrival + FCntUp checks."""

    def __init__(self, cfg: dict):
        cfg = cfg or {}
        self.base = (cfg.get("base_url") or "").rstrip("/")
        self.email = cfg.get("email", "")
        self.password = cfg.get("password", "")
        self.jwt = None
        self.available = bool(self.base and self.email and self.password)
        print("  [server] ChirpStack API: " + ("on" if self.available else "not configured — DB/serial-only"))

    def _req(self, method: str, path: str, payload: dict | None = None):
        req = urllib.request.Request(self.base + path,
                                     data=json.dumps(payload).encode() if payload is not None else None,
                                     method=method)
        req.add_header("Content-Type", "application/json")
        if self.jwt:
            req.add_header("Grpc-Metadata-Authorization", f"Bearer {self.jwt}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read() or b"{}")

    def _get(self, path: str):
        if not self.jwt:
            r = self._req("POST", "/api/internal/login", {"email": self.email, "password": self.password})
            self.jwt = r["jwt"]
        try:
            return self._req("GET", path)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            self.jwt = None  # token expired: re-login and retry once
            self._get(path)
            raise

    def last_seen(self, eui: str) -> datetime | None:
        """Device lastSeenAt (UTC) per the network server; None = never / API error."""
        if not self.available:
            return None
        try:
            d = self._get(f"/api/devices/{eui.lower()}").get("device", {})
        except Exception:
            return None
        ls = d.get("lastSeenAt")
        return datetime.fromisoformat(ls).astimezone(timezone.utc) if ls else None

    def fcnt_up(self, eui: str) -> int | None:
        """Current-session uplink counter, None = unavailable / no session."""
        if not self.available:
            return None
        try:
            a = self._get(f"/api/devices/{eui.lower()}/activation")
        except Exception:
            return None
        for k in ("fCntUp", "fcntUp", "f_cnt_up"):
            if isinstance(a.get(k), int):
                return a[k]
        return None


class DbProbe:
    """Optional fallback: the loralarm sqlite DB (works when run on the server host)."""

    def __init__(self, path: str | None):
        self.path = path
        self.available = bool(path and Path(path).exists())
        if path and not self.available:
            print(f"  [server] DB not found: {path} (DB probe skipped)")

    def last_seen(self, eui: str) -> datetime | None:
        if not self.available:
            return None
        try:
            db = sqlite3.connect(self.path)
            row = db.execute("SELECT snapshot FROM device_state WHERE dev_eui=?", (eui.lower(),)).fetchone()
            db.close()
        except sqlite3.Error:
            return None
        if not row:
            return None
        ls = json.loads(row[0]).get("last_seen")
        if not ls:
            return None
        try:
            dt = datetime.fromisoformat(ls)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except ValueError:
            return None


class MqttWatch:
    """ChirpStack MQTT integration: every uplink event carries the exact fCnt.

    Preferred server probe: instant arrival detection AND frame-counter
    continuity (B1.5) from the live event stream — subscribe QoS 0, never publish.
    Event schema follows the loralarm service's parser (deviceInfo.devEui, fCnt,
    fPort, time). Read-only: no retained/incremental state, connect once,
    daemon thread. paho-mqtt is only imported when an mqtt host is configured.
    """

    def __init__(self, cfg: dict | None, eui: str):
        cfg = cfg or {}
        self.eui = eui.lower()
        self.events: list[dict] = []  # {"ts": arrival (utc), "fcnt": int, "fport": int}
        self.host = cfg.get("host")
        if not self.host:
            self.available = False
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            print("  [server] paho-mqtt not installed (pip install paho-mqtt) — MQTT probe off")
            self.available = False
            return
        try:
            try:  # paho >= 2.x
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                    client_id=f"bench-{self.eui[-4:]}", clean_session=True)
            except AttributeError:  # paho 1.x
                client = mqtt.Client(client_id=f"bench-{self.eui[-4:]}", clean_session=True)
            if cfg.get("user"):
                client.username_pw_set(cfg.get("user"), cfg.get("password"))

            def on_message(c, userdata, msg, *rest):
                self._on_message(msg)

            client.on_message = on_message
            client.connect(self.host, int(cfg.get("port", 1883)), keepalive=30)
            client.subscribe(f"application/+/device/{self.eui}/event/up", qos=0)
            client.loop_start()
            self._client = client
            self.available = True
            print(f"  [server] MQTT probe on {self.host}:{cfg.get('port', 1883)} "
                  f"({cfg.get('user') or 'anonymous'}), device ...{self.eui[-8:]}")
        except Exception as e:
            self.available = False
            print(f"  [server] MQTT connect failed: {e}")

    def _on_message(self, msg):
        try:
            ev = json.loads(msg.payload)
        except Exception:
            return
        if str((ev.get("deviceInfo") or {}).get("devEui", "")).lower() != self.eui:
            return
        try:
            self.events.append({"ts": utcnow(), "fcnt": int(ev.get("fCnt", -1)),
                                "fport": int(ev.get("fPort", -1))})
        except (TypeError, ValueError):
            pass

    def wait_event(self, timeout_s: float) -> dict | None:
        """Block until an uplink event lands or the timeout passes; returns the event or None."""
        deadline = utcnow() + timedelta(seconds=timeout_s)
        while utcnow() < deadline:
            if self.events:
                return self.events[-1]
            time.sleep(2)
        return None


class ServerWatch:
    """Answers 'do frames arrive?'. Primary: MQTT event stream (ChirpStack v3+),
    fallback: ChirpStack REST (v4 deployments), then the loralarm DB."""

    def __init__(self, eui: str, chirpstack: ChirpStack, db: DbProbe, mqtt: MqttWatch | None = None):
        self.eui = eui
        self.cs = chirpstack
        self.db = db
        self.mqtt = mqtt
        if mqtt and mqtt.available:
            self.src = "mqtt"
        elif self.cs.available:
            self.src = "chirpstack"
        elif self.db.available:
            self.src = "loralarm-db"
        else:
            self.src = "none"

    def last_seen(self) -> tuple[datetime | None, str]:
        if self.src == "mqtt":
            evs = self.mqtt.events
            return (evs[-1]["ts"] if evs else None), self.src
        return (self.cs.last_seen(self.eui) if self.src == "chirpstack" else self.db.last_seen(self.eui)), self.src

    def fcnt_up(self) -> int | None:
        """fCnt of the newest uplink the probe has seen this run (None if none yet)."""
        if self.src == "mqtt":
            return self.mqtt.events[-1]["fcnt"] if self.mqtt.events else None
        return self.cs.fcnt_up(self.eui)

    def watch_fresh_after(self, t0: datetime, timeout_s: float) -> bool | None:
        """True = frame after t0 was seen; False = deadline passed; None = no probe."""
        if self.src == "none":
            print("  [server] no probe configured — cannot verify arrival (manual check)")
            return None
        deadline = utcnow() + timedelta(seconds=timeout_s)
        while utcnow() < deadline:
            ls, _ = self.last_seen()
            if ls and ls > t0:
                print(f"  [server] fresh frame seen at {iso(ls)} ({self.src})")
                return True
            time.sleep(10)
        print(f"  [server] no frame after {iso(t0)} within {int(timeout_s)} s ({self.src})")
        return False


# ---------------------------------------------------------------- actors


class Gateway:
    """Gateway control: shell commands, a Tasmota mains switch on the LAN, or manual.

    type = "shell"   -> run gateway.off_cmd / on_cmd (ssh/systemctl/...)
    type = "tasmota" -> switch gateway mains power via the Tasmota HTTP API
                        (http://<ip>/cm?cmnd=Power Off|On)
    type = "manual"  -> interactive prompt (fine for the attended stages;
                        stage-c refuses to run unattended this way)

    Mains-switch caveat: after switching ON, the gateway needs its boot time
    (~1-2 min) before it forwards again. `on_wait_s` covers that so the measured
    recovery times compare fairly with a service-stop method. The switch must
    power ONLY the gateway — server/ChirpStack must stay reachable for the
    arrival checks.
    """

    def __init__(self, cfg: dict, interactive: bool):
        cfg = cfg or {}
        self.type = (cfg.get("type") or
                     ("tasmota" if cfg.get("ip") else
                      "shell" if (cfg.get("off_cmd") or cfg.get("on_cmd")) else
                      "manual")).lower()
        self.off_cmd = cfg.get("off_cmd")
        self.on_cmd = cfg.get("on_cmd")
        self.ip = cfg.get("ip")
        self.password = cfg.get("password")
        default_wait = 90.0 if self.type == "tasmota" else 0.0
        self.on_wait_s = float(cfg.get("on_wait_s", default_wait))
        self.off_wait_s = float(cfg.get("off_wait_s", 5.0 if self.type == "tasmota" else 0.0))
        self.interactive = interactive

    def _run(self, cmd: str):
        print(f"  [gw] $ {cmd}")
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  [gw] rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()}")

    def _tasmota_cm(self, cmnd: str) -> str:
        from urllib.parse import urlencode
        q = {"cmnd": cmnd}
        if self.password:
            q["password"] = self.password
        url = f"http://{self.ip}/cm?{urlencode(q)}"
        print(f"  [gw] Tasmota {url}")
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.read().decode(errors="replace").strip()
        except Exception as e:
            raise Fail(f"Tasmota switch unreachable ({self.ip}): {e} — fix gateway.type/ip "
                       "in the config; the stages must not run with a wrong gateway state")

    def state(self) -> None:
        """Read-only: query the switch state (tasmota only)."""
        if self.type != "tasmota":
            print(f"  [gw] state query needs type=tasmota (configured: {self.type})")
            return
        print(f"  [gw] Power state: {self._tasmota_cm('Power')}")

    def _tasmota(self, state: str):
        body = self._tasmota_cm(f"Power {state}")
        print(f"  [gw] {body}")
        if state.upper() not in body.upper():
            raise Fail(f"Tasmota did not report Power {state} (got: {body!r}) — "
                       "the stage stops rather than run with a wrong gateway state")

    def _manual(self, what: str):
        if not self.interactive:
            raise Fail(f"gateway {what} required, no command configured, stdin not interactive")
        input(f">>> Turn the gateway {what.upper()} now, then press Enter... ")

    def off(self):
        if self.type == "tasmota":
            self._tasmota("Off")
            if self.off_wait_s:
                print(f"  [gw] settling {self.off_wait_s:.0f} s after gateway-off")
                time.sleep(self.off_wait_s)
        elif self.type == "shell" and self.off_cmd:
            self._run(self.off_cmd)
        else:
            self._manual("off")

    def on(self):
        if self.type == "tasmota":
            self._tasmota("On")
            if self.on_wait_s:
                print(f"  [gw] waiting {self.on_wait_s:.0f} s for the gateway to boot before timing recovery")
                time.sleep(self.on_wait_s)
        elif self.type == "shell" and self.on_cmd:
            self._run(self.on_cmd)
            if self.on_wait_s:
                time.sleep(self.on_wait_s)
        else:
            self._manual("on")


class Node:
    def __init__(self, slog: SerialLog, interactive: bool):
        self.slog = slog
        self.interactive = interactive

    def reboot(self):
        """Reboot via ATZ (RUI3 'MCU reset' — the BSP's own ATCMD_REBOOT). Never
        AT+BOOT: that enters the DFU bootloader and the node sits silent until
        power-cycled. Returns send time."""
        how = "a"
        if self.interactive:
            how = input(">>> reboot: [a]=send ATZ / [m]=you power-cycle: ").strip().lower() or "a"
        if how == "m":
            input(">>> Power-cycle the node now, then press Enter... ")
        else:
            self.slog.write("ATZ\r\n")
        return utcnow()


# ---------------------------------------------------------------- runner


@dataclass
class Step:
    name: str
    status: str  # PASS / FAIL / WARN / INFO
    detail: str
    ts: datetime = field(default_factory=utcnow)


class Fail(Exception):
    pass


MARK = {"PASS": "[PASS]", "FAIL": "[FAIL]", "WARN": "[WARN]", "INFO": "[ .. ]"}


class Runner:
    def __init__(self, cfgpath: Path):
        self.cfg = json.loads(cfgpath.read_text())
        self.factor = float(self.cfg.get("factor", 1.0))
        self.eui = self.cfg["dev_eui"]
        self.dir = HERE / self.cfg.get("log_dir", "logs") / utcnow().strftime("%Y%m%d_%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.slog = SerialLog(self.cfg["serial_port"], self.cfg.get("baud", 115200), self.dir / "raw.log")
        print(f"  [log] {self.dir / 'raw.log'}")
        self.server = ServerWatch(self.eui, ChirpStack(self.cfg.get("chirpstack")), DbProbe(self.cfg.get("loralarm_db")), MqttWatch(self.cfg.get("mqtt"), self.eui))
        self.gw = Gateway(self.cfg.get("gateway"), interactive=sys.stdin.isatty())
        self.node = Node(self.slog, interactive=sys.stdin.isatty())
        self.steps: list[Step] = []
        self.values: dict[str, object] = {}
        self.t_boot: datetime | None = None
        self.scan = 0        # forward-only tape cursor (index into slog.lines)
        self.last_ts: datetime | None = None

    def S(self, seconds: float) -> float:
        """Scale short waits by `factor` (harness debugging only; stage-c forbids it)."""
        return seconds * self.factor

    def step(self, name: str, status: str, detail: str):
        self.steps.append(Step(name, status, detail))
        print(f"  {MARK[status]} {name}: {detail}")

    def annotate(self, text: str):
        self.slog.fh.write(f"#### {iso(utcnow())} {text}\n")
        self.slog.fh.flush()
        print(f"  [ -- ] {text}")

    def wait_for(self, name: str, pattern: str, timeout_s: float, *,
                 since: datetime | None = None,
                 abort_on: list[tuple[str, str]] | None = None) -> re.Match:
        """Wait for a pattern in the serial log.

        Scanning is a strictly FORWARD tape from the last matched line
        (`self.scan`), so a line printed in the same poll batch as the previous
        step's line is never skipped. With `since=<ts>` the buffer is searched
        from the start, but only lines with ts >= since count — use the ts of the
        REFERENCE line (self.last_ts of the previous wait), never utcnow(): the
        follow-up line often carries the same second as the reference.
        Lines examined for abort_on also respect the same bounds.
        """
        pat = re.compile(pattern)
        aborts = [(re.compile(p), why) for p, why in (abort_on or [])]
        deadline = utcnow() + timedelta(seconds=timeout_s)
        while utcnow() < deadline:
            start_idx = 0 if since is not None else self.scan
            for idx in range(start_idx, len(self.slog.lines)):
                ts, line = self.slog.lines[idx]
                if since is not None and ts < since:
                    continue
                for ap, why in aborts:
                    if ap.search(line):
                        self.step(name, "FAIL", f"abort condition: {line.strip()}")
                        raise Fail(why)
                m = pat.search(line)
                if m:
                    self.scan = max(self.scan, idx + 1)
                    self.last_ts = ts
                    self.step(name, "PASS", line.strip())
                    return m
            time.sleep(0.25)
        tail = "\n".join(f"    | {l.strip()}" for _, l in self.slog.lines[-15:])
        self.step(name, "FAIL", f"timeout ({int(timeout_s)} s) waiting /{pattern}/")
        print(tail)
        raise Fail(f"timeout waiting for {name} ({pattern})")

    def expect_any(self, name: str, options: dict[str, str], timeout_s: float,
                   since: datetime | None = None) -> tuple[str, re.Match]:
        """options: pattern -> label. Returns (label, match) of the first hit.

        Uses the same bounds logic as wait_for: forward-only from self.scan,
        or the whole buffer filtered to ts >= `since`. `since` matters after
        wait_boot: boot-outcome lines (e.g. 'join() returned false') print
        BEFORE the 'Send interval found' line wait_boot stops at, so a plain
        forward scan would never see them — pass since=r.t_send (the reboot).
        """
        pats = [(re.compile(p), label) for p, label in options.items()]
        deadline = utcnow() + timedelta(seconds=timeout_s)
        while utcnow() < deadline:
            start_idx = 0 if since is not None else self.scan
            for idx in range(start_idx, len(self.slog.lines)):
                ts, line = self.slog.lines[idx]
                if since is not None and ts < since:
                    continue
                for p, label in pats:
                    m = p.search(line)
                    if m:
                        self.scan = max(self.scan, idx + 1)
                        self.last_ts = ts
                        self.step(name, "PASS", f"{label}: {line.strip()}")
                        return label, m
            time.sleep(0.25)
        self.step(name, "FAIL", "timeout — none of: " + ", ".join(options.values()))
        raise Fail(f"timeout waiting for {name}")

    def collect(self, pattern: str, duration_s: float) -> list[tuple[datetime, re.Match]]:
        """Non-asserting: gather matches for duration seconds (returns immediately-past too)."""
        pat = re.compile(pattern)
        i = len(self.slog.lines)
        end = utcnow() + timedelta(seconds=duration_s)
        out = []
        while utcnow() < end:
            lines = self.slog.lines[i:]
            i += len(lines)
            for ts, line in lines:
                m = pat.search(line)
                if m:
                    out.append((ts, m))
            time.sleep(0.5)
        return out

    def wait_boot(self, reason: str, boot_timeout: float = 180) -> datetime:
        t_send = self.node.reboot()
        self.t_send = t_send
        self.annotate(f"rebooting node ({reason})")
        # since=t_send (the AT+RESET send), NOT the banner detect time: lines from
        # the previous boot are excluded by it, but a line printed in the same
        # poll gap as the banner is still valid.
        self.wait_for("boot banner", BOOT_BANNER, boot_timeout, since=t_send,
                     abort_on=[(BOOT_MODE, "the node entered DFU boot mode (AT+BOOT was sent?) — "
                                           "power-cycle it to recover; the harness queues nothing else")])
        self.t_boot = utcnow()
        self.values.setdefault("boots", []).append(iso(self.t_boot))
        m = self.wait_for("send interval restored", SEND_INTERVAL_FOUND,
                          self.S(300), since=t_send)
        ms = int(m.group(1))
        self.values["send_interval_ms"] = ms
        if ms == 0:
            self.step("SENDINT", "FAIL", "send_interval is 0 — set AT+SENDINT=300 (no AT setter in the harness)")
            raise Fail("SENDINT=0 (P6)")
        return self.t_boot

    def finish(self, stage: str, status: str):
        rows = "\n".join(f"| {iso(s.ts)} | {s.status} | {s.name} | {s.detail} |" for s in self.steps)
        (self.dir / "report.md").write_text(
            f"# {stage} — {status}   {iso(utcnow())}\n\ndev_eui: {self.eui}\n\n"
            f"| time | status | step | detail |\n|---|---|---|---|\n{rows}\n\n"
            f"## values\n\n```json\n{json.dumps(self.values, indent=2, ensure_ascii=False, default=str)}\n```\n",
            encoding="utf-8")
        (self.dir / "timeline.json").write_text(
            json.dumps({"stage": stage, "status": status, "dev_eui": self.eui,
                        "steps": [{"ts": iso(s.ts), "name": s.name, "status": s.status, "detail": s.detail}
                                  for s in self.steps],
                        "values": self.values},
                       indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        self.slog.close()
        print(f"  [report] {self.dir / 'report.md'}")


# ---------------------------------------------------------------- stages


def check_server_arrival(r: Runner, after: datetime, timeout_s: float, step_name: str) -> bool:
    ok = r.server.watch_fresh_after(after, timeout_s)
    if ok is None:
        r.step(step_name, "WARN", "no server probe configured — verify arrival manually")
    elif ok:
        r.step(step_name, "PASS", "server received a frame")
    else:
        r.step(step_name, "FAIL", f"no frame on the server within {int(timeout_s)} s")
    return ok is not False


def stage_a(r: Runner):
    print("\n== Stage A: link check on test values (~30 min) ==")
    r.wait_boot("A: fresh start after flashing the test build")

    r.wait_for("A2 joined", JOINED, r.S(330))
    joined_ts = r.last_ts
    r.values["joined"] = iso(joined_ts)
    r.wait_for("A2 link policy", LINK_POLICY, r.S(120), since=joined_ts)
    r.wait_for("A2 first heartbeat", TX_SUCCESSFUL, r.S(240), since=joined_ts)
    fc = r.server.fcnt_up()
    if fc is None:
        r.step("A2 fcnt", "WARN", "no ChirpStack probe — FCnt checks manual")
    else:
        r.values["fcnt_pre_outage"] = fc

    r.annotate("A3: gateway OFF")
    r.gw.off()
    r.values["gw_off"] = iso(utcnow())

    r.wait_for("A4 link check due", LINK_CHECK_DUE, r.S(780), since=joined_ts)  # 10 min of 1-min ticks + join skew
    r.wait_for("A4 confirmed send", LINK_CHECK_SEND, r.S(420))
    r.wait_for("A4 stack evt", SEND_CONFIRMED_FAILED_EVT, r.S(240))

    # the decisive wait: FAILED with status n; OK means AS1 is wrong
    label, m = r.expect_any("A5 check result", {
        LINK_CHECK_FAILED: "failed",
        LINK_CHECK_OK: "ok",
    }, r.S(240))
    if label != "failed":
        r.step("A5 AS1", "FAIL", "link check reported OK with the gateway off — AS1 is WRONG")
        return r.finish("stage-a", "FAIL")
    r.values["status_n"] = int(m.group(1))
    r.step("A5 n", "PASS", f"status n = {r.values['status_n']}")

    as2_abort = [(JOIN_REFUSED, "AS2 WRONG: join() returned false after the failed link check — the forced rejoin never runs")]
    r.wait_for("A6 forcing rejoin", FORCING_REJOIN, r.S(180), abort_on=as2_abort)
    r.wait_for("A6 join started", JOIN_STARTED, r.S(120), abort_on=as2_abort)

    fails = r.collect(JOIN_FAIL, r.S(330))
    if not fails:
        r.step("A7 join attempts", "FAIL", "no join-fail line within one interval after 'forcing a fresh join'")
        return r.finish("stage-a", "FAIL")
    r.step("A7 join attempts", "PASS", f"{len(fails)} join failure line(s) in ~5.5 min")
    skips = r.collect(NOT_JOINED_SKIP, r.S(60))
    r.step("A7 heartbeat skips", "PASS" if skips else "WARN",
           f"{len(skips)} 'Not joined, skip sending' while unjoined (60 s battery cadence)")

    m = r.wait_for("A8 test-mode rejoin line", STILL_NOT_JOINED, r.S(780))
    r.values["still_not_joined_min"] = int(m.group(1))
    if r.values["still_not_joined_min"] != 10:
        r.step("A8 value", "WARN", f"'Still not joined after {r.values['still_not_joined_min']} min' — expected 10 in the test build")

    r.annotate("A9: gateway ON")
    r.gw.on()
    gw_on = utcnow()
    r.values["gw_on"] = iso(gw_on)
    r.wait_for("A9 rejoined", JOINED, r.S(600))
    r.wait_for("A9 first heartbeat", TX_SUCCESSFUL, r.S(300))
    check_server_arrival(r, gw_on, r.S(600), "A9 server arrival")
    r.values["recovery_s"] = int((utcnow() - gw_on).total_seconds())

    r.step("A summary", "PASS",
           f"AS1 ok (status {r.values['status_n']}), AS2 ok, recovery {r.values['recovery_s']} s — "
           "restore production values: bench.py flash --mode production")
    return r.finish("stage-a", "PASS")


def stage_b1(r: Runner):
    print("\n== Stage B1: power-cycle in coverage, production build (~10 min) ==")
    fc_before = r.server.fcnt_up()
    if fc_before is None and r.server.src == "mqtt":
        ev = r.server.mqtt.wait_event(r.S(360))
        fc_before = ev["fcnt"] if ev else None
        if ev:
            r.step("B1.5 baseline event", "PASS",
                   f"FCntUp {ev['fcnt']} arrived at {iso(ev['ts'])} (mq TTL uplink, SENDINT=300)")
    if fc_before is None:
        r.step("B1.5 pre-fcnt", "WARN", "no server probe/event — frame-counter check manual")
    else:
        r.step("B1.5 pre-fcnt", "PASS", f"FCntUp before reboot = {fc_before}")

    r.wait_boot("B1 power-cycle in coverage")
    # Three valid boot outcomes on this BSP: a fresh join series, instant
    # "Already joined", or join() returning false because the stack is busy
    # restoring/joining in the background (joined! follows seconds later).
    # Which of "restore" vs "fresh join" it really was: B1.5 fCnt continuity —
    # observed on the bench: join() refused, joined 5 s later, fCnt RESET
    # (silent fresh join; session does not survive a reset on this BSP).
    label, _ = r.expect_any("B1.2 boot outcome", {
        JOIN_STARTED: "fresh-join",
        ALREADY_JOINED_CACHED: "cached-session",
        JOIN_REFUSED: "deferred (stack busy at boot, joined after)",
    }, r.S(300), since=r.t_send)
    r.values["b1_outcome"] = label
    r.wait_for("B1.3 link policy", LINK_POLICY, r.S(600))
    r.wait_for("B1.4 first heartbeat", TX_SUCCESSFUL, r.S(360))
    if not check_server_arrival(r, r.t_boot, r.S(600), "B1.4 server arrival"):
        return r.finish("stage-b1", "FAIL")

    fc_after = r.server.fcnt_up()
    if fc_before is not None and fc_after is not None:
        r.values["fcnt_before"], r.values["fcnt_after"] = fc_before, fc_after
        if fc_after >= fc_before:
            r.step("B1.5 fcnt continuity", "PASS", f"{fc_before} -> {fc_after} (continued: session restored)")
        else:
            r.values["b1_fcnt_reset"] = True
            r.step("B1.5 fcnt continuity", "WARN",
                   f"{fc_before} -> {fc_after} (counter RESET: the session does not survive a "
                   "reset on this BSP — the node re-joined at boot; same finding as B2 outcome U)")
    return r.finish("stage-b1", "PASS")


def stage_b2(r: Runner):
    print("\n== Stage B2: power-cycle out of coverage (~25 min, either outcome is a pass) ==")
    r.gw.off()
    r.wait_boot("B2 power-cycle without coverage")
    label, _ = r.expect_any("B2.2 boot outcome", {
        JOIN_STARTED: "U (boots unjoined)",
        ALREADY_JOINED_CACHED: "C (cached session survives)",
        JOIN_REFUSED: "deferred (stack busy at boot — resolving)",
    }, r.S(300), since=r.t_send)
    if label.startswith("deferred"):
        # join() was refused at boot because the stack was busy (observed: it
        # silently re-joins/restores seconds later in coverage). With the
        # gateway off, the next line tells us which it was: joined! = restored
        # or a radio join that somehow got through; join fail = it went to the
        # air and lost.
        label, _ = r.expect_any("B2.2 deferred resolution", {
            JOINED: "C (joined without the gateway — restored session)",
            JOIN_FAIL: "U (tried the air, failed)",
        }, r.S(360))
    r.values["b2_outcome"] = label

    if label.startswith("U"):
        fails = r.collect(JOIN_FAIL, r.S(330))
        r.step("B2.2 join failures", "PASS" if fails else "FAIL",
               f"{len(fails)} join failure(s) in one interval (expected ~1 per 255 s)")
        tx = r.collect(TX_SUCCESSFUL, r.S(60))
        r.step("B2.2 heartbeats skipped", "PASS" if not tx else "WARN",
               "unjoined: heartbeats must skip, not transmit")
        r.annotate("B2: coverage back on (outcome U)")
        r.gw.on()
        r.wait_for("B2.3 rejoined in range", JOINED, r.S(600))
        check_server_arrival(r, utcnow(), r.S(600), "B2.3 server arrival")
    else:
        tx = r.collect(TX_SUCCESSFUL, r.S(330))
        r.step("B2.2 heartbeats into the void", "PASS" if tx else "FAIL",
               f"{len(tx)} 'Transmission successful' with the gateway off (outcome C)")
        joins = r.collect(JOIN_FAIL, r.S(330))
        r.step("B2.2 no join attempts", "PASS" if not joins else "WARN",
               "outcome C: no join attempts; a dead server session is caught only by the daily link check")
        r.annotate("B2: coverage back on (outcome C)")
        r.gw.on()
        check_server_arrival(r, utcnow(), r.S(600), "B2.3 recovery by heartbeat")
    r.step("B2.3 outcome", "PASS",
           f"outcome {r.values['b2_outcome']} — the session cache "
           f"{'survives' if label.startswith('C') else 'does not survive'} a power cycle on this BSP")
    return r.finish("stage-b2", "PASS")


def stage_c(r: Runner):
    print("\n== Stage C: 25 h soak on production timers (real-time waits) ==")
    if r.factor != 1.0:
        print("  [cfg] factor != 1.0 not allowed for stage-c (real timers)")
        raise Fail("factor must be 1.0 for stage-c")
    if r.gw.type == "manual":
        print("  [gw] gateway control not configured (no shell cmd / Tasmota switch): stage-c would "
              "need you awake at T0+23 h and T0+24.5 h. Configure it for an unattended run.")
        if not sys.stdin.isatty():
            raise Fail("unattended stage-c requires gateway control (shell cmd or Tasmota switch)")

    r.step("C0 alarm snooze", "WARN",
           "snooze the server's stale/battery alarms for this node before continuing")

    fc_before = r.server.fcnt_up()
    if fc_before is not None:
        r.values["fcnt_before_s1"] = fc_before

    t0 = r.wait_boot("C1 T0")
    r.step("C1 T0", "INFO", f"T0 = {iso(t0)}")
    r.wait_for("C1 joined-or-cached", f"({JOINED})|({ALREADY_JOINED_CACHED})", 2 * 3600)
    if not check_server_arrival(r, t0, 3600, "C1 baseline frames"):
        return r.finish("stage-c", "FAIL")
    r.annotate(f"C1 baseline ok — sleeping until T0+23:00 ({iso(t0 + timedelta(hours=23))})")
    sleep_until(t0 + timedelta(hours=23))

    r.annotate("C2: gateway OFF (T0+23:00)")
    r.gw.off()
    ls, src = r.server.last_seen()
    r.step("C2 server quiet", "INFO", f"last_seen={iso(ls) if ls else None} ({src}); "
           "radio silent until the ~T0+24:00 link check")
    silent_since = utcnow()

    # due window: the 24th joined tick — somewhere between T0+23:00 (if joined at boot)
    # and ~T0+26 h (if joined up to an hour late / rx delays)
    m = r.wait_for("C3 link check due", LINK_CHECK_DUE, 8 * 3600, since=silent_since)
    r.values["check_due"] = iso(utcnow())
    r.wait_for("C3 confirmed send", LINK_CHECK_SEND, 420)
    label, m = r.expect_any("C3 check result", {
        LINK_CHECK_FAILED: "failed",
        LINK_CHECK_OK: "ok",
    }, 300)
    if label != "failed":
        r.step("C3 link check", "FAIL", "network answered although the gateway is off — "
               "check that gateway.off_cmd really takes the node's network path down")
        return r.finish("stage-c", "FAIL")
    r.values["status_n"] = int(m.group(1))
    r.step("C3 n", "PASS", f"status n = {r.values['status_n']}")
    r.wait_for("C3 forcing rejoin", FORCING_REJOIN, 180,
               abort_on=[(JOIN_REFUSED, "AS2 wrong in production too: join() refused while the node thinks it is joined")])

    gw_on_at = max(t0 + timedelta(hours=24, minutes=30), utcnow() + timedelta(minutes=5))
    r.annotate(f"C4: sleeping until gateway-on at {iso(gw_on_at)}")
    sleep_until(gw_on_at)
    r.annotate("C4: gateway ON")
    r.gw.on()
    gw_on = utcnow()
    r.values["gw_on"] = iso(gw_on)
    r.wait_for("C4 rejoined", JOINED, 600)
    r.wait_for("C4 first heartbeat", TX_SUCCESSFUL, 360)
    if not check_server_arrival(r, gw_on, 600, "C4 server arrival"):
        return r.finish("stage-c", "FAIL")
    fc_after = r.server.fcnt_up()
    if fc_before is not None and fc_after is not None:
        r.values["fcnt_after_s2"] = fc_after
        r.step("C4 fcnt new session", "PASS" if fc_after < fc_before else "WARN",
               f"FCntUp {fc_before} -> {fc_after} (counter resets at a rejoin — expected)")
    r.values["recovery_s"] = int((utcnow() - gw_on).total_seconds())
    r.step("C5 recovery", "PASS", f"recovered {r.values['recovery_s']} s after gateway-on (target <= ~600 s)")
    return r.finish("stage-c", "PASS")


def sleep_until(t: datetime):
    while True:
        remaining = (t - utcnow()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 300))


# ---------------------------------------------------------------- flash helper


def flash(mode: str, cfg: dict):
    """A1/A10: switch the config.h defines (backup + restore), build and upload.

    config.h is ALWAYS restored afterwards (from the file copy, not git), so no
    uncommitted local edits are ever touched.
    """
    if not (cfg.get("arduino", {}) or {}).get("fqbn"):
        raise SystemExit("bench_config.json needs arduino.fqbn for flash (arduino-cli required)")
    backup = CONFIG_H.with_suffix(".h.bench_bak")
    shutil.copy2(CONFIG_H, backup)
    try:
        src = CONFIG_H.read_text()
        for define, value in VALUES[mode].items():
            src = re.sub(rf"(#define {define}\s+)\S+", rf"\g<1>{value}", src, count=1)
        CONFIG_H.write_text(src)
        # Verify by reading back: catches a pattern that genuinely matched nothing,
        # and passes when config.h already held the target value (no-op is fine).
        for define, value in VALUES[mode].items():
            m = re.search(rf"#define {define}\s+(\S+)", CONFIG_H.read_text())
            if not m or m.group(1) != value:
                raise SystemExit(f"config.h verify failed: {define}={m.group(1) if m else 'MISSING'}, wanted {value}")
            print(f"  [flash] {define} = {value} ({mode})")

        fqbn = cfg["arduino"]["fqbn"]
        port = cfg["serial_port"]
        for cmd in (["arduino-cli", "compile", "--fqbn", fqbn, str(SKETCH_DIR)],
                    ["arduino-cli", "upload", "--fqbn", fqbn, "--port", port, str(SKETCH_DIR)]):
            print(f"  [flash] $ {' '.join(cmd)}")
            if subprocess.run(cmd).returncode != 0:
                raise SystemExit(f"command failed: {' '.join(cmd)}")
        print(f"  [flash] done: {mode} values uploaded. Reboot/serial re-enumeration may take a few seconds.")
    finally:
        shutil.copy2(backup, CONFIG_H)
        backup.unlink()
        print("  [flash] config.h restored (production values are the committed state)")


# ---------------------------------------------------------------- probe


def probe(cfg: dict, seconds: float, send_at: bool) -> int:
    """Pre-flight: open the port, optionally query AT+STATUS, print what comes back.

    Passes when ANY full line was received — proves port, baud and a live node.
    AT+STATUS additionally prints join state and the SENDINT that the stages
    require to be non-zero (P6).
    """
    port, baud = cfg["serial_port"], cfg.get("baud", 115200)
    print(f"[probe] {port} @ {baud}, listening {seconds:.0f} s (Ctrl-C to stop early)")
    try:
        ser = serial.Serial(port, baud, timeout=0.2)
    except serial.SerialException as e:
        print(f"[probe] FAIL: cannot open {port}: {e}")
        return 1
    if send_at:
        # RUI3 custom commands registered with api.system.atMode.add are invoked
        # as AT+NAME; some builds only accept ATC+ — try both, both are read-only.
        for cmd in ("AT+STATUS", "ATC+STATUS"):
            ser.write((cmd + "\r\n").encode())
            print(f"[probe] sent: {cmd}")
    n = 0
    try:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            data = ser.read(1024)
            if data:
                for raw in data.split(b"\n"):
                    line = raw.decode("utf-8", "replace").rstrip("\r")
                    if line:
                        n += 1
                        print(f"[probe] {iso(utcnow())} {line}")
    except KeyboardInterrupt:
        print("[probe] stopped by user")
    ser.close()
    if n == 0:
        print(f"[probe] FAIL: no data at all on {port} — wrong port? baud? node off?")
        return 1
    print(f"[probe] {n} line(s) received — port and node are alive")
    return 0


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "bench_config.json"),
                    help="json config (default: tests/bench/bench_config.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("monitor")
    sub.add_parser("stage-a")
    sub.add_parser("stage-b1")
    sub.add_parser("stage-b2")
    sub.add_parser("stage-c")
    p = sub.add_parser("probe")
    p.add_argument("--seconds", type=float, default=15.0)
    p.add_argument("--at", action="store_true", help="also send AT/ATC+STATUS (RUI3 custom prefix is ATC+)")
    srv = sub.add_parser("server")
    srv.add_argument("--base-url", default=None, help="override chirpstack.base_url (e.g. to probe two servers)")
    srv.add_argument("--seconds", type=float, default=0.0,
                     help="with the MQTT probe: wait up to this many seconds for the first uplink event")
    si = sub.add_parser("sendint")
    si.add_argument("seconds", type=int, help="e.g. 300 -> ATC+SENDINT=300 (0 disables uplinks)")
    gw = sub.add_parser("gw")
    gw.add_argument("--on", action="store_true", help="switch the gateway socket ON")
    gw.add_argument("--off", action="store_true", help="switch the gateway socket OFF")
    gw.add_argument("--wait", action="store_true", help="also apply the configured on_wait_s/off_wait_s")
    fp = sub.add_parser("flash")
    fp.add_argument("--mode", choices=["test", "production"], required=True)
    args = ap.parse_args()

    cfgpath = Path(args.config)
    if not cfgpath.exists():
        example = HERE / "bench_config.example.json"
        raise SystemExit(f"no config at {cfgpath}\n  cp {example} {cfgpath}  # then edit serial_port, dev_eui, gateway cmds")

    if args.cmd == "flash":
        flash(args.mode, json.loads(cfgpath.read_text()))
        return

    cfg = json.loads(cfgpath.read_text())
    if args.cmd == "monitor":
        d = HERE / cfg.get("log_dir", "logs") / f"monitor_{utcnow().strftime('%Y%m%d_%H%M%S')}"
        d.mkdir(parents=True, exist_ok=True)
        slog = SerialLog(cfg["serial_port"], cfg.get("baud", 115200), d / "raw.log")
        print(f"  [log] {d / 'raw.log'} — Ctrl-C to stop")
        try:
            while True:
                time.sleep(10)
        except KeyboardInterrupt:
            slog.close()
            print("  [log] closed")
        return

    if args.cmd == "probe":
        sys.exit(probe(cfg, args.seconds, args.at))

    if args.cmd == "gw":
        g = Gateway(cfg.get("gateway"), interactive=sys.stdin.isatty())
        if not args.wait:
            g.on_wait_s = 0.0  # standalone toggles return immediately unless --wait
            g.off_wait_s = 0.0
        if args.on:
            g.on()
        elif args.off:
            g.off()
        else:
            g.state()
        sys.exit(0)

    if args.cmd == "server":
        eui = cfg["dev_eui"]
        mq = MqttWatch(cfg.get("mqtt"), eui)
        if args.seconds > 0:
            ev = mq.wait_event(args.seconds) if mq.available else None
            if ev:
                print(f"[server] uplink seen: fCnt={ev['fcnt']} fPort={ev['fport']} at {iso(ev['ts'])}")
            else:
                print(f"[server] no uplink within {args.seconds:.0f} s")
                sys.exit(1)
            sys.exit(0)
        if mq.available:
            evs = mq.events
            if evs:
                ev = evs[-1]
                print(f"[server] {eui.lower()}: {len(evs)} event(s), latest fCnt={ev['fcnt']} "
                      f"fPort={ev['fport']} at {iso(ev['ts'])}")
            else:
                print(f"[server] {eui.lower()}: connected, no event yet "
                      f"(use --seconds <t> to wait for one)")
                sys.exit(1)
            sys.exit(0)
        cs_cfg = dict(cfg.get("chirpstack") or {})
        if args.base_url:
            cs_cfg["base_url"] = args.base_url
        cs = ChirpStack(cs_cfg)
        if not cs.available:
            print("[server] configure mqtt.host or chirpstack.base_url/email/password "
                  "in bench_config.json first")
            sys.exit(1)
        ls = cs.last_seen(eui)
        fc = cs.fcnt_up(eui)
        print(f"[server] {eui.lower()}: last_seen = {ls}")
        print(f"[server] {eui.lower()}: fcnt_up  = {fc}")
        if ls is None and fc is None:
            print("[server] login ok but device unknown or no session on this server "
                  "(wrong ChirpStack instance?)")
            sys.exit(1)
        sys.exit(0)

    if args.cmd == "sendint":
        # RUI3 custom AT commands (api.system.atMode.add) take the ATC+ prefix;
        # native commands (ATZ, AT+BOOT) take AT+ / plain.
        ser_port = serial.Serial(cfg["serial_port"], cfg.get("baud", 115200), timeout=0.2)
        ser_port.write(f"ATC+SENDINT={args.seconds}\r\n".encode())
        print(f"[sendint] sent ATC+SENDINT={args.seconds}")
        reply = ""
        end = time.monotonic() + 8
        while time.monotonic() < end:
            data = ser_port.read(512)
            if data:
                reply += data.decode("utf-8", "replace")
                if "OK" in reply or "ERROR" in reply:
                    break
        print(f"[sendint] reply: {reply.strip()!r}")
        print("[sendint] verified on the next boot by the 'Send interval found' log line")
        ser_port.close()
        sys.exit(0)

    stage = {"stage-a": stage_a, "stage-b1": stage_b1, "stage-b2": stage_b2, "stage-c": stage_c}[args.cmd]
    r = Runner(cfgpath)
    try:
        stage(r)
    except Fail as e:
        print(f"\n  [result] FAIL: {e}")
        r.finish(args.cmd, "FAIL")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n  [result] interrupted — partial report written")
        r.finish(args.cmd, "INTERRUPTED")
        sys.exit(130)


if __name__ == "__main__":
    main()
