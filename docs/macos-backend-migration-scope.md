# macOS Backend Migration — Scope

**Status: SCOPED, NOT STARTED (2026-06-26).** Decision captured for clean revisit. No work executed.

## Decision summary
- Move the Mac mini (host `ScannerBox`, 2018 Intel/T2) from **Ubuntu → macOS**.
- Backend becomes: **SDRangel (analog) + SDRTrunk (digital P25) + a thin custom web UI** on top (native UIs for deep config; custom panel for mobile control).
- **Motivator: thermal.** Linux cannot drive the T2 SMC fan (`applesmc` fails `-5` on T2); macOS controls it natively. The chirp/op25 DSP load pinned the CPU to **100°C** today, forcing a full stop of chirp+op25 to recover.

## What carries over
- Scanner repo + full git history + tag **`sb6-baseline`** on GitHub (the Linux era stays as reference).
- **HPDB** SQLite (`homepatrol.db`) + `hp_state.json` favorites — but need **conversion to SDRangel/SDRTrunk CSV/XML** formats.
- **TACN RadioReference archive** (`etc/scannerbox/scannerproject/digital/profiles/tacn/`) — reference data (145 sites, control channels, talkgroups); useful for any decoder.
- **BOOM speaker** — BlueZ/PipeWire on Linux today → **CoreAudio** on macOS (simpler). Never change BOOM volume directly (standing rule).
- **Tailscale + SSH** access pattern.
- **Dispatch / remote-desktop** model — via macOS **Screen Sharing** or a CRD-mac equivalent (Linux side was CRD→XFCE).
- **Claude/Dispatch integration** (same access pattern as today) — Will tells Claude what he wants while mobile; Claude drives the box on his behalf. Requires preserving through the OS swap:
  - **SSH access from the Claude environment** to the macOS Mac mini (Tailscale + SSH key auth, same model as `ubuntu@scannerbox` today).
  - **All third-party APIs reachable via shell/curl** from Claude's task sessions: SDRangel REST on :8091, SDRTrunk hooks, system commands (`launchctl`, logs).
  - Whatever Claude needs to drive things programmatically must be exposed (no GUI-only choke points).

## What gets replaced (lost or migrated)
| Linux component | Fate |
|---|---|
| chirp daemons + cmd-bus | → **SDRangel** (Qt app + REST API on :8091) |
| `scanner-digital-op25` (multi_rx) | → **SDRTrunk** (JMBE lib built — one-time setup) |
| `airband-ui` (Flask, :5050) | → SDRTrunk + SDRangel **native UIs** for deep config; **thin custom web UI** for mobile control |
| `scanner-vlc-*` | → SDRTrunk built-in **icecast broadcaster** + SDRangel **CoreAudio** out |
| `icecast2` multi-mount | → SDRTrunk broadcaster (digital) + SDRangel→icecast (analog) |
| systemd drop-ins (gain-inversion guard, `--gain 0.7` BOOM cap, ExecStartPost favorites, `+ln` profile, HPDB path) | → **launchd plists** / macOS equivalents |
| Linux-specific fixes (linger, XFCE-for-CRD, `apple-firmware` WiFi) | → **not needed** on macOS |

## Architecture mapping
| Today (Linux SB6) | Tomorrow (macOS) |
|---|---|
| chirp@airband + @ground | SDRangel — Frequency Scanner channel (AM `R0:0` / NFM `R0:1`) |
| scanner-digital-op25 | SDRTrunk (P25 Phase 1 CC + Phase 2 TDMA voice following) |
| scanner-vfo | SDRangel (separate device-set or instance) |
| airband-ui (:5050) | Thin custom web UI on SDRangel REST + SDRTrunk hooks |
| icecast2 multi-mount | SDRTrunk broadcaster + SDRangel→icecast |
| scanner-vlc-* | SDRangel/SDRTrunk native CoreAudio out |
| systemd boot-order | launchd plists |
| apple-firmware WiFi | native macOS |
| Linux applesmc fail (no fan) | **macOS T2 SMC fan native ✅** |
| chirp gain-inversion bug | gone (decoders handle their own gain) |
| `--gain 0.7` BOOM cap | macOS CoreAudio + BOOM AVRCP (different mechanism) |
| Claude SSH → `ubuntu@scannerbox` | Claude SSH → `willminkoff@mac-mini` (or whatever user) |
| Claude drives systemctl / cmd-bus / journalctl | Claude drives `launchctl` + REST calls + log scraping |
| Claude drives chirp cmd-bus | Claude drives SDRangel REST :8091 + SDRTrunk hooks |

## Device/role mapping (from `etc/mac/sdr_fleet_policy.json`)
- **RSP-A `180903EF32`** (digital) → **SDRTrunk** → MTRTRS + TACN (P25 Phase II).
- **RSP-B `1809063632`** (airband+ground) → **SDRangel** → AM airband + NFM ground.
- Both via the shared SDRplay apiService (`com.sdrplay.service` LaunchDaemon). Invariant: **max 1 dual-tuner RSPduo** (concurrent dual-tuner segfaults the daemon).

