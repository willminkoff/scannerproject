# macos/sdrtrunk — working SDRTrunk config (reference copies)

These are **reference copies** of the SDRTrunk config that produced a live MTRTRS P25
control-channel decode on the Mac mini (2026-06-27). The live files SDRTrunk actually
reads are on the box, NOT here:

| Repo (reference) | Live on box (authoritative) |
|---|---|
| `tuner_configuration.json` | `~/SDRTrunk/configuration/tuner_configuration.json` |
| `mtrtrs-playlist.xml` | `~/SDRTrunk/playlist/default.xml` |

SDRTrunk **rewrites** the live files on exit — edit them only while SDRTrunk is stopped,
or your change is clobbered. After editing, re-copy here if you want the repo to track it.

## Why these are the way they are
- **`tuner_configuration.json` → `disabledTuners`** pins SDRTrunk to a single tuner:
  `RSPduo Tuner 1 SER#180903EF32`. SDRTrunk auto-opens *every* tuner by default; two
  concurrent dual-tuner RSPduos collapse the USB isochronous stream
  (`libusb submit_iso_transfer ... 0xe00002ee`). One single-tuner RSPduo streams fine.
- **`mtrtrs-playlist.xml` → `modulation="CQPSK"`** — MTRTRS is **simulcast**. With `C4FM`
  the decoder produces bits but only `SYNC LOSS`; `CQPSK` (LSM) syncs and decodes TSBKs.
- **No `preferred_tuner`** in the source config — let SDRTrunk pick the only enabled tuner.
- **`event_log_configuration`** (`DECODED_MESSAGE` + `CALL_EVENT`) is what makes decoded
  TSBKs land in `~/SDRTrunk/event_logs/` — SDRTrunk does NOT log decode to the app log.

MTRTRS control channels: 856.4875 / 856.7125 / 857.0375 / 857.4875 MHz. WACN `xBEE00`.

See `docs/macos-transition-memo.md` for the full picture.
