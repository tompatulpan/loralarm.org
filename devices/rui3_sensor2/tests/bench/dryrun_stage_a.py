#!/usr/bin/env python3
"""Host dry-run of stage-a against a scripted serial log — no hardware required.

Feeds a realistic Stage-A log timeline (join, gateway off, link check due, FAILED(12),
forced rejoin, gateway on, recovery) through the real Runner.wait_for/expect_any/
collect machinery with a scaled `factor`, then asserts the values the report
would carry. Validates the harness logic itself before it is trusted at the bench:

    python3 tests/bench/dryrun_stage_a.py     -> prints PASS/FAIL
"""

import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.modules.setdefault("serial", type(sys)("serial"))  # allow import without pyserial
sys.path.insert(0, str(Path(__file__).parent))
import bench  # noqa: E402

FACTOR = 0.02  # scale all short stage-a waits


def utcnow():
    return datetime.now(timezone.utc)


class FakeLog:
    """Same interface the Runner uses: .lines, .fh.write/flush, .write(), .close()."""

    def __init__(self):
        self.lines = []
        self.dir = Path(tempfile.mkdtemp())

        class FH:
            def write(self, s):
                pass

            def flush(self):
                pass

        self.fh = FH()

    def emit(self, line):
        self.lines.append((utcnow(), f"[SIM] {line}"))

    def write(self, s):
        pass  # AT+BOOT from Node.reboot

    def close(self):
        pass


class FakeNode:
    def __init__(self, slog, started):
        self.slog = slog
        self.started = started

    def reboot(self):
        # model the real node: the boot banner appears AFTER the reset command
        # (RAK3172 boot loop ~seconds); a 0.25 s delay in sim time rules out the
        # scheduling race where the banner lands before t_send
        t = threading.Timer(0.25, lambda: self.slog.emit("LoraLarm.org RAKwireless RUI3 Node"))
        t.daemon = True
        t.start()
        return utcnow()


class FakeGateway:
    def __init__(self):
        self.events = []

    def off(self):
        self.events.append(("off", utcnow()))

    def on(self):
        self.events.append(("on", utcnow()))


class FakeServer:
    def fcnt_up(self):
        return 123

    def watch_fresh_after(self, t0, timeout_s):
        return True

    def last_seen(self):
        return utcnow(), "sim"


def build_runner():
    r = object.__new__(bench.Runner)
    slog = FakeLog()
    started = threading.Event()
    r.factor = FACTOR
    r.eui = "sim".rjust(16, "0")
    r.dir = slog.dir
    r.slog = slog
    r.node = FakeNode(slog, started)
    r.gw = FakeGateway()
    r.server = FakeServer()
    r.steps = []
    r.values = {}
    r.t_boot = None
    r.scan = 0
    r.last_ts = None
    return r, slog


def feeder(r, slog):
    """Scripted Stage-A timeline, timed to land while the stage is waiting
    for each line (the harness polls every 0.25 s; line times below include
    margin so nothing arrives in a poll gap before its wait starts)."""
    def after(d, line):
        t = threading.Timer(d, lambda: slog.emit(line))
        t.daemon = True
        t.start()

    after(0.25 + 0.10, "[AT_CMD] Send interval found 300000")   # wait at ~0.35 s: after the 0.25 s banner
    after(1.20, "[JOIN-CB] LoRaWan OTAA - joined!")             # A2 (~0.9 s: after wait starts)
    after(1.60, "[JOIN-CB] Link policy: ADR=off, TXP=0 (~16 dBm EIRP), DR=3")
    after(2.00, "[TX-CB] Transmission successful - ready for sleep")
    # gateway off happens when the script reaches A3 (~2.2 s)
    after(4.00, "[LINK] Link check due: next uplink goes out confirmed")   # A4
    after(4.50, "[UPLINK] Sending packet over LoRaWAN (DR=3 TXP=0 ADR=off, confirmed link check)")
    after(5.00, "+EVT:SEND_CONFIRMED_FAILED(12)")
    after(5.60, "[TX-CB] Transmission failed with status 12")
    after(5.70, "[LINK] Link check FAILED (status 12): no answer from the network — rejoin requested")
    after(6.30, "[LINK] Forcing a fresh join (up to 255 attempts)")        # A6
    after(6.60, "[SETUP] Join started — retry every 255 s, up to 255 attempts")
    after(7.00, "[JOIN-CB] LoRaWan OTAA - join fail! Next retry in 255 s (auto-rejoin active)")
    # A7 collect(JOIN_FAIL, 6.6 s) runs ~7.3..13.9; one more failure inside it:
    after(8.50, "[JOIN-CB] LoRaWan OTAA - join fail! Next retry in 255 s (auto-rejoin active)")
    # A7 collect(skip, 1.2 s) runs ~13.9..15.1:
    after(14.20, "[UPLINK] Not joined, skip sending")
    # A8 wait (15.6 s) runs ~15.1..30.7:
    after(15.50, "[SETUP] Still not joined after 10 min — starting a new join series")
    # gateway on happens when the script reaches A9 (~15.7 s); rejoin:
    after(16.20, "[JOIN-CB] LoRaWan OTAA - joined!")            # A9 (must NOT match the 1.2 s one)
    after(16.40, "[UPLINK] Send timer started: 300000 ms")
    after(16.60, "[TX-CB] Transmission successful - ready for sleep")


def main():
    r, slog = build_runner()
    threading.Thread(target=feeder, args=(r, slog), daemon=True).start()
    try:
        bench.stage_a(r)
    except bench.Fail as e:
        print("dry-run FAIL:", e)
        for s in r.steps:
            print(f"  {s.status}: {s.name} — {s.detail}")
        return 1

    v = r.values
    checks = [
        (v.get("status_n") == 12, f"status_n captured = {v.get('status_n')}, want 12"),
        (v.get("send_interval_ms") == 300000, f"send_interval_ms = {v.get('send_interval_ms')}"),
        (v.get("still_not_joined_min") == 10, f"still_not_joined_min = {v.get('still_not_joined_min')}"),
        (len(r.gw.events) == 2 and r.gw.events[0][0] == "off", f"gateway events = {r.gw.events}"),
        (v.get("recovery_s", -1) >= 0, f"recovery_s = {v.get('recovery_s')}"),
        (any(s.name == "A7 join attempts" and s.status == "PASS" for s in r.steps), "A7 join attempts PASS"),
    ]
    bad = [msg for ok, msg in checks if not ok]
    for ok, msg in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {msg}")
    if bad:
        return 1
    print("\ndry-run PASS: stage-a runner logic works on a scripted log "
          f"({sum(1 for s in r.steps if s.status == 'PASS')} PASS steps)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
