# Smoke tests

Operator-run reliability checks. These are **not** part of CI — several of them
bounce live backends and drop audio, so they are run by hand when nobody is
relying on uninterrupted listening.

## `smoke_favorite_switch.py` — favorite-switch reliability

Exercises the full favorite-switch path (rtl-airband + op25 reconfigure) and
reports green/red per check: rtl freqs updated, op25 systems updated, no service
failed, hits flow on a known-active frequency, then switches back to the
original favorite.

> ⚠️ **Switching favorites bounces rtl-airband and op25 — audio drops for a few
> seconds.** The script never auto-executes: by default it runs read-only, and
> `--execute` still requires typing a confirmation phrase at the prompt.

```bash
# read-only (safe): shows current favorite + the plan, makes no changes
python3 scripts/smoke_favorite_switch.py

# full run (bounces backends — prompts for confirmation first)
python3 scripts/smoke_favorite_switch.py --execute
```

Useful env overrides: `SMOKE_KNOWN_ACTIVE_FREQ` (default `172.8120`, Ground
Control — set to a freq known active at the current site), `SMOKE_UI_BASE`,
`SMOKE_RECONFIGURE_WAIT_SEC`, `SMOKE_HITS_WAIT_SEC`.

**Run this when ready to verify favorite-switch reliability** — i.e. during a
maintenance window, not while someone is actively listening.
