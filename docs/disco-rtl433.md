# Disco — rtl_433 ISM-device identification (PR #30)

## What this adds

Disco's identification chain (HPDB → CDBS → signature → ULS → ML class) is
good at licensed and broadcast services but blind to the dense world of
**ISM-band consumer/industrial devices**: wireless weather stations, TPMS
tire sensors, doorbells, remote thermometers, energy/utility meters, soil
sensors, garage remotes, and so on.

[`rtl_433`](https://github.com/merbanan/rtl_433) is a mature decoder for
**250+ such device protocols**. PR #30 wires it in as a specialist layer:
when disco sees a detection in an ISM band and the curated databases miss,
it replays the captured IQ slice through `rtl_433` and, if a device packet
decodes, labels the detection with the device name + id at **high
confidence** (`id_source = "rtl_433"`).

## Architecture — Option 3 (IQ slice replay)

Disco's sweep already saves short IQ slices to
`/run/scannerproject/disco/slices/`. The classifier, after it fails to
match HPDB/CDBS for an ISM-band detection, runs:

```
rtl_433 -s <sample_rate> -r cf32:<slice_path> -F json -A
```

Slices are interleaved complex float32 (`.iq.f32`), which `rtl_433` reads as
`cf32`; the sample rate is parsed from the slice filename. The top decoded
packet becomes the identification.

This is **Option 3** of the three integration options considered. **Option 1
(a dedicated rtl_433 dongle on its own SDR)** is the documented fallback if
replay yield proves too low (see *Known limitations*).

### Bands covered

| Band | Range | Typical devices |
|------|-------|-----------------|
| 315 MHz | 314–316 MHz | TPMS, garage/gate remotes, NA sensors |
| 433 MHz | 433–435 MHz | weather stations, doorbells, thermometers |
| 868 MHz | 863–870 MHz | EU SRD sensors / meters |
| 915 MHz | 902–928 MHz | NA ISM — meters, sensors, telemetry |

### Where it sits in the trust hierarchy

```
A   HPDB exact-freq match        → high      (curated)
B   CDBS exact-freq match        → high      (curated)
B2  rtl_433 device decode (ISM)  → high      ← NEW
B3  spectral signature match     → high/med
C/D ULS licensee                 → medium
E   ML class in band             → low
F/G band-rejected / NOISE        → spurious
```

rtl_433 outranks the generic spectral signature and ULS (a decoded packet
is a definitive protocol-level ID) but **defers to the curated HPDB/CDBS
databases**. It is only invoked when HPDB **and** CDBS both miss **and** the
frequency is in an ISM band — so the subprocess never runs where it can't
help.

## Do-no-harm guarantees

This integration cannot break disco:

- `disco/src/rtl433.py` **never raises into the caller**. Every failure mode
  (binary missing, slice missing, subprocess timeout, malformed/empty
  output, any exception) returns `None`, and the classifier falls through to
  the signature layer exactly as before PR #30.
- The classifier imports the module under a non-fatal guard and wraps the
  lookup call in its own `try/except` as defense in depth.
- subprocess **stderr is redirected to a file** (`rtl433.stderr.log`), never
  a PIPE — avoiding the unbounded-PIPE memory blowup pattern. stdout (small
  line-delimited JSON) uses a PIPE bounded by a 5 s timeout and the tiny
  2048-sample slice.
- **No schema change.** `id_source='rtl_433'` reuses the existing `id_*`
  columns; counters live in memory + a stats file.

## Kill switch

A single environment variable disables the layer entirely:

```bash
# disco-classifier uses an inline systemd Environment=, so add a drop-in:
sudo systemctl edit disco-classifier
# In the editor, add:
#   [Service]
#   Environment=DISCO_RTL433_ENABLED=0
sudo systemctl restart disco-classifier
```

With `DISCO_RTL433_ENABLED=0` the classifier behaves **identically to
PR #29** — the rtl_433 lookup is skipped before any subprocess is spawned.
The layer is also independently gated by binary availability, so a host
without `rtl_433` on `PATH` is already a no-op.

Other env knobs (all optional):

| Var | Default | Meaning |
|-----|---------|---------|
| `DISCO_RTL433_ENABLED` | `1` | kill switch (0/false/no/off disables) |
| `DISCO_RTL433_BIN` | `rtl_433` | binary name / path |
| `DISCO_RTL433_TIMEOUT_S` | `5.0` | per-invocation subprocess timeout |
| `DISCO_RTL433_STATS_PATH` | `/run/scannerproject/disco/rtl433_stats.json` | counter file the dashboard reads |
| `DISCO_RTL433_STDERR_LOG` | `/run/scannerproject/disco/rtl433.stderr.log` | rtl_433 stderr sink |

## Verifying it's running

**`/api/status`** (dashboard, port 8092) surfaces live counters:

```bash
curl -s http://localhost:8092/api/status | python3 -m json.tool
```

```json
{
  "rtl433_available": true,
  "rtl433_enabled": true,
  "rtl433_invocations_total": 142,
  "rtl433_matches_total": 7,
  "rtl433_errors_total": 0,
  "rtl433_last_match_ts": 1779414100.5,
  "rtl433_last_match_service": "Acurite-Tower (id 1234) chA",
  "rtl433_matches_in_db": 7
}
```

**journal** — every invocation logs one line:

```bash
journalctl -u disco-classifier -f | grep '\[rtl_433\]'
# [rtl_433] freq=433.9200 slice=B-T2_433920000_..._.iq.f32 result=match:Acurite-Tower (id 1234) chA
# [rtl_433] freq=915.0000 slice=...  result=no-match
# [rtl_433] freq=433.9200 slice=...  result=error:timeout-5.0s
```

**DB** — authoritative match rows:

```sql
SELECT freq_hz/1e6, id_service, id_evidence_json
FROM detections WHERE id_source = 'rtl_433' ORDER BY ts DESC LIMIT 10;
```

## Known limitations / future work

- **Sample-rate mismatch.** Most OOK ISM devices are designed to be sampled
  at ~250 kHz; disco's slices are decimated to ~50 kHz (the slice rate
  floor). Many narrow OOK packets still decode, but some devices — and most
  wideband FSK protocols — will not be recoverable from replay. This is the
  primary reason replay yield may be lower than a live capture.
- **Short slices.** Slices are ~2048 samples (tens of ms). A device whose
  packet didn't fall inside the captured window won't decode, even if it's
  active on that frequency.
- **If yield is persistently low**, the fallback is **Option 1: a dedicated
  rtl_433 dongle** running `rtl_433` live on an ISM band with its own SDR,
  publishing decodes that disco ingests — no replay, native sample rate.
  That's a larger change (extra hardware + a new ingest path) and is
  deliberately deferred until the replay data shows it's needed.

## Rollback

1. **Immediate disable:** `DISCO_RTL433_ENABLED=0` + restart classifier
   (see *Kill switch*). Disco reverts to PR #29 behavior.
2. **Full revert:** `git revert <PR #30 squash sha>` + redeploy + restart
   `disco-classifier`. No other subsystem is touched — the classifier
   without rtl_433 is byte-for-byte the PR #29 identification chain.
