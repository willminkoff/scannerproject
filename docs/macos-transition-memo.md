# macOS Transition Memo — ScannerBox on the Mac mini

**Canonical handoff doc.** Written 2026-06-27 at the end of a remote-driven bring-up
session, just before control passes to a code agent running **natively on the Mac mini**.
Read this first, then the memory entries listed in §F. Be skeptical of anything here that
you can verify on the box — verify it.

This is bring-up state, not production. Two decode proofs landed; nothing is "in service"
(no bands are being scanned for the user yet). Plenty of warts below — they're called out
on purpose.

> ⚠️ **Historical, and about a DIFFERENT BOX than the current fleet.** This memo
> describes the **2018 Intel Mac mini** (`macmini.lan`, macOS 15.7.7, T2) during
> the era when **both RSPduos were attached to that one host**. Its serial→role
> claims are accurate for that box and are **left as-written on purpose** — do
> not "correct" them, and do not apply them to Neptune. Today the RSPduos are on
> separate hosts: `180903EF32` on **Neptune** (digital/SDRTrunk), `1809063632`
> on **Venus** (airband/SDRangel). Note §66/§110's
> `RSPduo Tuner 1 SER#180903EF32` — that has been right all along, and it is
> corroborating evidence for the rev-5.0 serial correction. See fleet policy
> rev 5.0 and `docs/sb3-neptune-architecture.md` §7.5.

---

## A. Current state (as of handoff)

- **Host:** `macmini.lan` / Tailscale `wills-mac-mini-1.tail508e50.ts.net` (100.106.194.41).
  User `willminkoff-scannerbox`. **macOS 15.7.7 (24G720)**, 2018 Intel Mac mini / T2.
- **SDRangel:** installed at `/Applications/SDRangel.app`, REST API on **:8091**. Decodes
  analog via the **airband RSPduo `1809063632`**. Proof: locked **NOAA WX 162.550 at −37 dBFS**,
  NFM squelch open (vs −66 noise floor). At handoff it is **auto-running (pid varies)** —
  see the contention wart in §C. It has **crashed at least twice** today (DiagnosticReports
  `SDRangel-2026-06-27-19224 7.ips`, `-164439.ips`) — watch stability.
- **SDRTrunk:** installed at `~/SDRTrunk` (v0.6.1), JMBE built. MTRTRS P25 playlist
  auto-starts on the **digital RSPduo `180903EF32`**. Proof: **decoded the MTRTRS control
  channel** — real TSBKs incl. `NET_STATUS_BCAST`, `RFSS_STATUS_BCST` (RFSS 3 / SITE 3),
  `GRP_VCH_GRNT_UPD` (voice grant TG 10218), **WACN `xBEE00`, NAC `x443`, SYSTEM `x44B`**.
  Lock is **intermittent** (lots of `SYNC LOSS`, ~124 valid TSBKs in the last run) — gain not
  yet tuned. **At handoff SDRTrunk is STOPPED** (clean state; start recipe in §C).
- **Foundation:** Homebrew (`/usr/local`, Intel), **openjdk@21** (runtime) + **openjdk@17**
  (JMBE build only), **SDRplay API 3.15.1** (LaunchDaemon `com.sdrplay.service`, apiService
  at `/Library/SDRplayAPI/3.15.1/bin/sdrplay_apiService`). All survived a reboot today.
- **No bands are being scanned for the user.** This is validation/bring-up only.

## B. Architecture / file layout

