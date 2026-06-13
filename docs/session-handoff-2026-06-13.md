# Session handoff — 2026-06-13 (putting it to bed)

Status at sign-off: **all green and stable.** Mac + GitHub + Micro git all at
`42916e7`, both work trees clean. All five services active (airband, ground,
scanner-digital-op25, scanner-reliability-watchdog, sdrplay).

This note is the pick-up point after stepping away. Read the **"If airband is
deaf when you come back"** section first — it's the most likely thing you'll hit.

---

## What got fixed this session

1. **Git reconciliation** — Mac main and the Micro's 73-commit divergence merged
   into one history; all three locations in sync. (commit `8f330f2`)

2. **The "dead as a doornail" outage** — the reliability watchdog was the
   saboteur. Its `safe_restart` STOPPED the live `gr-demod@*` units but tried to
   START legacy `rtl-airband-*` ghost units (stale names from the pre-chirp era),
   failing every cycle and leaving the bands dead. **Fixed by retiring the
   watchdog's band-management** — systemd's `Restart=on-failure` now owns band
   recovery; the watchdog only handles BT speaker + op25 now. (commit `50da606`)

3. **Two "unsolved mysteries" were measurement errors:**
   - "Cmd server listening on 7400 but nothing there" → the cmd bus is **UDP**
     (7400 airband / 7401 ground); I'd been checking with `ss -t` (TCP-only).
     It's healthy. Probe: `{"v":1,"id":"<string>","cmd":"get_status","args":{}}`.
   - "No RF to the analog RSPduo" → see #4 and #5; RF is fine, software wasn't.

4. **Airband sdrplay retune-wedge** (the real "no RF" cause) — after ~2h of
   runtime, retunes start failing with `sdrplay_api_Update(Tuner_FrF) Error:
   sdrplay_api_RfUpdateError`. The scan scheduler commands a new cluster, sdrplay
   rejects it, the front end freezes, and the band goes deaf to everything
   (verified: nothing opens even at -92 dBFS squelch). **Cleared by the safe
   restart sequence. IT RECURS** — see the recovery section below.

5. **Airband gain overload** (the chronic weak/flapping signal) — the assumption
   that airband needed *more* gain was **backwards**. It was OVERLOADED.
   Unfiltered AM airband + high front-end gain desensitizes the receiver.
   Live midday sweep at BNA:
   - gain 48 → 0 hits (deaf)
   - gain 32.8 (old default) → -74/-76 dBFS, ~2 hits/min, 1 dB squelch margin
   - **gain 20 (now) → -63/-67 dBFS, ~6 hits/min, 8-12 dB margin (clean)**

   Pinned via `systemd/gr-demod@airband.service.d/zz-gain.conf`
   (`CHIRP_SDR_GAIN_DB=20`). The full airband drop-in set is now captured in the
   repo (was Micro-only). (commit `42916e7`)

---

## If airband is deaf when you come back (MOST LIKELY ISSUE)

The retune-wedge (#4) recurs roughly every ~2h and **nothing auto-recovers it**
(we retired the watchdog's band-recovery; systemd doesn't catch a non-crash
wedge). So airband may well be silent on return. To confirm + fix:

```bash
# confirm the wedge: look for RfUpdateError
ssh ubuntu@micro.local 'journalctl -u gr-demod@airband --since "1h ago" | grep -c RfUpdateError'

# recover — the safe restart sequence (sudo pw also 1234):
ssh ubuntu@micro.local
sudo systemctl stop gr-demod@airband gr-demod@ground scanner-digital-op25
sudo systemctl reset-failed gr-demod@airband gr-demod@ground
sudo systemctl restart sdrplay.service
sleep 3
sudo systemctl start gr-demod@airband     # wait for active
sudo systemctl start gr-demod@ground
sudo systemctl start scanner-digital-op25

# verify hits return:
journalctl -u gr-demod@airband -f | grep hit_start
```

NEVER bounce the bands casually without the sdrplay restart + reset-failed — the
MA/SL release race wedges them. Always the full sequence above.

---

## Open items (priority order for next session)

1. **Recurring airband retune-wedge (#4)** — the #1 stability blocker. A restart
   is a band-aid. Likely root cause: MA/SL retune contention on the shared RSPduo
   (serial 1809063632) — airband (MA tuner 1) and ground (SL tuner 2) both retune
   their scan clusters independently and occasionally collide → RfUpdateError.
   This is the thing standing between "works when restarted" and "stable."

2. **Airband gain fine-tune** — gain 20 is a big win but maybe not optimal. The
   lo-clusters note cites ideal ATC at -50/-30 dBFS; we're at -64. Lower (15?)
   might help. Each test is a full restart, so the right tool is a **live
   `set_source_gain` cmd** (doesn't exist yet — `set_gain` on the cmd bus is
   per-channel audio trim, not the SDR front end). Build it, sweep 12→25 live.

3. **Ground gain** — same RSPduo, probably same overload. Untested (GMRS/MURS too
   quiet to measure). Give it the same gain cut and verify on a known-active VHF.

4. **Digital MTRTRS** — TACN locks clean (error ~12); MTRTRS shows persistent
   error ~-384, not clearly decoding TSBKs. May be weak signal or a CC config
   issue. TACN gives digital coverage in the meantime.

5. **UI "reset radios" button** — still routes through the same ghost-unit
   `safe_restart_rtl_airband` path (`ui/chirp_adapter.py` + `ui/config.py:355-358`
   UNITS names). Manual-click only, not a loop, but it's a live landmine. Fix:
   repoint UNITS from `rtl-airband-*` to `gr-demod@*`.

6. **Daemon shutdown hang** — bands take >10s on SDR teardown → SIGKILL
   (`TimeoutStopSec=10`). Ungraceful but no longer cascades. Bump it / fix the
   drain when convenient.

---

## Things deliberately NOT done (don't undo)

- Watchdog band-management is **retired on purpose**. Don't re-enable
  `WATCHDOG_RECOVER_CHIRP`. If you want auto-recovery for the wedge, build a
  *correct* one (detect RfUpdateError → safe restart with `gr-demod@` names) —
  don't resurrect the old path.
- Phase-1 source validator stays **OFF** (`CHIRP_SOURCE_VALIDATE` unset) — it
  head-stalls the flowgraph until it learns to disconnect post-capture.

## Key facts

- SSH: `ubuntu@micro.local`, password `1234` (sudo too). SSH can rate-limit on
  rapid reconnects — pause a few seconds and retry if you get "Permission denied".
- Airband RSPduo serial `1809063632` (MA tuner 1 = airband, SL tuner 2 = ground).
  Digital is a separate RSPduo.
- Cmd bus: UDP 7400 (airband) / 7401 (ground). op25 HTTP: 8080 / 8081.
  airband-ui: 5050.
- Live drop-ins live in `/etc/systemd/system/gr-demod@{airband,ground}.service.d/`
  on the Micro and are mirrored in the repo under `systemd/`.
