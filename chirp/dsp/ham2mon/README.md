# chirp/dsp/ham2mon — vendored DSP from ham2mon

**Upstream:** [github.com/madengr/ham2mon](https://github.com/madengr/ham2mon)
**Vendored at SHA:** `db9834ce923c1919602bf33cb47720daba9bc6ea` (2018-10-27)
**License:** GPL (see `./LICENSE`).

This directory vendors `apps/receiver.py` and `apps/scanner.py` from ham2mon, ported
from GNU Radio 3.7 to GNU Radio 3.10. The originals live in `~/sb5-spike/ham2mon/`
on Micro (Phase 0 spike scratch).

## Why vendored, not submoduled

Per design doc Section 3: ham2mon is lightly maintained (last commit Oct 2018), the
GR 3.10 port will diverge over time, and we want one repo to bisect.

## Port summary (2026-06-03)

- `2to3 -w` for Python 2 → Python 3 (print-as-function only — no other Python-2 idioms
  in these files).
- `grfilter.firdes_low_pass(...)` → `grfilter.firdes.low_pass(...)` (6 sites in receiver.py).
- `grfilter.firdes.WIN_HAMMING` → `window.WIN_HAMMING` (6 sites; `window` is imported
  from `gnuradio.fft`, which the original already did).
- `scanner.py`: top-of-file `import receiver / import estimate / import parser` rewritten
  for package layout (`chirp.dsp.ham2mon`). `estimate` and `parser` are **not** vendored
  in this commit (scaffold brief specified only `receiver.py` + `scanner.py`); they are
  stubbed to `None` so the module imports cleanly. Any function in `scanner.py` that
  touches them will raise `AttributeError` until Phase 1 vendors them.

## What's here vs. what's NOT

| ham2mon file | Vendored here? | Notes |
|---|---|---|
| `apps/receiver.py` | Yes (ported)  | TunerDemodNBFM + TunerDemodAM hier_blocks. The chirp design uses these as the per-channel demod pattern. |
| `apps/scanner.py`  | Yes (ported)  | Scanner control loop. Import-clean; some code paths need estimate/parser vendored before they run. |
| `apps/estimate.py` | **No** (deferred) | Spectrum-bin estimator. Phase 1 work. |
| `apps/parser.py`   | **No** (deferred) | CLI option parser. We use the JSON command bus, so this may stay un-vendored. |
| `apps/cursesgui.py`| **No** (never)| Curses TUI; chirp has no TUI (we use the airband-ui dashboard via UDP JSON). |
| `apps/ham2mon.py`  | **No** (never)| ham2mon's main entry; chirp has its own `chirp.daemon`. |
| `apps/am_flow_example.py`, `nbfm_flow_example.py` | **No** | GRC-exported demos; replaced by chirp.flowgraph. |

## License inheritance

ham2mon is **GPL**. By vendoring its code, the chirp project is GPL-licensed in
turn. See repo-root `chirp/PROGRESS.md` — flagged for Will to acknowledge.

## Smoke test

From repo root:

```
python3 -c 'from chirp.dsp.ham2mon import receiver, scanner; print("OK")'
```

If this prints `OK`, the port is healthy at the import-graph level. Actual flowgraph
construction requires `gnuradio`, `gr-osmosdr`, etc. — exercised by Phase 1's
`test_flowgraph_smoke.py`.
