# Disco — multimon-ng paging decode (PR #32)

## What this adds

When disco detects a narrowband FSK signal in a **paging allocation**, it
pipes the captured IQ slice through [`multimon-ng`](https://github.com/EliasOenal/multimon-ng)
to decode **POCSAG / FLEX** page content. A decoded page carries a
**capcode** (recipient ID) and often message text — the
"hospital pager / utility SCADA / restaurant buzzer?" answer for the paging
band. Result is labelled `id_source="multimon"` at high confidence.

### Bands covered (`PAGING_RANGES_HZ`)

| Range | Use |
|-------|-----|
| 152.700–153.000 MHz | 152.84 MHz VHF paging |
| 158.600–158.800 MHz | 158.7 MHz VHF paging |
| 450.000–454.000 MHz | UHF paging |
| 929.000–932.000 MHz | 929–932 MHz paging |

### Decoders tried
`POCSAG512`, `POCSAG1200`, `POCSAG2400`, `FLEX_NEXT` (`-f alpha`).

## Where it sits in the trust hierarchy

```
0   rtl_433 device decode (classic-ISM)  → high   (PR #31)
0b  multimon-ng page decode (paging)     → high   ← THIS PR
A   HPDB → B CDBS → B2 rtl_433 fallback → B3 signature → C/D ULS → …
```

A decoded page is definitive content, so it wins ahead of HPDB/CDBS/ULS.
Paging and ISM bands don't overlap, so multimon never competes with the
classic-ISM rtl_433 priority step. multimon-ng is only invoked for
paging-band slices.

## Do-no-harm guarantees

Identical contract to `disco/src/rtl433.py`:

- `lookup_multimon()` **never raises** — binary-missing / slice-missing /
  timeout / no-decode / any-exception → `None`, and the classifier falls
  through to the existing chain unchanged.
- subprocess **stderr → append-mode file**, never a PIPE; stdout PIPE
  bounded by a 5 s timeout + the tiny slice.
- **No schema change** — `id_source='multimon'` reuses the `id_*` columns;
  counters are in-memory + a stats file.

## Kill switch

```bash
sudo systemctl edit disco-classifier
#   [Service]
#   Environment=DISCO_MULTIMON_ENABLED=0
sudo systemctl restart disco-classifier
```

With `DISCO_MULTIMON_ENABLED=0` the paging layer is skipped before any
subprocess spawns; the chain behaves exactly as PR #31. Independently gated
by binary availability, so a host without `multimon-ng` is a no-op.

| Env var | Default | Meaning |
|---------|---------|---------|
| `DISCO_MULTIMON_ENABLED` | `1` | kill switch |
| `DISCO_MULTIMON_BIN` | `multimon-ng` | binary name / path |
| `DISCO_MULTIMON_TIMEOUT_S` | `5.0` | per-invocation timeout |
| `DISCO_MULTIMON_STATS_PATH` | `/run/scannerproject/disco/multimon_stats.json` | counter file |
| `DISCO_MULTIMON_STDERR_LOG` | `/run/scannerproject/disco/multimon.stderr.log` | stderr sink |

## Verifying

```bash
curl -s http://localhost:8092/api/status | python3 -m json.tool   # multimon_* fields
journalctl -u disco-classifier -f | grep '\[multimon\]'
# DB:
sqlite3 disco/state/disco.sqlite \
  "SELECT freq_hz/1e6, id_service FROM detections WHERE id_source='multimon' LIMIT 10;"
```

`/api/status` fields: `multimon_available`, `multimon_enabled`,
`multimon_invocations_total`, `multimon_matches_total`,
`multimon_errors_total`, `multimon_last_match_capcode`,
`multimon_last_match_ts`, `multimon_matches_in_db`.

## Install

`multimon-ng` is in the Ubuntu repos:

```bash
sudo apt-get install -y multimon-ng
```

## Known limitation — IQ vs demodulated audio

**multimon-ng expects demodulated audio** (raw 16-bit signed mono, ~22050
Hz), **not complex IQ.** disco's slices are complex float32. The invocation
here follows the requested command shape, but until an **FM-demodulation
pre-stage** is added (IQ → audio, e.g. via an in-process numpy quadrature
demod or an `rtl_fm`/`sox` hop), most slices will not decode and
`lookup_multimon()` returns `None` (fail-open).

This is the direct analogue of rtl_433's slice-rate gap. If paging decode
yield proves persistently zero once deployed, the follow-up is to add the
demod pre-stage (preferred — keeps the slice-replay architecture) or a
dedicated paging-band audio feed. Tracked as future work.

## Rollback

1. `DISCO_MULTIMON_ENABLED=0` + restart classifier → reverts to PR #31.
2. `git revert <PR #32 squash>` + redeploy. No other subsystem touched.
