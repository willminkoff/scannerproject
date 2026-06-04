# chirp/tests/fixtures

Placeholder directory for the rtl-airband software-bug regression fixtures.
These land in Phase 2. See `SDR_DEMOD_DESIGN_2026-06-03.md` Section 10 for the full
list and rationale.

## Targeted bugs (in scope)

1. **Squelch poison value** — stats file occasionally writes garbage noise-floor
   numbers; SB5 squelch presets consume them verbatim and produce nonsense thresholds.
   Fixture: stats-shaped input with poison values. Test: chirp rejects/clamps.
2. **SDRplay master/slave wedge on restart** — rtl-airband SIGKILL leaves shared-memory
   semaphores in a state where the next process can't claim master or slave; opens wedge.
   Fixture: simulated wedged semaphore state. Test: source open detects + recovers,
   surfaces clean EHWFAIL.
3. **libshout drop without reconnect** — rtl-airband drops Icecast on transient network
   issues and never reconnects without a process restart.
   Fixture: mock Icecast that drops mid-stream. Test: chirp's audio sink reconnects
   with backoff and resumes within N seconds.
4. **Noise-floor init race** — per-channel noise-floor estimator inits from zero and
   takes seconds to converge; first-window squelch decisions are wrong.
   Fixture: fresh start with IQ file source. Test: no spurious `hit_start` in the
   first second.

## Out of scope

- **USB dongle physical flap.** Hardware/kernel-level event. Chirp inherits whatever
  resilience SoapySDR + osmocom_source provide; we test we surface a clean error and
  let systemd restart us, but we do not fixture the hardware flap itself.
