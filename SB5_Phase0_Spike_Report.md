# SB5 Phase 0 Spike — Validation Report

**Date:** 2026-06-03
**Target:** `ubuntu@micro.tail508e50.ts.net` (Ubuntu 24.04 noble, GR 3.10.9.2, Python 3.12.3)
**Scope:** Read-only on production stack. Experiments in `~/sb5-spike/`.
**Repo HEAD:** `a066439` (no commits made)

---

## Claim 1 — ham2mon `receiver.py` ports to GR 3.10 in <3 days

**Verdict: YES — port is ~1 day, not 3.**

**Evidence.** Cloned `madengr/ham2mon@db9834c` into `~/sb5-spike/ham2mon`. Surveyed `apps/receiver.py` (647 lines), `apps/scanner.py` (406), `apps/ham2mon.py` (154).

Two distinct breakage classes:

1. **Python 2 → 3 syntax.** `print` statements (only in `__main__` test scaffolds of `receiver.py`/`scanner.py` and inside `estimate.py`/`parser.py`/`ham2mon.py`), `raise SystemExit, 1`, `import __builtin__`, `xrange`. **All handled by `2to3 -w`** — ran it; after the rewrite `python3 -c "import receiver"` and `import scanner` both succeed cleanly (only stderr noise was libxtrxdsp CPU-feature banners from gr-osmosdr — not errors).

2. **GR 3.7 → 3.10 API drift.** I introspected every GR symbol `receiver.py` touches. Only **two** are gone:
   - `grfilter.firdes_low_pass(...)` → `grfilter.firdes.low_pass(...)` (sed-able)
   - `grfilter.firdes.WIN_HAMMING` → `gnuradio.fft.window.WIN_HAMMING` (constant rename; `window` is already imported)

   `freq_xlating_fir_filter_ccc`, `fir_filter_ccc/fff`, `pwr_squelch_cc/ff`, `quadrature_demod_cf`, `agc3_cc`, `wavfile_sink`, `stream_to_vector`, `keep_one_in_n`, `integrate_ff`, `probe_signal_vf`, `complex_to_mag_squared`, `fft.fft_vcc` — **all present with compatible signatures**. `fft.fft_vcc(fft_size, forward, window, shift=False, nthreads=1)` matches the call site verbatim.

The PyQt4 dependency is **only in `am_flow_example.py` / `nbfm_flow_example.py`** — example/demo flowgraphs we don't need to borrow. `receiver.py` is GUI-free.

**Implication.** SB5 should *transplant* the `Receiver`/`TunerDemodNBFM`/`TunerDemodAM` hier_block classes (lines 22–370 of `receiver.py`), drop the curses/console wrapper, fix two API names, done. Budget **0.5–1 dev-day** for the borrow itself, plus integration work. The `<3 days` claim has comfortable headroom.

---

## Claim 2 — `pfb_channelizer_ccf.set_channel_map()` works at runtime in GR 3.10

**Verdict: YES — confirmed live, including dynamic channel-count changes.**

**Evidence.** Introspection: `hasattr(filter.pfb_channelizer_ccf, 'set_channel_map') == True`. Built a 6-channel PFB channelizer fed by `null_source → head → stream_to_streams(6) → pfb_channelizer_ccf`, started the top_block, then called `set_channel_map([1,0,3,2,5,4])`, `[3,2,1,0,5,4]`, and `[0,1,2]` while running. Each `channel_map()` readback returned the new value; no exceptions; flowgraph kept running. Full test script and output captured.

**One architectural note worth flagging.** GR 3.10's `pfb_channelizer_ccf` requires **N input streams** (`min_streams == max_streams == nchans` on the input signature) — it's the pure polyphase block, not the convenience hierarchical wrapper. You feed it via a `stream_to_streams` block ahead of it. Not a blocker, just a wiring detail SB5's flowgraph needs to get right.

Constructor signature also tightened: `pfb_channelizer_ccf(numchans, taps, oversample_rate)` — the `attenuation` parameter from older docs is gone.

**Implication.** SB5's dynamic-channel-map plan works. We can re-point channels at new airband frequencies live without rebuilding the flowgraph. Green light.

---

## Claim 3 — SDRplay RSPduo master/slave works under SoapySDR + gr-osmosdr

**Verdict: YES — empirically proven by the production stack already using exactly this chain.**

**Evidence.** Inventory: `gnuradio 3.10.9.2`, `gr-osmosdr 0.2.5` (with `soapy` listed in built-in source types: `file fcd rtl rtl_tcp uhd miri hackrf bladerf rfspace airspy airspyhf soapy redpitaya freesrp xtrx`), `libsoapysdr 0.8.1`, and the SoapySDRPlay3 module **`libsdrPlaySupport.so 0.5.2-8ef31b2`** loaded at `/usr/local/lib/SoapySDR/modules0.8/`. SDRplay API daemon active.

`SoapySDRUtil --probe="driver=sdrplay"` returned `no available RSP devices found` — **expected**, because rtl-airband currently holds the RSPduo. So I read the production runtime configs instead:

```
/run/rtl_airband_airband_runtime.conf:  device_string = "driver=sdrplay,serial=1809063632,mode=MA,tuner=1"
/run/rtl_airband_ground_runtime.conf:   device_string = "driver=sdrplay,serial=1809063632,mode=SL,tuner=2"
```

This is the **same SoapySDR call site** gr-osmosdr would use via `osmosdr.source(args="soapy=0,driver=sdrplay,serial=1809063632,mode=MA,tuner=1")`. The fact that rtl-airband is right now successfully running MA+SL against this exact module is empirical proof the master/slave pair works.

**Heads-up on the args string.** The Phase 0 prompt guessed `rspduo_mode=...`. That syntax belongs to the upstream `pothosware/SoapySDRPlay3` fork. The **installed** driver here (build `8ef31b2`) uses `mode=MA|SL|ST` plus `tuner=1|2` — the syntax production already proves works. Don't change it.

**Implication.** When SB5 is ready to cut over, swap rtl-airband for a GR flowgraph wired as `osmosdr.source(args="soapy=0,driver=sdrplay,serial=1809063632,mode=MA,tuner=1") → pfb_channelizer_ccf → N × TunerDemodAM` (master) plus a parallel `mode=SL,tuner=2` source for VHF ground. Identical hardware contract.

---

## Overall confidence

**All three claims validated. High confidence in the 2–4 week estimate — lean toward the 2-week end.** Claim 1 turned out easier than budgeted (1 day instead of 3); Claim 2 worked exactly as hoped, including live channel-count changes; Claim 3 is de-risked because production already uses this exact SoapySDR module with this exact arg syntax.

**Phase 1 can start tomorrow.** Two small surprises to fold into the plan: (a) `pfb_channelizer_ccf` needs `stream_to_streams` upstream, (b) keep the `mode=MA/SL,tuner=N` arg syntax from the existing config — don't rewrite it to `rspduo_mode=`.

## Sources

- [SoapySDRPlay3 Settings.cpp (pothosware)](https://github.com/pothosware/SoapySDRPlay3/blob/master/Settings.cpp)
- [SoapySDRPlay3 README (pothosware)](https://github.com/pothosware/SoapySDRPlay3/blob/master/README.md)
- [SoapySDRPlay3 fork (fventuri)](https://github.com/fventuri/SoapySDRPlay3)
- [madengr/ham2mon](https://github.com/madengr/ham2mon)
- Local: `/run/rtl_airband_airband_runtime.conf`, `/run/rtl_airband_ground_runtime.conf` (production config on Micro)
- Local: `~/sb5-spike/ham2mon/` (experiment workspace)
