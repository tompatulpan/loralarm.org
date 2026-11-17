# Bench checklist — connection supervision, rui3_sensor2

Work top to bottom and tick. Keep the serial logger running the whole time.
Expected log lines are given exactly — grep them in the log afterwards if in doubt.

**Automated:** `tests/bench/bench.py` runs the whole plan (stages A, B1, B2, C,
plus `monitor` for P2 and `flash` for A1/A10). See `tests/bench/README.md`.
Verify the harness itself with `python3 tests/bench/selfcheck.py` and
`python3 tests/bench/dryrun_stage_a.py` (both host-only, no hardware).
This checklist remains the manual fallback and explains what the automation
checks.

## Prepare (once, ~15 min)

- [x] **P1 — Node facts.** Write down: DevEUI, board (RAK3172/RAK4631-R),
      firmware commit (`git rev-parse HEAD`), bench supply voltage (must stay
      above the battery-critical threshold, or the boot guard/latch interferes).
- [x] **P2 — Logger on.** The bench harness timestamps every line itself
      (`logs/<run>/raw.log`); no manual logger needed at the PC. For a plain
      manual session without the harness, use `bench.py monitor`.

- [x] **P3 — Host tests.** `make -C devices/rui3_sensor2/tests/host` → `OK: 51 checks passed`.
- [x] **P4 — Both builds compile.** `arduino-cli compile` OK for RAK3172 (sketch.yaml)
      and RAK4631-R (`rak_rui:nrf52:WisCoreRAK4631Board`).
- [x] **P5 — Server side.** Device present in ChirpStack with the RUI3 codec in the
      profile, gateway online, and you know where to read the frame counter.
- [x] **P6 — SENDINT.** `ATC+SENDINT=300` set (custom command, ATC+ prefix —
      NOT `AT+SENDINT`, that is a different/native prefix and fails). The boot log must later say
      `Send interval found 300000`. If SENDINT is 0 no uplink ever happens and the
      link check silently never fires.

## Stage A — link check on the bench (~30 min, test build)

What this stage answers (the 2 things never verified on hardware):
**AS1** — does a confirmed uplink with no ACK report a non-zero send status?
**AS2** — does `api.lorawan.join()` actually run while the node still thinks it is joined?

- [x] **A1 — Flash the test build.** In `config.h` set ALL THREE test values:
      `REJOIN_CHECK_INTERVAL_MS 60000UL`, `REJOIN_RETRY_TICKS 10`, `LINK_CHECK_TICKS 10`
      (not just the last one). Build, flash.
- [x] **A2 — Boot in coverage.** Write down boot time T. Within ~5 min expect
      `LoRaWan OTAA - joined!`, then `Link policy: ADR=off, TXP=0 (~16 dBm EIRP), DR=3`
      and a first heartbeat with `Transmission successful - ready for sleep`.
      If not joined → check credentials/region in the log (`OTAA credentials missing`).
- [x] **A3 — Gateway OFF** any time before T+10 min. Nothing happens at first:
      a few heartbeats still log `Transmission successful`. That is correct —
      unconfirmed uplinks cannot notice the outage.
- [x] **A4 — Between T+10 and T+15 min** expect
      `[LINK] Link check due: next uplink goes out confirmed` followed by
      `Sending packet over LoRaWAN (DR=3 TXP=0 ADR=off, confirmed link check)`.
      (10 min is counted from boot T, not from the join.)
- [x] **A5 — The check fails.** Expect the stack's `+EVT:SEND_CONFIRMED_FAILED(n)`,
      then `[TX-CB] Transmission failed with status n`, then
      `[LINK] Link check FAILED (status n): no answer from the network — rejoin requested`.
      **Write down n.**
      **STOP if status is 0** → AS1 is wrong, the link check can never detect a dead
      network. Report; do not continue to Stage B/C.
- [x] **A6 — Seconds later** expect `[LINK] Forcing a fresh join (up to 255 attempts)`
      and `Join started — retry every 255 s, up to 255 attempts`.
      **STOP if** you instead see `join() returned false` and no join attempts after it
      → AS2 is wrong, the forced rejoin is dead code. Report.
