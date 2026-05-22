# Disco — dump1090 ADS-B decode (PR #33)

## What this adds

When disco's sweep captures activity at **1090 MHz** (ADS-B aircraft
downlink), it replays the IQ slice through [`dump1090`](https://github.com/flightaware/dump1090)
in static-file decode mode to extract **aircraft identification**: ICAO
24-bit hex address, flight/callsign, altitude (and speed/position when the
frame carries them). Result is labelled `id_source="dump1090"` at high
confidence.

ADS-B has no licensee/curated-DB equivalent, so a dump1090 decode is the
only way to put a name on a 1090 MHz signal.

### Band
`ADSB_FREQ_HZ = 1_090_000_000`, tolerance ±1 MHz (`is_adsb_band` → 1089–1091 MHz).

## Where it sits in the trust hierarchy

```
0   rtl_433 device decode (classic-ISM)  → high   (PR #31)
0b  multimon-ng page decode (paging)     → high   (PR #32)
0c  dump1090 ADS-B decode (1090 MHz)     → high   ← THIS PR
A   HPDB → B CDBS → … → C/D ULS → …
```

A decoded aircraft wins ahead of everything. 1090 MHz doesn't overlap the
ISM or paging bands, so the three specialist priority steps never compete.
dump1090 is only invoked for 1090 MHz ±1 MHz slices.

## Do-no-harm guarantees

Identical contract to `disco/src/rtl433.py`:

- `lookup_dump1090()` **never raises** — binary-missing / slice-missing /
  timeout / no-decode / any-exception → `None`, chain falls through
  unchanged.
- subprocess **stderr → append file**, never PIPE; stdout PIPE bounded by a
  5 s timeout.
- **No schema change** — `id_source='dump1090'` reuses `id_*` columns;
  counters in-memory + stats file.

## Kill switch

```bash
sudo systemctl edit disco-classifier
#   [Service]
#   Environment=DISCO_DUMP1090_ENABLED=0
sudo systemctl restart disco-classifier
```

| Env var | Default | Meaning |
|---------|---------|---------|
| `DISCO_DUMP1090_ENABLED` | `1` | kill switch |
| `DISCO_DUMP1090_BIN` | `dump1090` | binary name / path |
| `DISCO_DUMP1090_ARGS` | (empty) | extra args appended (fork differences) |
| `DISCO_DUMP1090_TIMEOUT_S` | `5.0` | per-invocation timeout |
| `DISCO_DUMP1090_STATS_PATH` | `/run/scannerproject/disco/dump1090_stats.json` | counter file |
| `DISCO_DUMP1090_STDERR_LOG` | `/run/scannerproject/disco/dump1090.stderr.log` | stderr sink |

## Verifying

```bash
curl -s http://localhost:8092/api/status | python3 -m json.tool   # dump1090_* fields
journalctl -u disco-classifier -f | grep '\[dump1090\]'
sqlite3 disco/state/disco.sqlite \
  "SELECT freq_hz/1e6, id_service FROM detections WHERE id_source='dump1090' LIMIT 10;"
```

## Install — binary availability

dump1090 is **not in the base Ubuntu repos** under the plain name. Options
(in order of preference):

```bash
# FlightAware build (best-maintained; provides /usr/bin/dump1090-fa):
sudo apt-get install -y dump1090-fa
#   then: export DISCO_DUMP1090_BIN=dump1090-fa

# or the older mutability fork (provides /usr/bin/dump1090-mutability):
sudo apt-get install -y dump1090-mutability
#   then: export DISCO_DUMP1090_BIN=dump1090-mutability

# or build from source: https://github.com/flightaware/dump1090
```

If none is installed, the module reports `dump1090_available: false` and the
layer is a transparent no-op — disco is unaffected. Set `DISCO_DUMP1090_BIN`
to whichever fork is present.

The exact file-decode flags differ slightly between forks; the module uses
`--ifile <slice> --iformat SC16 --quiet --no-interactive` and exposes
`DISCO_DUMP1090_ARGS` for any fork-specific overrides.

## Known limitation — bandwidth & sample rate

ADS-B is a **2 Mbit/s Manchester** signal that decoders sample at **~2.4
MHz**, and dump1090 expects 8-/16-bit complex IQ. disco's slices are
complex float32 **decimated to ~50 kHz** — far too narrow to carry an ADS-B
frame, and a format dump1090 doesn't natively read. So with the current
slice pipeline, decodes will be **rare or absent** and `lookup_dump1090()`
returns `None` (fail-open).

This is a harder gap than rtl_433's: ADS-B fundamentally needs a wideband
1090 MHz capture. The realistic path to real decodes is a **dedicated
wideband 1090 MHz feed** (e.g. a separate dongle running dump1090 live, or a
sweep mode that captures a ≥2.4 MHz slice at 1090). The module is shipped
now so the wiring + status plumbing is ready; decode yield awaits that feed.
Tracked as future work.

## Rollback

1. `DISCO_DUMP1090_ENABLED=0` + restart classifier → reverts to PR #32.
2. `git revert <PR #33 squash>` + redeploy. No other subsystem touched.