## Mobile UX — TWO complementary paths
Mobile control has **two valid entry points; one does not preclude the other**:
- **Path A — thin custom web UI** on the Mac (the Option-2 thing): responsive layout over SDRangel REST + SDRTrunk hooks, surfacing the controls Will reaches for most. Build effort = Phase 2.
- **Path B — Dispatch chat with Claude:** Will tells Claude what he wants while mobile; Claude drives the box (SSH → SDRangel REST / SDRTrunk hooks / `launchctl` / logs), exactly like today on Linux. **The easy alternative** — no UI to build — but requires Claude to remain in the loop with SSH + API access to the macOS box.
- They complement each other: Path B is available immediately once Claude's access is preserved (Phase 4); Path A is the polished daily-driver once built (Phase 2). Path B also covers anything the thin UI doesn't expose.

## Phased plan
- **Phase 0 — Decision** (this doc). ✅ captured.
- **Phase 1 — External-media validation** (NO cutover; fully reversible):
  - Install macOS on a **USB / Thunderbolt SSD** (internal Ubuntu SB6 untouched = rollback).
  - SDRangel: install + **analog scan parity** (RSPduo airband + ground; BOOM audio routes via CoreAudio).
  - SDRTrunk: install + **JMBE compile** + **MTRTRS lock** = the definitive live P25-on-macOS test.
  - **Verify the fan ramps under load** (the thermal proof — the whole point).
- **Phase 2 — Build the thin web UI** (if Phase 1 passes):
  - Pick stack (Flask to match airband-ui style, or Node/Express).
  - SDRangel REST integration (documented Swagger on :8091).
  - SDRTrunk integration mechanism — **research needed** (JMS broadcast? poll event log? CLI/playlist hooks?).
  - Mobile-responsive; surface the controls Will reaches for most (informed by Phase 1).
- **Phase 3 — Disk strategy**: wipe internal NVMe → macOS, or boot-from-external permanent? Trade-offs: NVMe perf vs removability vs rollback ease.
- **Phase 4 — Re-implement glue on macOS**: launchd auto-start (SDRangel + SDRTrunk), **HPDB→CSV converter**, remote-audio path (SDRTrunk icecast done; SDRangel's piece), macOS BOOM management (CoreAudio + AVRCP), **verify Claude/Dispatch SSH access + API reachability from Claude task sessions** (Tailscale + key auth to the macOS box; SDRangel REST :8091, SDRTrunk hooks, `launchctl`/log access, sudo equivalents all reachable).
- **Phase 5 — Parity check** vs the Ubuntu setup (bands, audio, remote, boot-survival, thermal-under-load).
- **Phase 6 — Physical cutover** (only after parity + rollback proven).

## Risk register
1. **SDRTrunk Phase 2 in production** — verified it *supports* P25 Phase 2 (CC follow + TDMA voice), but **live testing on this antenna is the proof** (Phase 1).
2. **TACN won't decode regardless of decoder** — RF/site problem (TACN already fails on op25); decoder-agnostic.
3. **JMBE library build** — a known SDRTrunk step (voice decode) the install script doesn't yet automate.
4. **macOS USB version sensitivity** — SDRTrunk repo flags RSPduo USB quirks on recent macOS (e.g. Tahoe 26.1); verify on whatever version we run.
5. **SDRTrunk control surface < SDRangel REST** — SDRTrunk's programmatic API is less mature; the custom UI may need creative integration on the SDRTrunk side.
6. **Mobile UX downgrade** until the Phase 2 custom UI ships (native apps aren't mobile-friendly).
7. **Rollback discipline** — Phase 1 external boot must be solid before ANY internal-disk action.
8. **Today's Linux work becomes legacy** (gain-inversion fix, `--gain 0.7` cap, boot ordering) — but stays in git as reference.
9. **Claude/Dispatch access must survive the OS swap** — SSH key setup on macOS, Tailscale auth, `sudo`/`launchctl` equivalents, and API reachability (:8091 etc.) from Claude task sessions. If this isn't preserved, Path B (conversational control) breaks and mobile UX falls back to Path A only.

## Why now
- Thermal critical today (**100°C**) forced stopping chirp+op25 entirely.
- The Linux fan-control wall is **permanent** (`applesmc` fails `-5` on T2).
- **SDRTrunk + SDRangel together cover analog + P25 + remote audio** (no band loss).
- Repo author **already pre-scoped this** (`mac-install-sdrtrunk.sh`, `mac-install-sdrangel.sh`, `sdr_fleet_policy.json`, `etc/mac/`).
- **Cleaner architecture:** two native macOS apps + a thin UI vs the multi-process chirp/op25/airband-ui/scanner-vlc/icecast2 chain.

## Recommendation
**Phase 1 first.** External-media validation is fully reversible, ~half a day, and definitively answers the three gating questions: **(1) does SDRTrunk lock MTRTRS on this antenna, (2) does the fan ramp under load, (3) does BOOM audio route via CoreAudio.** If yes → the full plan proceeds. If no → stay on Linux and just reduce DSP load (fewer channels / gate idle bands), accepting marginal thermals.

## Cross-references
- Supersedes the SDRangel-substrate revision of `SDR_DEMOD_DECISION_2026-06-03.md` (Option C), now extended with SDRTrunk for P25.
- `feature_scanner_sdrangel_mode_switch` (memory) — the Linux-side A/B SDRangel switch; still relevant if Phase 1 fails and we stay on Linux.
- `project_airband_rf_collapse_recurring` (memory) — root-caused as gain inversion (resolved operationally); left as cross-reference.
- `docs/sdrangel-scan-38380.md` — the verified-working SDRangel analog scanner.
