# Gen-2 Profile Cleanup — Rip Scope

**Host:** `ubuntu@micro.local`  **Branch:** `main` `a351ab3`  **Generated:** 2026-05-30T02:29:32Z
**Mode:** read-only audit. No edits made. Skim `journalctl` and registry, propose plan, do not touch.

## What "gen-2" means in this codebase

Will's intent matches one specific concept: the **rtl-airband "profile" as a complete preset** — one `.conf` file per use case (KBNA, KATL, KHOP, etc.), one active at a time, switched via `POST /api/profile`. HPDB favorites replaced that pattern: the active rtl-airband conf is now `rtl_airband_hp3_favorites_{airband,ground}.conf`, regenerated from `data/hp_state.json[favorites][].custom_favorites[]` on every tile tap.

The literal markers (`gen2`, `pre_hpdb`, `profile_loader`, `legacy_profile`) are almost absent — only one mention in `data/favorites_seeds/README.md` ("rebuilt from the legacy gen-2 rtl-airband .conf"). The signal is structural, not textual. The two reverted commits (`b28fee8`, `d91980d`) are the smoking gun: Will tried to surface 8 specific airband presets — `acy_airshow, airband (KBNA), atl, khop, kmqy, nashville_centers, tower, tune_atis` — in the Favorites widget, then reverted, calling them "old gen 2 profiles". Those 8 ids define the gen-2 perimeter.

## Critical constraint: `/api/profile` is NOT all gen-2

`journalctl -u airband-ui.service --since "24 hours ago"` shows **10 POSTs to `/api/profile`** in 24h, **all** for `bandscan_marine` or `bandscan_mil_air` on `target=ground`. The band-scan tiles (`#btn-bandscan-{airband,marine,cb,mil-air,rail}`, sb3.html:3172+, bound at 4737+) call the same endpoint with profile ids the reverted commit explicitly excluded as "managed elsewhere". So `/api/profile` (POST form), `/api/profiles` (GET), `profile_config.py`, `profiles.json`, the registry in `config.py PROFILES`, and the non-gen-2 `.conf` files in `profiles/` (bandscan_*, acars, radiosonde, wx, hp3_favorites_*, none_*) all **stay**.

## Findings, classified

### Confidently dead (Phase 1 — safe rip)

| Path | LOC | Why dead |
|---|---|---|
| `ui/profile_loop.py` | 982 | `get_profile_loop_manager()` has **zero callers** outside the file. `app.py` and `server_workers.py` never start it. `/api/profile-loop` already returns HTTP 410 (`handlers.py:4159`). Zero runtime hits in 30 days. |
| `ui/hp_favorites.py` | 319 | Module docstring claims "bridge for analog and digital profiles", but `grep -rn 'from .*hp_favorites\b'` returns zero importers (only `hp_favorites_wizard` and `hp_favorites_sync` are imported elsewhere). Pre-HPDB scaffolding superseded by `favorites_runtime.py`. |
| `actions.py` `_DEFAULT_PROFILE_LOOP_BUNDLE_DIR`, `_PROFILE_LOOP_BUNDLE_DIR`, `_PROFILE_LOOP_BUNDLE_NAME`, `_PROFILE_LOOP_MAX_SELECTED`, `action_apply_profile_loop_bundle`, dispatch entry at `actions.py:1687` | ~120 | Only dispatched on `action_type == "profile_loop_bundle"`. Only enqueuer of that action is inside `profile_loop.py:538` itself (circular). Removing `profile_loop.py` strands it. |
| `handlers.py:4159-4164` (`/api/profile-loop` GET 410 stub) and `5666-5671` (POST 410 stub) | ~12 | Tombstones for retired endpoint. |
| `config.py:273-277` `PROFILE_LOOP_STATE_PATH`, `PROFILE_LOOP_TICK_SEC` | 5 | Only referenced inside `profile_loop.py`. |
| `sb3.html` profile-loop JS — `els.profileLoop*` (5048-5510), `openProfileLoopSidecar` (8874), `state.profileLoopUi` branches (8345, 8824, 8866-8869) | ~80 JS | DOM ids `profile-loop-sidecar`, `profile-loop-tab-analog`, etc. **do not exist in sb3.html**. Every `if (els.profileLoopX)` guard short-circuits on null. |
| `sb3.html` Profile Editor Sidecar UI — `#profile-sidecar` aside + analog panel (3970-4060ish) + `openProfileEditorSidecar` (8823) + event bindings (16933, 16936) | ~150 | DOM trigger ids `profile-editor-open-analog` and `profile-editor-open-digital` **do not exist in sb3.html** (only the `getElementById` lookups at 5513-5514). Sidecar unreachable from UI. |
| `handlers.py` `/api/profile-editor/analog` (3161), `/analog/validate` (5238), `/analog/save` (5293) | ~180 | Only ever called by the dead Profile Editor Sidecar. Zero log hits in 7 days. |
| `handlers.py` `/api/profile-editor/digital`, `/digital/validate`, `/digital/save` (3170, 5274, 5350) | ~200 | Same dead UI. **CAVEAT:** `favorites_runtime.py:1275` imports `save_digital_editor_payload` directly — keep that import path live; only the HTTP route can go. |
| `handlers.py` `/api/profile/create` (5751), `/update` (5913), `/delete` (5931), `/update_freqs` (5844), GET `/api/profile?id=` (3178) | ~280 | Profile-editor CRUD. Zero hits in 7 days. (POST `/api/profile` form-body is the bandscan-shared path and stays.) |