| Thing | Path |
|---|---|
| Repo | `~/scannerproject` (on `main`; **can't push** — no git creds, fetch only if public) |
| SDRTrunk app | `~/SDRTrunk/bin/sdr-trunk` (jpackage runtime; **no `.app`**, `open -a` fails) |
| SDRTrunk tuner config | `~/SDRTrunk/configuration/tuner_configuration.json` |
| SDRTrunk playlist (live) | `~/SDRTrunk/playlist/default.xml` |
| SDRTrunk app log | `~/SDRTrunk/logs/sdrtrunk_app.log` (NO decoded TSBKs here) |
| SDRTrunk decoded events | `~/SDRTrunk/event_logs/*_decoded_messages.log` / `*_call_events.log` |
| SDRTrunk + calibration + JMBE prefs | `~/Library/Preferences/io.github.dsheirer.plist` (Java Preferences) |
| JMBE library | `~/SDRTrunk/jmbe/jmbe-1.0.9.jar` |
| SDRangel app | `/Applications/SDRangel.app` (REST :8091) |
| SDRangel device/DSP settings | `~/Library/Application Support/f4exb/SDRangel/` + `~/Library/Preferences/com.f4exb.SDRangel.plist` |
| SDRangel SDRplay plugin | `…/SDRangel.app/Contents/Resources/lib/plugins/libinputsdrplayv3.dylib` (+ `.bak`) |
| SDRplay API | `/Library/SDRplayAPI/3.15.1/`, libs symlinked in `/usr/local/lib/libsdrplay_api.*` |
| Repo reference copies | `macos/sdrtrunk/` (this commit), `macos/` (clients, data, launchd, scannerctl) |

## C. Known-good recipes / gotchas (the real warts)

1. **SDRangel hogs the apiService on auto-start — THIS was the "Unable to source" red herring.**
   Both SDRangel and SDRTrunk reopen on login (macOS "reopen windows", not login items —
   `System Events` shows no login items). SDRangel grabs the SDRplay apiService first, so
   SDRTrunk sees `Discovered [0] RSP devices` and its channel allocator logs
   `Unable to source channel … No Tuner Available`. **It was never an `isTunable`/`canTune`/
   sample-rate bug** — the tuner simply wasn't in SDRTrunk's discovered set. **Fix: only ONE
   SDRplay consumer at a time.** Stop SDRangel before running SDRTrunk on the RSPduo, or give
   each app a different RSPduo and never let both enumerate simultaneously at boot.

2. **SDRTrunk single-CC decode recipe (headless, reproducible):**
   - Ensure SDRangel is stopped (see #1).
   - `~/SDRTrunk/configuration/tuner_configuration.json` has `disabledTuners` leaving ONLY
     `RSPduo Tuner 1 SER#180903EF32` (reference copy in `macos/sdrtrunk/`).
   - `~/SDRTrunk/playlist/default.xml` = P25 Phase 1 decoder, `modulation="CQPSK"`,
     `sourceConfigTunerMultipleFrequency` with the 4 CCs, `event_log_configuration` enabled.
   - Launch: `nohup ~/SDRTrunk/bin/sdr-trunk >/tmp/sdrtrunk.out 2>&1 &` (runs headless and
     DOES decode — the log says "starting main application headless").
   - Watch `~/SDRTrunk/event_logs/*_decoded_messages.log`. `grep -av 'SYNC LOSS'` = real decode.

3. **The dirty-release problem (NOT yet properly fixed).** Every SDRTrunk stop leaves the
   RSPduo open in the apiService → next start sees `[0] RSP`. **No sudo bandaid allowed.**
   Confirmed workaround: **graceful stop + wait ~22 s** → the apiService times out the dead
   client and frees the device (`pkill -TERM …; sleep 22`). A full reboot also clears it.
   **Proper fix (backlog): a JVM shutdown hook in SDRTrunk that calls `sdrplay_api_Close()` on
   each tuner before exit** — upstream-style, no sudoers `launchctl kickstart` entry.

4. **SDRangel dyld `@rpath` fix (already applied).** The SDRplay plugin shipped depending on
   the **bare** name `libsdrplay_api.so.3`; macOS dyld doesn't search `/usr/local/lib` for
   bare names, so the plugin silently failed to load and SDRangel showed no RSPduo. Fixed with
   `install_name_tool -change libsdrplay_api.so.3 @rpath/libsdrplay_api.so.3` + ad-hoc
   re-sign. Backup at `…/plugins/libinputsdrplayv3.dylib.bak`. If you ever reinstall SDRangel,
   redo this.

5. **JMBE build needs JDK 17.** Gradle 7.4 (JMBE's wrapper) can't run on Java 21
   ("Unsupported class file major version 65"). `brew install openjdk@17`, build with
   `JAVA_HOME=/usr/local/opt/openjdk@17`. The resulting jar runs fine under the Java 21 runtime.

6. **Calibration is mandatory and headless-able.** Without it, `ComplexMixerFactory:
   UNCALIBRATED` → SDRTrunk can't build a channel mixer. Run once:
   `~/SDRTrunk/bin/java --add-modules=jdk.incubator.vector --enable-preview
   --enable-native-access=ALL-UNNAMED -cp "$HOME/SDRTrunk/lib/*"
   io.github.dsheirer.vector.calibrate.CalibrationManager` (~4 min; `MIXER_COMPLEX` is the
   one that matters). Persists in the plist. Already done.

7. **JMBE path preference — set via the Java Preferences API, NOT by hand-editing the plist**
   (hand-editing risks clobbering the calibration in the same plist). Key
   `path.jmbe.library.1.0.0` under node `/io/github/dsheirer/preference/decoder`. Done via
   `jshell` using `/usr/local/opt/openjdk@21/bin/jshell`. Already set.

8. **Two concurrent dual-tuner RSPduos = isochronous USB collapse** (`0xe00002ee`). This is
   why #2 pins a single tuner. Don't enable both tuners on a duo, and don't run both duos
   dual.

## D. Backlog / open items

- **Gain tuning** for a solid continuous CC lock + a **JMBE voice decode** proof. Edit
  `tuner_configuration.json` for the `RSPduo Tuner 1 SER#180903EF32` entry
  (`basebandGainReduction` down / `lna` up) while SDRTrunk is stopped. Also: voice traffic
  channels span the 851–862 downlink; at the current **2 MHz** sample rate the tuner only
  covers ±1 MHz of the CC, so most grants are out of window — **raise the RSPduo sample rate
  (~8 MHz single-tuner)** so granted Phase-2 traffic channels are reachable.
- **Clean-release fix** (§C#3) — JVM shutdown hook, upstream-style.
- **Auto-start coordination** — stop SDRangel + SDRTrunk fighting over the apiService on boot.
  Likely disable app reopen-on-login and start them deliberately (launchd plists exist as
  templates in `macos/launchd/`, not yet installed).
- **`macos/scannerctl/`** is a skeleton — build it into a working SB6-style mobile dashboard.
  This is Will's real UX gap (he had a web UI on Linux).
- **BOOM audio routing** — SDRangel/SDRTrunk → BOOM over CoreAudio. SDRangel audio-output
  device selection needs the GUI; logged CoreAudio errors when launched headless over SSH.
- **Second RSPduo for SDRTrunk** — the airband duo `1809063632` is currently disabled in
  SDRTrunk to dodge the dual-RSPduo conflict. Long-term: SDRangel + SDRTrunk coexist, one
  duo each, never enumerating simultaneously.
- **HPDB → SDRTrunk talkgroup XML** — `macos/data/hpdb_to_sdrtrunk.py` is a skeleton, not
  run; the current playlist has no talkgroup aliases (TGIDs show raw).
- **Scope doc** `docs/macos-backend-migration-scope.md` has Phases 3–6.

## E. Critical operational rules

- **NO BOOM volume changes — ever.** Connection only; no AVRCP/PipeWire/`wpctl`/`pactl`
  volume mutations. Surface and ask first. (The Ubuntu-era `cvlc --gain 0.7` BOOM cap is
  **Linux-specific and does NOT carry over** — macOS routes audio via CoreAudio/AVRCP; a new
  cap mechanism is TBD, do not invent one that touches BOOM volume.)
- **NO sudoers bandaids** for the apiService dirty-release. Solve it properly (§C#3). Do not
  add a NOPASSWD `launchctl kickstart` entry.
- **No new sudo dependencies** generally.

## F. Context the new agent needs

- **Backup data:** `/Users/willminkoff/Downloads/sb6-data-backup-2026-06-26/sb6-data-backup-2026-06-26.tar.gz`
  on Will's *laptop* (homepatrol.db 52 MB + hp_state.json). On the mini, the HPDB +
  hp_state are already at `~/scannerproject/data/`.
- **Tailscale:** `wills-mac-mini-1.tail508e50.ts.net`.
- **main HEAD at handoff:** recorded in the commit that adds this memo (see git log; the memo
  commit is the new HEAD).
- **Memory entries to read first:** `project_macos_sdrtrunk_p25_validated` (the full SDRTrunk
  recipe + gotchas), `macos-backend-migration-scoped-sdrangel-sdrtrunk-thin-ui` (the scope),
  `project_scannerbox_sdrangel_2026_06_23` (foundation install), `project_mac_mini_sdrangel_installed`.
- **Linux-era lessons that still inform decisions:** `project_airband_rf_collapse_recurring`
  (gain *inversion* — a value-mapping lesson, though that exact bug was a chirp/Linux thing),
  `feature_volume_equalization`, plus any `feedback_*` rules (never touch BOOM volume;
  Nashville-metro brand voice; deploy/service-restart discipline).
- **The repo on the box can't push** (no credentials, `Device not configured`). Set up git
  auth before committing from the box, or commit from a machine that has creds.

## G. Suggested next moves

1. Verify foundation if the box rebooted (apiService up, RSPduos on USB, calibration intact,
   JMBE pref present). Quick checks: `ps aux | grep sdrplay_apiService`,
   `system_profiler SPUSBDataType | grep -c 0x3020` (expect 2), `defaults read
   io.github.dsheirer | grep -c UNCALIBRATED` (expect 0).
2. Resolve the SDRangel/SDRTrunk auto-start contention (§C#1, §D) so you have a deterministic
   single-consumer state to work in.
3. Pick the priority with Will: **gain/sample-rate tuning → voice-decode proof** vs **building
   `scannerctl` into a real dashboard**. Both are teed up.
4. Solve the dirty-release properly (§C#3) — it makes every future iteration cheaper.
