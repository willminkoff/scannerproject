# Digital (OP25) — open questions for tomorrow

State at end of 2026-05-26 session: **analog working, digital not**.
The MA/SL split-process architecture for analog is sound and shipped
(`f5d3c24`).  Digital coexistence with that architecture failed
repeatedly tonight in ways that need a real diagnostic plan, not
more daemon-hammering.

## What we proved

1. **OP25 CAN work in this architecture** — at cutover time it
   locked NJICS Site 1.9, decoded tsbks at hundreds/sec, voice
   grants flowing.  ~30 minutes of clean operation.

2. **Every subsequent OP25 restart wedged at `sdrplay_api_Open`.**
   `multi_rx.py`'s main thread was in `clock_nanosleep` waiting on
   the sdrplay daemon's response semaphore (`/dev/shm/Glbl\sdrSrvRespSema`).
   The daemon's command queue stops servicing new opens but
   continues streaming samples to existing clients (analog kept
   working through these wedges).

3. **Hard-killing the daemon and restarting it doesn't reliably
   unwedge it** — confirmed by `pgrep -f /opt/sdrplay_api/sdrplay_apiService`
   showing the same PID surviving multiple `sudo kill -9` and
   `systemctl restart sdrplay`.  Either systemd respawned with the
   same PID by coincidence, or my kills failed silently due to SSH
   connection drops.  Either way, the wedge persists.

4. **Only a full system reboot reliably clears the wedge.**  After
   reboot, OP25 starts and can lock.  Until something cycles it
   again, it stays working.

## Hypothesis to test tomorrow

The wedge appears to be triggered by **concurrent `sdrplay_api_Open`
calls from different services to the same daemon**.  Evidence:

- `_rspduo_launch_gap_sec()` in `scripts/ensure-op25-runtime.py`
  documents this exact problem: "the SDRplay user-space daemon does
  not safely serialize concurrent `sdrplay_api_Open` calls from
  different processes — even when the calls target different
  physical RSPduos."
- The cutover script starts services with explicit waits between
  them (Step 10) — and that worked.
- Boot-time systemd starts them all roughly concurrently — and
  that fails (boot race).
- Every restart we did mid-day was uncoordinated with the running
  rtl-airband services — and most of them wedged.

## What to try, in order

1. **Boot-race fix: ground unit needs a samples-flow gate, not
   just unit-active gate.**  Add `ExecStartPre=/bin/bash -c 'for
   i in {1..30}; do [ -f /run/rtl_airband_airband_stats.txt ] &&
   [ "$(( $(date +%s) - $(stat -c %Y /run/rtl_airband_airband_stats.txt) ))"
   -lt 10 ] && break; sleep 1; done'` to `rtl-airband-ground.service`.
   Slave open will then only happen after Master is producing
   samples.  Same idea for OP25 unit if needed.

2. **OP25 unit needs `After=rtl-airband-airband.service` + a
   samples-flow gate against the airband stats file**.  Sequence
   on boot: airband Master comes up first, ground Slave waits for
   airband samples, OP25 waits for ground samples.  Each open is
   serialized via the gates.

3. **OP25 restart from the UI should also bring the daemon to a
   known-clean state.**  The `restart_digital()` machinery already
   does this for OP25's own daemon coordination, but didn't
   anticipate analog services being on the same daemon.  Extend it
   to coordinate with rtl-airband-* the way `restart_rtl_airband()`
   coordinates with op25.

4. **If 1-3 don't crack it, try systemd `OnFailure=` to chain
   automatic full-stack recovery** — `OnFailure=sdr-stack-recover.target`
   that does the proven manual sequence (stop all, kill daemon,
   wait, restart daemon, start in order).

5. **Last resort: confirm via `strace`/`ltrace`** what exactly the
   daemon is doing during the wedge.  Is it deadlocked on its own
   mutex?  Is the kernel waiting on USB?  Is the firmware hung?
   This would let us file an upstream issue with SDRplay if it's
   a driver bug.

## What NOT to do

- **Don't hammer the daemon with repeated restart cycles.**  Each
  cycle tonight made things WORSE, not better.  If a single
  restart attempt doesn't recover the daemon, REBOOT — don't loop.
- **Don't try to fix digital with analog still running** unless
  you have a coordinated stop/start plan.  The daemon shares
  state between consumers and gets corrupted by mid-stream
  concurrent open calls.
- **Don't change OP25's RFGR gain without isolating one variable
  at a time.**  Tonight we changed RFGR:4 → RFGR:1, restarted,
  found OP25 still couldn't lock — then changed back to RFGR:4
  and the daemon wedged.  The gain change may have been correct
  but we'll never know because we conflated it with the daemon
  cycle.

## State to expect when resuming

- Repo on `main` at `f5d3c24` — full MA/SL split architecture + auto-
  dispatch fix.
- Micro likely needs another reboot to clear whatever wedge state
  this session ended in.
- After reboot: analog should come up via boot-chain (with the
  known boot-race issue affecting ground until you reset-failed +
  start manually).
- OP25 will either lock cleanly (if antenna still good) or sit
  scanning without decoding (need to investigate antenna position).
