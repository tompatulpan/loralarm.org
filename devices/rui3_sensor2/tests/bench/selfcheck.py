#!/usr/bin/env python3
"""Offline self-check for the bench harness — no hardware needed.

Validates, against the real files in this repo:
  1. every serial pattern matches its exact firmware log string
  2. the `flash` config.h substitution works for both modes (on a copy,
     the real config.h is only read, never written)
  3. bench_config.example.json parses

Run: python3 tests/bench/selfcheck.py
"""

import json
import re
import sys
from pathlib import Path

sys.modules.setdefault("serial", type(sys)("serial"))  # allow import without pyserial
sys.path.insert(0, str(Path(__file__).parent))
import bench  # noqa: E402

HERE = Path(__file__).parent
SKETCH = HERE.parent.parent

fails = 0

# 1) patterns vs firmware strings (taken verbatim from lorawan.cpp / uplink.cpp /
#    custom_at.cpp / rui3_sensor2.ino)
samples = {
    bench.BOOT_BANNER: "LoraLarm.org RAKwireless RUI3 Node",
    bench.JOINED: "[JOIN-CB] LoRaWan OTAA - joined!",
    bench.JOIN_FAIL: "[JOIN-CB] LoRaWan OTAA - join fail! Next retry in 255 s (auto-rejoin active)",
    bench.JOIN_STARTED: "[SETUP] Join started — retry every 255 s, up to 255 attempts",
    bench.JOIN_REFUSED: "[SETUP] join() returned false — cached session restore in progress or stack busy. Waiting for joinCallback...",
    bench.ALREADY_JOINED_CACHED: "[SETUP] Already joined (cached session) — skipping join setup",
    bench.LINK_POLICY: "[JOIN-CB] Link policy: ADR=off, TXP=0 (~16 dBm EIRP), DR=3",
    bench.LINK_CHECK_DUE: "[LINK] Link check due: next uplink goes out confirmed",
    bench.LINK_CHECK_SEND: "[UPLINK] Sending packet over LoRaWAN (DR=3 TXP=0 ADR=off, confirmed link check)",
    bench.LINK_CHECK_FAILED: "[LINK] Link check FAILED (status 12): no answer from the network — rejoin requested",
    bench.LINK_CHECK_OK: "[LINK] Link check OK: network answered",
    bench.FORCING_REJOIN: "[LINK] Forcing a fresh join (up to 255 attempts)",
    bench.STILL_NOT_JOINED: "[SETUP] Still not joined after 10 min — starting a new join series",
    bench.TX_SUCCESSFUL: "[TX-CB] Transmission successful - ready for sleep",
    bench.NOT_JOINED_SKIP: "[UPLINK] Not joined, skip sending",
    bench.SEND_INTERVAL_FOUND: "[AT_CMD] Send interval found 300000",
    bench.SEND_CONFIRMED_FAILED_EVT: "+EVT:SEND_CONFIRMED_FAILED(12)",
    bench.JOINED + "": "[JOIN-CB] LoRaWan OTAA - joined!",
}
for pat, line in samples.items():
    if re.search(pat, line):
        print(f"  ok   /{pat[:46]:<48}/ <- {line[:58]}")
    else:
        fails += 1
        print(f"  FAIL /{pat}/ vs {line!r}")

# 2) flash substitution: prove the pattern rewrites the defines no matter what the
#    file currently holds (sentinel swap instead of change detection), and report
#    which mode the working tree config.h is currently in.
text = (SKETCH / "config.h").read_text()
current = {d: (re.search(rf"#define {d}\s+(\S+)", text) or [None, "MISSING"])[1]
           if re.search(rf"#define {d}\s+(\S+)", text) else "MISSING"
           for d in bench.VALUES["production"]}
mode_now = ("test" if current == bench.VALUES["test"]
            else "production" if current == bench.VALUES["production"] else "unknown")
print(f"  info config.h currently holds {current} -> {mode_now}")
for mode in ("test", "production"):
    src = text
    for define, _ in bench.VALUES[mode].items():
        # first prove the pattern matches by swapping in a sentinel
        src = re.sub(rf"(#define {define}\s+)\S+", rf"\g<1>999BENCH", src, count=1)
        if "999BENCH" not in src:
            fails += 1
            print(f"  FAIL flash: pattern not found in config.h: {define}")
            continue
        value = bench.VALUES[mode][define]
        src = re.sub(rf"(#define {define}\s+)999BENCH", rf"\g<1>{value}", src, count=1)
    got = {d: (re.search(rf"#define {d}\s+(\S+)", src) or [None, "MISSING"])[1]
           for d in bench.VALUES[mode]}
    ok = all(got[d] == v for d, v in bench.VALUES[mode].items())
    print(f"  {'ok  ' if ok else 'FAIL'} flash-{mode}: {got}")
    fails += 0 if ok else 1

# 3) gateway type inference and manual-mode refusal
cases = [
    ({"type": "tasmota", "ip": "1.2.3.4"}, "tasmota", 90.0),
    ({"ip": "1.2.3.4"}, "tasmota", 90.0),            # ip alone implies tasmota
    ({"off_cmd": "x", "on_cmd": "y"}, "shell", 0.0),  # cmds alone imply shell
    ({}, "manual", 0.0),
]
for cfg, want_type, want_wait in cases:
    g = bench.Gateway(cfg, interactive=False)
    ok = g.type == want_type and g.on_wait_s == want_wait
    print(f"  {'ok  ' if ok else 'FAIL'} gateway type: {want_type} (on_wait={g.on_wait_s:.0f})")
    fails += 0 if ok else 1
try:
    bench.Gateway({}, interactive=False).off()
    fails += 1
    print("  FAIL manual gateway off() without a tty must raise Fail")
except bench.Fail:
    print("  ok   manual gateway off() without a tty raises Fail")

# 4) example config parses
cfg = json.loads((HERE / "bench_config.example.json").read_text())
print(f"  ok   bench_config.example.json (keys: {', '.join(cfg)})")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
