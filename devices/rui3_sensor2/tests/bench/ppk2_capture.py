#!/usr/bin/env python3
"""PPK2 capture correlating RAK3172 sleep current spikes with the serial log.

Usage:
  python3 ppk2_capture.py capture --seconds 60 --port /dev/ttyACM0 [--mode smu --vout 3000 | --mode amp]
  python3 ppk2_capture.py analyze capture.csv [serial_raw.log]

Capture mode (default):
  - PPK2 source mode (--vout mV, default 3000 = 3.0 V bench supply level) or
    ampere meter mode (--mode amp, DUT powered externally through the AMP inputs).
  - Samples at 10 kHz, writes (pc_time_s, current_uA) rows to capture_<ts>.csv.
    pc_time_s is time.time() at the first sample plus the PPK2 sample index / rate.
  - IMPORTANT: close nRF Connect Power Profiler first; it holds the PPK2 port.

Analyze mode:
  - Loads capture.csv, computes baseline (median), finds spikes
    (samples above baseline + threshold), reports period, duty, charge per spike.
  - If a serial log path is given, compares spike-centre timestamps against the
    log's line timestamps (harness writes local-time second-resolution stamps):
    a spike cluster with no line within +/- 0.5 s = wake invisible to the app.

Requires: pip install --user --break-system-packages ppk2-api
"""
import argparse
import csv
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_RATE = 10000  # PPK2 10 kHz
SPIKE_UA_THRESHOLD = 150.0  # uA above baseline counts as a spike sample
SPIKE_GAP_S = 0.05  # samples closer than this belong to the same spike


def connect(port):
    from ppk2_api.ppk2_api import PPK2_API  # pylint: disable=import-outside-toplevel

    ppk2 = PPK2_API(serial_port=port)
    ppk2.connect()
    return ppk2


def capture(args):
    ppk2 = connect(args.port)
    try:
        if args.mode == "amp":
            print("  [ppk2] ampere meter mode (device must be powered externally, AMP inputs)")
            ppk2.use_ampere_meter()
        else:
            print(f"  [ppk2] source mode, Vout = {args.vout} mV")
            ppk2.use_power_measurements()  # Power (source) mode
            ppk2.set_power_measurement_voltage(args.vout)
        # NB: switching modes / setting Vout re-powers the DUT -> it reboots.
        rate = DEFAULT_RATE
        ppk2.set_sample_rate(rate)
        ppk2.start_measuring()

        out = Path(args.out)
        with out.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["pc_time_s", "current_uA"])
            t0 = None
            n = 0
            target = int(args.seconds * rate)
            while n < target:
                raw = ppk2.get_data()
                if raw is None or not raw["data"]:
                    time.sleep(0.05)
                    continue
                if t0 is None:
                    t0 = time.time()
                samples = ppk2.get_samples(raw)  # uA floats
                for i, ua in enumerate(samples):
                    w.writerow([f"{t0 + (n + i) / rate:.6f}", f"{ua:.2f}"])
                n += len(samples)
                if n % (rate * 10) == 0:
                    print(f"  [ppk2] {n // rate} s captured")
        ppk2.stop_measuring()
        print(f"  [ppk2] wrote {out} ({n} samples, ~{n / rate:.1f} s)")
    finally:
        ppk2.disconnect()


def analyze(args):
    rows = []
    with open(args.capture) as fh:
        reader = csv.reader(fh)
        next(reader)
        for t, ua in reader:
            rows.append((float(t), float(ua)))
    if not rows:
        raise SystemExit("no samples")

    baseline = statistics.median(u for _, u in rows)
    spikes = [ (t, u) for t, u in rows if u > baseline + SPIKE_UA_THRESHOLD ]

    # group spike samples into clusters separated by > SPIKE_GAP_S
    clusters = []
    for t, u in spikes:
        if clusters and t - clusters[-1][-1][0] <= SPIKE_GAP_S:
            clusters[-1].append((t, u))
        else:
            clusters.append([(t, u)])

    print(f"  samples: {len(rows)}, baseline(median): {baseline:.1f} uA")
    print(f"  spike clusters: {len(clusters)} in {rows[-1][0] - rows[0][0]:.1f} s")
    fmt = clusters[:12]
    for c in fmt if len(clusters) <= 40 else clusters[:12]:
        start, end = c[0][0], c[-1][0]
        peak = max(u for _, u in c)
        charge = (end - start) * sum((u - baseline) for _, u in c) / max(len(c), 1) * 1e0
        print(f"    spike @ {start:.3f}: {peak:.0f} uA peak, {(end - start) * 1000:.1f} ms, "
              f"~{charge * 1000:.0f} nAs excess")
    if len(clusters) > 12:
        print(f"    ... {len(clusters) - 12} more")

    if clusters:
        periods = [b[0][0] - a[-1][0] for a, b in zip(clusters, clusters[1:])]
        if periods:
            print(f"  period between spikes (start-to-start of gaps): median {statistics.median(periods) * 1000:.0f} ms")
        mean_current = statistics.mean(u for _, u in rows)
        print(f"  mean current {mean_current:.1f} uA, baseline {baseline:.1f} uA, "
              f"excess {mean_current - baseline:.1f} uA")
    else:
        print("  no spikes detected")

    if args.serial_log:
        # timestamps in SerialLog lines: "2026-11-16T19:17:40 ..." (local time)
        log_times = []
        for line in Path(args.serial_log).read_text().splitlines():
            try:
                log_times.append(datetime.strptime(line[:19], "%Y-%m-%dT%H:%M:%S").timestamp())
            except ValueError:
                pass
        if clusters and log_times:
            print(f"  serial log: {len(log_times)} stamped lines between {datetime.fromtimestamp(log_times[0])} and {datetime.fromtimestamp(log_times[-1])}")
            hits = 0
            for c in clusters:
                centre = statistics.median(t for t, _ in c)
                if any(abs(centre - lt) <= 0.5 for lt in log_times):
                    hits += 1
            print(f"  spikes with a serial line within +/-0.5 s: {hits}/{len(clusters)}")
            print("  -> low number = the wake source is below the application (stack/scheduler)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ccap = sub.add_parser("capture", help="capture current to CSV")
    ccap.add_argument("--seconds", type=float, default=60.0)
    ccap.add_argument("--out", default=None)
    ccap.add_argument("--port", default="/dev/ttyACM0")
    ccap.add_argument("--mode", choices=["smu", "amp"], default="smu")
    ccap.add_argument("--vout", type=int, default=3000)

    cana = sub.add_parser("analyze", help="analyse a capture CSV")
    cana.add_argument("capture")
    cana.add_argument("serial_log", nargs="?", default=None)

    args = ap.parse_args()
    if args.cmd == "capture":
        if args.out is None:
            args.out = f"capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        capture(args)
    else:
        analyze(args)


if __name__ == "__main__":
    main()
