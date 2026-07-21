# macOS backend (`macos/`)

Scaffolding for the **macOS migration** of ScannerBox — see the decision/scope in
[`docs/macos-backend-migration-scope.md`](../docs/macos-backend-migration-scope.md).

Backend on macOS = **SDRangel (analog)** + **SDRTrunk (digital P25)** + the
**SB3 web UI** (`sb3/ui/`, served by the `sb3-ui` agent), with **two
mobile-control paths**: the web UI and **conversational control via Claude**
(SSH-drives-the-box, same as Linux today).

> **Status: SCAFFOLDING.** Skeletons + working stubs, written before macOS was up.
> Real implementation iterates against running SDRangel + SDRTrunk after Phase 1
> (external-media validation) proves the stack. Nothing here is wired/tested live yet.

## Where to go for what
| Path | What |
|---|---|
| `install/bringup.md` | Ordered checklist to bring a fresh macOS install to a working scanner |
| `install/jmbe-build.sh` | Compile the JMBE voice library SDRTrunk needs |
| `install/post-install-checks.sh` | Verify SDRplay API, RSPduo, JMBE, REST :8091 |
| `launchd/*.plist` | Auto-start templates (SDRangel, SDRTrunk, icecast, audio bridges) |
| `clients/sdrangel_client.py` | SDRangel REST (:8091) wrapper — library + CLI |
| `clients/sdrtrunk_client.py` | SDRTrunk integration scaffolding (limited control surface — see notes) |
| `data/hpdb_to_sdrangel.py` | HPDB SQLite → SDRangel channel CSV |
| `data/hpdb_to_sdrtrunk.py` | HPDB SQLite → SDRTrunk playlist XML |

> The mobile-first web UI now lives in `sb3/ui/` (the `sb3-ui` agent), which
> superseded the earlier `scannerctl/` Flask skeleton (retired, never installed).

## Device/role mapping (from `etc/mac/sdr_fleet_policy.json`)
- **RSP `180903EF32`** → SDRTrunk → MTRTRS + TACN (P25 Phase II). ✅ This is
  **Neptune's** RSPduo, measured on the bus 2026-07-16.
- **RSP `1809063632`** → SDRangel → AM airband + NFM ground. ⚠️ This line
  describes **Venus**, a different host — not this box. The two RSPduos have
  never shared a host in the current arrangement, and they must not (two RSPs
  through one `sdrplay_apiService` is the 0x6bed hazard).
- Shared SDRplay apiService (`com.sdrplay.service`); **max 1 dual-tuner RSPduo**.

> **Note:** the serial→role intent above was always right; only fleet policy
> rev 4.1 got the host assignment backwards. See rev 5.0 and
> `docs/sb3-neptune-architecture.md` §7.5.

## Data carried over from SB6 (Linux)
`homepatrol.db` (52 MB) + `hp_state.json` were backed up off the Mini before the
wipe (`~/Downloads/sb6-data-backup-2026-06-26.tar.gz`). The `data/` converters
turn them into SDRangel CSV + SDRTrunk XML.