**Phase 1 total: ~2,300 LOC removable** with zero runtime impact based on logs + reachability.

### Vestigial-but-referenced (Phase 2 — careful surgery)

| Path | Reference | Required edit before removal |
|---|---|---|
| `ui/profile_editor.py` analog half (~237 of 991 LOC: `_find_analog_profile`, `_read_analog_modulation_bandwidth`, `_format_analog_freqs_text`, `get_analog_editor_payload`, `_apply_analog_modulation_bandwidth_text`, `_normalize_analog_settings`, `save_analog_editor_payload`, `validate_analog_editor_payload`, `analog_profile_is_active`) | Imported by `handlers.py:201, 321` (top-of-file batch import) and by the analog HTTP routes | Delete the analog HTTP routes first (Phase 1), trim the batch import, then drop the analog functions. Digital half + shared helpers (~754 LOC) stay — `favorites_runtime.py:1275` still uses `save_digital_editor_payload`, and `tests/test_profile_editor_site_aware.py` covers shared `_parse_systems_json_text`. |
| `ui/config.py:380-396` `PROFILES` list (17 entries) | `profile_config.py:182` uses it as default seed when `profiles/profiles.json` is absent | Once gen-2 ids are dropped from `profiles.json`, prune the matching `PROFILES` entries. Keep bandscan_*, acars, radiosonde, wx, hp3_favorites_*, none_*. |

### Mixed responsibility (needs human review)

The 8 gen-2 `.conf` files (`acy_airshow, airband, atl, khop, kmqy, nashville_centers, tower, tune_atis`) — total ~432 lines — and their `profiles.json` entries. **These are gitignored** (`.gitignore` line `profiles/rtl_airband_*.conf`), so "removal" means deleting from the live `profiles/` directory plus removing the registry rows. Only `acy_airshow` has an HPDB seed equivalent (`data/favorites_seeds/fav-acy-airshow.json`, commit `cb0ed10`). KBNA, KATL, KHOP, KMQY, Nashville Centers, TOWER, Tune ATIS would need seeds before deletion, otherwise the user loses any path to those frequency sets. **Do not delete without Will's per-id approval.**

The 5 possibly-gen-2 ground-side presets (`campbell_ground, campbell_nfm, gmrs, gmrs_frs_murs, mtears`) were never enumerated by Will. Pattern-match suggests they're gen-2 (single-preset .conf), but I can't prove it from logs. **Human review.**

### Still in use (do not touch)