- [x] **A7 — Keep the gateway off ~5 min.** Expect `[JOIN-CB] ... join fail!`
      (+ `+EVT:JOIN_FAILED_RX_TIMEOUT`) about every 4.5 min, and every 60 s
      `Not joined, skip sending` (battery-cadence heartbeat attempts).
- [x] **A8 — Around T+21 min** expect one line
      `Still not joined after 10 min — starting a new join series`.
      Expected in the test build; the same line with production values would be a bug.
- [x] **A9 — Gateway ON.** Within ~4.5 min (one join interval): `joined!`,
      the link-policy line, `Send timer started: 300000 ms`, a heartbeat
      `Transmission successful`, and frames in ChirpStack again.
- [x] **A10 — Restore production values** in `config.h`
      (`REJOIN_CHECK_INTERVAL_MS 3600000UL`, `REJOIN_RETRY_TICKS 24`,
      `LINK_CHECK_TICKS 24`), rebuild. Verify against the "PRODUCTION (current)"
      comments before building Stage B/C.

## Stage B — production build, quick checks (~40 min)

### B1 — power-cycle in coverage (~10 min)

- [x] **B1.1** Flash the production build, gateway on, power-cycle the node.
      Boot guard logs `Boot battery: x.xxV` and passes.
- [x] **B1.2** One of two boot outcomes, both fine — write down which:
      - `Join started — retry every 255 s, up to 255 attempts` → `joined!` within ~4.5 min, or
      - `Already joined (cached session) — skipping join setup` (the stack kept the session).
- [x] **B1.3** In both cases the link policy must appear:
      `Link policy: ADR=off, TXP=0 (~16 dBm EIRP), DR=3`
      (cached case: it comes right after
      `Already joined — starting send timer and triggering first transmission`).
- [x] **B1.4** First heartbeat `Transmission successful` within ~1 min. No MAC ACK is
      expected (`AT+CFM` is 0 by default; set `AT+CFM=1` first if you want a real ACK).
- [x] **B1.5** ChirpStack: frame counters **continue** from before the reboot
      (no reset to 0) → session restore works. Write down the first FCnt after reboot.

### B2 — power-cycle out of coverage (~25 min)

Either outcome is a valid result — this answers whether the RUI3 session cache
survives a power cycle, i.e. which README scenario is true for this BSP.

- [x] **B2.1** Gateway OFF (or node moved out of range), power-cycle the node.
- [x] **B2.2** Read the boot line, then follow the matching bullet:
      - **Outcome U** — `Join started — ...`: join failures about every 4.5 min,
        heartbeats log `Not joined, skip sending`. Nothing reached ChirpStack.
        Move in range → `joined!` within ~4.5 min, heartbeats resume.
      - **Outcome C** — `Already joined (cached session)`: NO join attempts at all;
        heartbeats keep logging `Transmission successful` into the void. The node
        recovers on its own the moment coverage returns (next heartbeat reaches the
        server); a *server-side* dead session is only caught by the daily link check.
      - **Third boot line (bench-verified on RAK3172, 2026-11-16)** —
        `join() returned false — cached session restore in progress or stack busy`:
        the stack was busy restoring/joining in the background at boot, so neither
        line above prints. What follows decides U or C: `joined!` within seconds =
        it (re)joined; `join fail! Next retry in 255 s (auto-rejoin active)` = it
        went to the air — treat as U (the stack retries on its own; heartbeats skip,
        fCnt resets at the eventual join).
- [x] **B2.3** Write down U or C and the exact boot line.

## Stage C — 25 h soak on the production timers

- [ ] **C0** Snooze the server's stale/battery alarms for this node — the planned
      outage at +23 h would otherwise fire them and spam the phones.
- [ ] **C1 — T0.** Note the exact reboot time, reboot in coverage, logger running.
      Verify heartbeats arrive in ChirpStack every 5 min.
- [ ] **C2 — T0+23:00. Gateway OFF.** The log keeps showing `Transmission successful`
      (unconfirmed — expected); ChirpStack goes silent. Nothing else happens yet.
- [ ] **C3 — around T0+24:00** expect, without touching anything:
      `Link check due: next uplink goes out confirmed` → confirmed heartbeat →
      `Link check FAILED (status n)` (same n as Stage A) →
      `Forcing a fresh join (up to 255 attempts)` → join failures every ~4.5 min.
      Shifts later if the node joined later than boot (24 *joined* ticks) — in B/C
      that could be up to ~5 min.
- [ ] **C4 — T0+24:30. Gateway ON.** Within ~4.5 min: `joined!` → link-policy line →
      first heartbeat. ChirpStack starts a NEW session here, so the frame counter
      resets — expected at a rejoin, do not confuse with B1.5.
- [ ] **C5** Write down the total recovery time, gateway-on → first frame in
      ChirpStack (should be ≤ ~10 min). Stage C done.

### C-opt — 24 h+ extra: daily retry after an unjoined boot (optional)

Needs a node that boots unjoined (Stage B2 outcome U; if your node boots C,
re-provision the device first — see B2).

- [ ] **C-opt.1** Gateway off from boot: `Join started — retry every 255 s, up to 255 attempts`.
- [ ] **C-opt.2** 0–18 h: join failures ~every 4.5 min. 18–24 h: radio silent (series exhausted).
- [ ] **C-opt.3** Around T0+24:00: `Still not joined after 1440 min — starting a new join
      series`, then 5 attempts (~21 min), then silent again.
- [ ] **C-opt.4** Leave it 48 h to see the series repeat daily (every 24 h).

This measures the real reconnect gap: up to ~6 h after the initial 18 h series,
then ~23 h between daily series. Too slow → tune `REJOIN_RETRY_ATTEMPTS`
(longer daily series) or `REJOIN_RETRY_TICKS` (shorter period), rerun C-opt.

## Notes — fill in as you go

```
Stage A:  date 2026-11-16  commit 718b0e5+ (working tree; production values restored before B)
          status n in A5 = 4   (+EVT:SEND_CONFIRMED_FAILED(4))
          A6 join attempts appeared? yes      recovery time after gateway-on = 178 s
Stage B:  B1.2 boot outcome = third line: "[SETUP] join() returned false — cached session
          restore in progress or stack busy", joined ~5 s later
          first FCnt after reboot = 0  (B1.5: fCnt 3 -> 0, counter RESET)
Stage B:  B2 outcome = U   boot line: join() refused, then "[JOIN-CB] ... join fail!
          Next retry in 255 s (auto-rejoin active)" — the stack retries on its own
Stage B:  node = sensor15, DevEUI AC1F09FFFE12D9A3, RAK3172 EVB (USB powered, ~3.3 V)
Stage C:  T0 = __________  link check due at __________  FAILED at __________
          rejoined at __________  gateway was on at __________  recovery = ____ min
          FCnt reset seen in ChirpStack? yes/no
C-opt:    daily series seen at 24 h? yes/no   at 48 h? yes/no
Log files: tests/bench/logs/20261116_120339 (A), _185433 (B1), _191030 (B2);
          _174822/_181835/_184317 = aborted runs while fixing the harness scan window
          (bug: boot-outcome lines print before "Send interval found", expect_any
          needs since=t_send — fixed 2026-11-16)
Deviations: the RUI3 session cache does NOT survive a reset on this BSP — every
          reboot does a silent fresh join (fCnt reset). No "cached session" boot
          (outcome C) has ever been observed on this board.
```

**Stop-and-report cases** (do not fix at the bench, just report with the log):
A5 status 0 → the link check never detects a dead network (AS1 wrong).
A6 `join() returned false` with no attempts → the forced rejoin never runs (AS2 wrong).
Neither case occurred: A5 status was 4, A6 join attempts appeared (both verified
2026-11-16).