`profile_config.py` (986 LOC, modified today in `a351ab3`), `profile_metadata.py` (171 LOC, used for HPDB teardown contracts on `hp3_favorites_*`), `managed_analog_controls.py`, `favorites_runtime.py`, `hp_favorites_sync.py`, `hp_favorites_wizard.py`, `profiles/profile_metadata.json`, `profiles/managed_analog_controls.json`, `profiles/profiles.json` (the registry itself — it lists HPDB and bandscan profiles too), all `rtl_airband_hp3_favorites_*.conf`, all `rtl_airband_bandscan_*.conf`, `rtl_airband_acars.conf`, `rtl_airband_radiosonde.conf`, `rtl_airband_wx.conf`, `rtl_airband_none_*.conf`.

### `hp_state.json` schema check

Top-level keys: `avoid_list, custom_favorites, enabled_service_tags, favorites, favorites_name, lat, lon, mode, nationwide_systems, range_miles, service_tag_schema_version, strict_location, travel_mode_enabled, use_location, zip`. `mode ∈ {"full_database","favorites"}`. **No gen-2 leftover keys at boot.** Nothing to clean here.

### Runtime journal check

`journalctl -u airband-ui.service --since "1 hour ago" | grep -iE 'gen2|legacy|profile_loader|legacy_profile'` — **zero matches**. Past 7 days: zero hits on `/api/profile/{create,update,delete,update_freqs}`. Past 30 days: zero `profile_loop_bundle` activity.

## Proposed rip plan

**Phase 0 — "Fix E.1" smallest safe rip (~80 LOC, JS only, zero controversy):** delete the dead profile-loop JS branches in `sb3.html` — the `els.profileLoop*` lookups at 5048-5510, `openProfileLoopSidecar` at 8874, and the `state.profileLoopUi` branches in `pollStatus`/`refresh` at 8345/8824/8866-8869. No DOM exists for any of it; no network calls happen; no Python touched. Validates the audit's reachability findings without risk. Test by hard-refreshing the UI and watching DevTools console for null errors (there should be none — the guards already protect against it; you're just deleting the guards).

**Phase 1 — Confidently dead rip (~2,300 LOC), in this order to keep tests green:**
(1) `sb3.html`: delete Profile Editor Sidecar markup (the `#profile-sidecar` aside) + `openProfileEditorSidecar` + tab-switch JS + the remaining dead profile-loop JS skeletons.
(2) `handlers.py`: delete `/api/profile-editor/analog*` (3 routes) and `/api/profile-editor/digital*` (3 routes), then `/api/profile/{create,update,delete,update_freqs}` and GET `/api/profile?id=` (5 routes), then the two `/api/profile-loop` 410 stubs.
(3) `actions.py`: delete `action_apply_profile_loop_bundle`, the 4 `_PROFILE_LOOP_*` module constants, and the dispatch entry at line 1687.
(4) `ui/profile_loop.py`: delete the whole file.
(5) `ui/config.py`: delete `PROFILE_LOOP_STATE_PATH` + `PROFILE_LOOP_TICK_SEC`.
(6) `ui/hp_favorites.py`: delete the whole file.
Verify `airband-ui.service` starts cleanly; run `tests/test_profile_editor_site_aware.py`, `tests/test_profile_metadata.py`, `tests/test_profile_config_labels.py`, `tests/test_managed_analog_controls.py`.

**Phase 2 — Surgery (~250 LOC + registry shrink):** trim analog half of `profile_editor.py`; trim gen-2 entries from `config.py PROFILES`; then (only after per-id Will approval + HPDB-seed migration) drop the 8 airport `.conf` files and their `profiles.json` rows.

**Phase 3 — Hygiene:** the `.gitignore` patterns `profiles/rtl_airband_*.conf`, `profiles/managed_analog_controls.json`, `profiles/profiles.json` stay (still runtime-written, see in-tree comment). Nothing else to touch.

**Risk:** Phase 0 = trivial. Phase 1 = low (everything runtime-cold for 7-30 days, but run the test suite + dev-box `airband-ui.service` restart before pushing). Phase 2 = medium (`config.py PROFILES` is the registry default; verify `split_profiles()` still returns sane airband/ground lists). Phase 3 = none.

**Out of scope:** restart-routing consolidation, wedge escalation review, the `*.bak.*` / `*.pre-*` clutter in `ui/` (already gitignored), `.codex-backups/`, `archive/`.
