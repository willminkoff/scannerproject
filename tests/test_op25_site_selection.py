from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ui import digital
from ui.op25_adapter import (
    Op25Adapter,
    _candidate_is_unhealthy,
    _flatten_active_runtime_systems,
    _hydrate_runtime_systems_for_config,
    _iso_utc,
    _load_selector_state,
    _normalize_runtime_system_definitions,
    _save_selector_state,
    generate_multi_rx_config,
    generate_trunk_tsv,
)


def _make_adapter(runtime_dir: str | None = None) -> Op25Adapter:
    with mock.patch("ui.op25_adapter.validate_digital_service_name", return_value=True):
        adapter = Op25Adapter()
    if runtime_dir:
        adapter._runtime_dir = runtime_dir
    return adapter


def _candidate(
    site_id: str,
    *,
    site_name: str | None = None,
    enabled: bool = True,
    score: int = 0,
    control_locked: bool = True,
    control_decode_available: bool = True,
    last_tsbk_age_sec: float | None = 1.0,
    recent_any_grants: int = 0,
    recent_monitored_tg_hits: int = 0,
    avoided: bool = False,
    cooldown_until_ms: int = 0,
) -> dict:
    return {
        "site_id": site_id,
        "site_name": site_name or site_id,
        "enabled": enabled,
        "score": score,
        "control_locked": control_locked,
        "control_decode_available": control_decode_available,
        "last_tsbk_age_sec": last_tsbk_age_sec,
        "recent_any_grants": recent_any_grants,
        "recent_monitored_tg_hits": recent_monitored_tg_hits,
        "_avoided": avoided,
        "_revisit_block_until_ms": cooldown_until_ms,
        "state": "candidate",
        "exclusion_reason": "",
        "demotion_reason": "",
    }


def _write_profile(
    profile_dir: Path,
    *,
    systems_payload: dict,
    overrides: dict | None = None,
    talkgroups: dict[str, str] | None = None,
) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "systems.json").write_text(
        json.dumps(systems_payload) + "\n",
        encoding="utf-8",
    )
    labels = talkgroups or {"3207": "Police Dispatch"}
    (profile_dir / "talkgroups.csv").write_text(
        "\n".join(f"{tgid},{label}" for tgid, label in labels.items()) + "\n",
        encoding="utf-8",
    )
    if overrides is not None:
        (profile_dir / "op25_system_config.json").write_text(
            json.dumps(overrides) + "\n",
            encoding="utf-8",
        )


def _selector_state(now_ms: int, **overrides) -> dict:
    state = {
        "selected_site_id": "41154",
        "selected_site_name": "Davidson County Services",
        "selection_mode": "auto",
        "reason_code": "",
        "reason_text": "",
        "last_switch_time": _iso_utc(now_ms - 400_000),
        "switch_count": 0,
        "same_site_restart_count": 0,
        "site_switch_restart_count": 0,
        "generic_restart_count": 0,
        "stale_window_count": 0,
        "current_site_since": _iso_utc(now_ms - 400_000),
        "unproductive_since": "",
        "revisit_block_until": {},
        "candidates": [],
        "_last_restart_time_ms": 0,
        "_last_stale_window_time_ms": 0,
        "_stale_window_times_ms": [],
        "_survey_started_at_ms": 0,
        "_survey_candidate_index": 0,
        "_survey_completed": False,
    }
    state.update(overrides)
    return state


class RuntimeNormalizationTests(unittest.TestCase):
    def test_legacy_profile_normalizes_to_synthetic_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp)
            _write_profile(
                profile_dir,
                systems_payload={
                    "systems": [
                        {
                            "name": "MTRTRS",
                            "control_channels_mhz": [856.9375, 857.4375],
                        }
                    ]
                },
            )
            systems = _normalize_runtime_system_definitions(str(profile_dir))
            self.assertEqual(1, len(systems))
            self.assertEqual("legacy:auto", systems[0]["sites"][0]["site_id"])
            self.assertEqual(
                [856937500, 857437500],
                systems[0]["sites"][0]["control_channels_hz"],
            )

    def test_site_aware_systems_preserve_multiple_sites_and_policy_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp)
            _write_profile(
                profile_dir,
                systems_payload={
                    "systems": [
                        {
                            "name": "MTRTRS",
                            "sites": [
                                {
                                    "site_id": "18863",
                                    "site_name": "Davidson County Simulcast",
                                    "control_channels_hz": [856937500, 857437500],
                                    "enabled": True,
                                },
                                {
                                    "site_id": "41154",
                                    "site_name": "Davidson County Services",
                                    "control_channels_hz": [855912500],
                                    "enabled": "false",
                                },
                            ],
                        }
                    ]
                },
                overrides={
                    "MTRTRS": {
                        "site_policy": {
                            "pinned_site_id": "missing",
                            "preferred_site_ids": ["18863", "missing-pref"],
                            "avoid_site_ids": ["missing-avoid"],
                            "min_dwell_sec": -5,
                        }
                    }
                },
            )
            systems = _normalize_runtime_system_definitions(
                str(profile_dir),
                op25_overrides=json.loads((profile_dir / "op25_system_config.json").read_text(encoding="utf-8")),
            )
            self.assertEqual(2, len(systems[0]["sites"]))
            self.assertTrue(systems[0]["sites"][0]["enabled"])
            self.assertFalse(systems[0]["sites"][1]["enabled"])
            self.assertIn("unknown pinned_site_id missing", systems[0]["site_policy_warnings"])
            self.assertIn("unknown preferred_site_ids entry missing-pref", systems[0]["site_policy_warnings"])
            self.assertIn("unknown avoid_site_ids entry missing-avoid", systems[0]["site_policy_warnings"])
            self.assertEqual(120, systems[0]["site_policy"]["min_dwell_sec"])

    def test_hydrate_runtime_state_persists_without_rewriting_profile_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            profile_dir = tmp_path / "profile"
            runtime_dir = tmp_path / "runtime"
            systems_payload = {
                "systems": [
                    {
                        "name": "MTRTRS",
                        "sites": [
                            {
                                "site_id": "18863",
                                "site_name": "Davidson County Simulcast",
                                "control_channels_hz": [856937500],
                                "enabled": True,
                            },
                            {
                                "site_id": "41154",
                                "site_name": "Davidson County Services",
                                "control_channels_hz": [855912500],
                                "enabled": True,
                            },
                        ],
                    }
                ]
            }
            _write_profile(
                profile_dir,
                systems_payload=systems_payload,
                overrides={
                    "MTRTRS": {
                        "site_policy": {
                            "pinned_site_id": "18863",
                        }
                    }
                },
            )
            systems_before = (profile_dir / "systems.json").read_text(encoding="utf-8")

            runtime_systems, _ = _hydrate_runtime_systems_for_config(
                str(profile_dir),
                str(runtime_dir),
                op25_overrides=json.loads((profile_dir / "op25_system_config.json").read_text(encoding="utf-8")),
            )
            self.assertEqual("18863", runtime_systems[0]["active_site_id"])
            state = _load_selector_state(str(runtime_dir))
            self.assertTrue(state["systems"])

            runtime_systems_again, _ = _hydrate_runtime_systems_for_config(
                str(profile_dir),
                str(runtime_dir),
                op25_overrides=json.loads((profile_dir / "op25_system_config.json").read_text(encoding="utf-8")),
            )
            self.assertEqual("18863", runtime_systems_again[0]["active_site_id"])
            self.assertEqual(systems_before, (profile_dir / "systems.json").read_text(encoding="utf-8"))


class SiteSelectionDecisionTests(unittest.TestCase):
    def setUp(self):
        self.adapter = _make_adapter()
        self.system = {
            "name": "MTRTRS",
            "site_policy": {
                "mode": "auto",
                "pinned_site_id": "",
                "preferred_site_ids": [],
                "avoid_site_ids": [],
                "min_dwell_sec": 120,
                "unproductive_window_sec": 300,
                "revisit_cooldown_sec": 180,
                "switch_margin": 20,
            },
            "sites": [
                {"site_id": "18863", "site_name": "Davidson County Simulcast", "enabled": True},
                {"site_id": "41154", "site_name": "Davidson County Services", "enabled": True},
            ],
        }
        self.now_ms = 2_000_000

    def _sys_state(self, **overrides) -> dict:
        return _selector_state(self.now_ms, **overrides)

    def test_pinned_enabled_site_always_wins(self):
        system = dict(self.system)
        system["site_policy"] = dict(self.system["site_policy"], pinned_site_id="18863")
        decision, _ = self.adapter._selector_decision_for_system(
            system,
            self._sys_state(),
            [
                _candidate("41154", score=60, site_name="Davidson County Services"),
                _candidate("18863", score=80, site_name="Davidson County Simulcast"),
            ],
            now_ms=self.now_ms,
        )
        self.assertEqual("switch", decision["action"])
        self.assertEqual("18863", decision["site_id"])
        self.assertEqual("site_switch_policy_pinned", decision["reason_code"])
        self.assertEqual("pinned", decision["selection_mode"])

    def test_preferred_site_beats_equal_healthy_non_preferred_when_no_current_site(self):
        system = dict(self.system)
        system["site_policy"] = dict(self.system["site_policy"], preferred_site_ids=["18863"])
        decision, _ = self.adapter._selector_decision_for_system(
            system,
            self._sys_state(selected_site_id="", selected_site_name="", current_site_since=""),
            [
                _candidate("41154", score=60, site_name="Davidson County Services"),
                _candidate("18863", score=160, site_name="Davidson County Simulcast"),
            ],
            now_ms=self.now_ms,
        )
        self.assertEqual("switch", decision["action"])
        self.assertEqual("18863", decision["site_id"])
        self.assertEqual("fallback", decision["selection_mode"])

    def test_avoid_site_loses_to_comparable_non_avoided_site(self):
        decision, _ = self.adapter._selector_decision_for_system(
            self.system,
            self._sys_state(selected_site_id="", selected_site_name="", current_site_since=""),
            [
                _candidate("41154", score=70, site_name="Davidson County Services", avoided=False),
                _candidate("18863", score=90, site_name="Davidson County Simulcast", avoided=True),
            ],
            now_ms=self.now_ms,
        )
        self.assertEqual("41154", decision["site_id"])

    def test_current_site_retained_during_min_dwell_when_healthy(self):
        decision, _ = self.adapter._selector_decision_for_system(
            self.system,
            self._sys_state(current_site_since=_iso_utc(self.now_ms - 30_000)),
            [
                _candidate("41154", score=60, site_name="Davidson County Services"),
                _candidate("18863", score=200, site_name="Davidson County Simulcast"),
            ],
            now_ms=self.now_ms,
        )
        self.assertEqual("stay", decision["action"])
        self.assertEqual("stay_current_min_dwell", decision["reason_code"])

    def test_unhealthy_current_site_switches_immediately_despite_min_dwell(self):
        decision, _ = self.adapter._selector_decision_for_system(
            self.system,
            self._sys_state(current_site_since=_iso_utc(self.now_ms - 30_000)),
            [
                _candidate(
                    "41154",
                    score=-40,
                    site_name="Davidson County Services",
                    control_locked=False,
                    control_decode_available=False,
                    last_tsbk_age_sec=120,
                ),
                _candidate("18863", score=90, site_name="Davidson County Simulcast"),
            ],
            now_ms=self.now_ms,
        )
        self.assertEqual("switch", decision["action"])
        self.assertEqual("18863", decision["site_id"])
        self.assertEqual("site_switch_unhealthy", decision["reason_code"])

    def test_fresh_decode_without_control_tag_is_not_unhealthy(self):
        self.assertFalse(
            _candidate_is_unhealthy(
                _candidate(
                    "41154",
                    score=20,
                    site_name="Davidson County Services",
                    control_locked=False,
                    control_decode_available=True,
                    last_tsbk_age_sec=1.0,
                )
            )
        )

    def test_fresh_decode_without_control_tag_does_not_trigger_same_site_restart(self):
        decision, state = self.adapter._selector_decision_for_system(
            self.system,
            self._sys_state(
                _stale_window_times_ms=[self.now_ms - 31_000] * 5,
                _last_stale_window_time_ms=self.now_ms - 31_000,
                stale_window_count=5,
            ),
            [
                _candidate(
                    "41154",
                    score=20,
                    site_name="Davidson County Services",
                    control_locked=False,
                    control_decode_available=True,
                    last_tsbk_age_sec=1.0,
                ),
            ],
            now_ms=self.now_ms,
        )
        self.assertEqual("stay", decision["action"])
        self.assertEqual("stay_current_healthy", decision["reason_code"])
        self.assertEqual(5, state["stale_window_count"])

    def test_healthy_but_unproductive_current_site_switches_when_alternate_exceeds_margin(self):
        decision, _ = self.adapter._selector_decision_for_system(
            self.system,
            self._sys_state(unproductive_since=_iso_utc(self.now_ms - 400_000)),
            [
                _candidate(
                    "41154",
                    score=50,
                    site_name="Davidson County Services",
                    recent_any_grants=2,
                    recent_monitored_tg_hits=0,
                ),
                _candidate(
                    "18863",
                    score=120,
                    site_name="Davidson County Simulcast",
                    recent_any_grants=2,
                    recent_monitored_tg_hits=3,
                ),
            ],
            now_ms=self.now_ms,
        )
        self.assertEqual("switch", decision["action"])
        self.assertEqual("18863", decision["site_id"])
        self.assertEqual("site_switch_unproductive", decision["reason_code"])

    def test_healthy_quiet_window_shorter_than_unproductive_threshold_does_not_switch(self):
        decision, _ = self.adapter._selector_decision_for_system(
            self.system,
            self._sys_state(unproductive_since=_iso_utc(self.now_ms - 120_000)),
            [
                _candidate("41154", score=60, site_name="Davidson County Services"),
                _candidate("18863", score=200, site_name="Davidson County Simulcast", recent_monitored_tg_hits=3),
            ],
            now_ms=self.now_ms,
        )
        self.assertEqual("stay", decision["action"])
        self.assertEqual("stay_current_healthy", decision["reason_code"])

    def test_same_site_restart_only_after_repeated_stale_windows_with_no_alternate(self):
        stale_site = _candidate(
            "41154",
            score=-40,
            site_name="Davidson County Services",
            control_locked=False,
            control_decode_available=False,
            last_tsbk_age_sec=120,
        )
        state = self._sys_state()
        first, state = self.adapter._selector_decision_for_system(
            self.system,
            state,
            [stale_site],
            now_ms=self.now_ms,
        )
        self.assertEqual("stay", first["action"])
        self.assertEqual("stay_current_unhealthy_no_alternate", first["reason_code"])
        self.assertEqual(1, state["stale_window_count"])

        # Stale threshold is 6: need more stale windows before restart triggers
        for i in range(2, 6):
            decision, state = self.adapter._selector_decision_for_system(
                self.system,
                state,
                [stale_site],
                now_ms=self.now_ms + 31_000 * i,
            )
            self.assertEqual("stay", decision["action"], f"iteration {i}")

        # 6th stale window should trigger same_site_restart
        sixth, state = self.adapter._selector_decision_for_system(
            self.system,
            state,
            [stale_site],
            now_ms=self.now_ms + 31_000 * 6,
        )
        self.assertEqual("same_site_restart", sixth["action"])
        self.assertEqual("same_site_restart_stale", sixth["reason_code"])

    def test_post_restart_grace_period_prevents_stale_counting(self):
        """After a restart, the grace period (90s) prevents stale window accumulation."""
        stale_site = _candidate(
            "41154",
            score=-40,
            site_name="Davidson County Services",
            control_locked=False,
            control_decode_available=False,
            last_tsbk_age_sec=120,
        )
        # Simulate state right after a restart (30s ago)
        state = self._sys_state(
            _last_restart_time_ms=self.now_ms - 30_000,
        )
        decision, state = self.adapter._selector_decision_for_system(
            self.system,
            state,
            [stale_site],
            now_ms=self.now_ms,
        )
        self.assertEqual("stay", decision["action"])
        self.assertEqual("stay_post_restart_grace", decision["reason_code"])
        # Stale window count should not have increased
        self.assertEqual(0, state.get("stale_window_count", 0))

        # After grace period expires (91s after restart), stale counting resumes
        decision2, state2 = self.adapter._selector_decision_for_system(
            self.system,
            state,
            [stale_site],
            now_ms=self.now_ms + 61_000,  # 91s after restart
        )
        self.assertEqual("stay", decision2["action"])
        self.assertEqual("stay_current_unhealthy_no_alternate", decision2["reason_code"])
        self.assertEqual(1, state2["stale_window_count"])

    def test_alternate_switch_happens_before_same_site_restart_when_better_alternate_exists(self):
        state = self._sys_state(
            _stale_window_times_ms=[self.now_ms - 60_000],
            _last_stale_window_time_ms=self.now_ms - 60_000,
            stale_window_count=1,
        )
        decision, _ = self.adapter._selector_decision_for_system(
            self.system,
            state,
            [
                _candidate(
                    "41154",
                    score=-40,
                    site_name="Davidson County Services",
                    control_locked=False,
                    control_decode_available=False,
                    last_tsbk_age_sec=120,
                ),
                _candidate("18863", score=90, site_name="Davidson County Simulcast"),
            ],
            now_ms=self.now_ms,
        )
        self.assertEqual("switch", decision["action"])
        self.assertEqual("site_switch_unhealthy", decision["reason_code"])

    def test_generic_restart_only_when_no_valid_active_site_exists(self):
        decision, _ = self.adapter._selector_decision_for_system(
            self.system,
            self._sys_state(selected_site_id="", selected_site_name=""),
            [
                _candidate("41154", enabled=False),
                _candidate("18863", enabled=False),
            ],
            now_ms=self.now_ms,
        )
        self.assertEqual("generic_restart", decision["action"])
        self.assertEqual("generic_restart_no_valid_site", decision["reason_code"])

    def test_survey_advances_in_canonical_order_not_input_order(self):
        state = self._sys_state(
            selected_site_id="a1",
            selected_site_name="Alpha Site",
            selection_mode="survey",
            current_site_since=_iso_utc(self.now_ms - 20_000),
            _survey_started_at_ms=self.now_ms - 20_000,
            _survey_candidate_index=0,
            _survey_completed=False,
        )
        decision, state = self.adapter._selector_decision_for_system(
            self.system,
            state,
            [
                _candidate("z9", site_name="Zulu Site", score=60),
                _candidate("m5", site_name="Mike Site", score=50),
                _candidate("a1", site_name="Alpha Site", score=55),
            ],
            now_ms=self.now_ms,
        )
        self.assertEqual("switch", decision["action"])
        self.assertEqual("m5", decision["site_id"])
        self.assertEqual(1, state["_survey_candidate_index"])


class RuntimeConfigGenerationTests(unittest.TestCase):
    def test_trunk_and_multi_rx_use_selected_site_channels_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            profile_dir = tmp_path / "profile"
            runtime_dir = tmp_path / "runtime"
            _write_profile(
                profile_dir,
                systems_payload={
                    "systems": [
                        {
                            "name": "MTRTRS",
                            "sites": [
                                {
                                    "site_id": "18863",
                                    "site_name": "Davidson County Simulcast",
                                    "control_channels_hz": [856937500, 857437500],
                                    "enabled": True,
                                },
                                {
                                    "site_id": "41154",
                                    "site_name": "Davidson County Services",
                                    "control_channels_hz": [855912500, 856462500],
                                    "enabled": True,
                                },
                            ],
                        }
                    ]
                },
                overrides={
                    "MTRTRS": {
                        "site_policy": {
                            "pinned_site_id": "18863",
                        }
                    }
                },
            )
            runtime_systems, _ = _hydrate_runtime_systems_for_config(
                str(profile_dir),
                str(runtime_dir),
                op25_overrides=json.loads((profile_dir / "op25_system_config.json").read_text(encoding="utf-8")),
            )
            active = _flatten_active_runtime_systems(runtime_systems)

            trunk = generate_trunk_tsv(active)
            config = generate_multi_rx_config(
                active,
                {"MTRTRS": "14306619"},
                traffic_dongle_serial="56919602",
                traffic_system_name="MTRTRS",
            )

            self.assertIn('"856.93750,857.43750"', trunk)
            self.assertNotIn('"855.91250,856.46250"', trunk)
            self.assertEqual("856.93750,857.43750", config["trunking"]["chans"][0]["control_channel_list"])
            self.assertEqual(856937500, config["devices"][0]["frequency"])
            self.assertEqual(856937500, config["devices"][1]["frequency"])


class RestartBatchingTests(unittest.TestCase):
    def test_batched_restart_runs_once_and_updates_counters_only_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            profile_dir = tmp_path / "profile"
            runtime_dir = tmp_path / "runtime"
            _write_profile(
                profile_dir,
                systems_payload={
                    "systems": [
                        {
                            "name": "MTRTRS",
                            "sites": [
                                {"site_id": "18863", "site_name": "Davidson County Simulcast", "control_channels_hz": [856937500], "enabled": True},
                                {"site_id": "41154", "site_name": "Davidson County Services", "control_channels_hz": [855912500], "enabled": True},
                            ],
                        },
                        {
                            "name": "TACN",
                            "sites": [
                                {"site_id": "legacy:auto", "site_name": "Legacy Control Channel Set", "control_channels_hz": [769831250], "enabled": True},
                            ],
                        },
                    ]
                },
            )
            runtime_dir.mkdir(parents=True, exist_ok=True)
            state = {
                "systems": {
                    "profile::MTRTRS": _selector_state(
                        2_000_000,
                        selected_site_id="18863",
                        selected_site_name="Davidson County Simulcast",
                    ),
                    "profile::TACN": _selector_state(
                        2_000_000,
                        selected_site_id="legacy:auto",
                        selected_site_name="Legacy Control Channel Set",
                    ),
                }
            }
            (runtime_dir / "site_selector_state.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
            adapter = _make_adapter(str(runtime_dir))
            with mock.patch.object(adapter, "_regenerate_runtime_via_script", return_value=(True, "")) as regen, \
                mock.patch.object(adapter, "restart", return_value=(True, "")) as restart:
                adapter._handle_selector_restart_requests(
                    str(profile_dir),
                    [
                        {
                            "type": "switch",
                            "system_name": "MTRTRS",
                            "previous_site_id": "41154",
                            "selected_site_id": "18863",
                            "reason_code": "site_switch_unproductive",
                            "reason_text": "Current site healthy but unproductive",
                            "selection_mode": "auto",
                        },
                        {
                            "type": "same_site_restart",
                            "system_name": "TACN",
                            "previous_site_id": "legacy:auto",
                            "selected_site_id": "legacy:auto",
                            "reason_code": "same_site_restart_stale",
                            "reason_text": "Repeated stale windows",
                            "selection_mode": "legacy",
                        },
                    ],
                )

            regen.assert_called_once()
            restart.assert_called_once()
            updated = _load_selector_state(str(runtime_dir))
            mtrtrs = updated["systems"]["profile::MTRTRS"]
            tacn = updated["systems"]["profile::TACN"]
            self.assertEqual(1, mtrtrs["switch_count"])
            self.assertEqual(1, mtrtrs["site_switch_restart_count"])
            self.assertTrue(mtrtrs["last_switch_time"])
            self.assertTrue(mtrtrs["current_site_since"])
            self.assertTrue(mtrtrs["revisit_block_until"]["41154"])
            self.assertGreater(int(mtrtrs["_last_restart_time_ms"]), 0)
            self.assertEqual(1, tacn["same_site_restart_count"])
            self.assertGreater(int(tacn["_last_restart_time_ms"]), 0)
            # Stale window times should be cleared after restart
            self.assertEqual([], tacn.get("_stale_window_times_ms", []))
            self.assertEqual(0, tacn.get("stale_window_count", 0))
            self.assertEqual(0, tacn.get("_last_stale_window_time_ms", 0))

    def test_failed_restart_does_not_bump_counters_or_last_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            profile_dir = tmp_path / "profile"
            runtime_dir = tmp_path / "runtime"
            _write_profile(
                profile_dir,
                systems_payload={
                    "systems": [
                        {
                            "name": "MTRTRS",
                            "sites": [
                                {"site_id": "18863", "site_name": "Davidson County Simulcast", "control_channels_hz": [856937500], "enabled": True},
                                {"site_id": "41154", "site_name": "Davidson County Services", "control_channels_hz": [855912500], "enabled": True},
                            ],
                        }
                    ]
                },
            )
            runtime_dir.mkdir(parents=True, exist_ok=True)
            state = {
                "systems": {
                    "profile::MTRTRS": _selector_state(
                        2_000_000,
                        selected_site_id="18863",
                        selected_site_name="Davidson County Simulcast",
                    ),
                }
            }
            (runtime_dir / "site_selector_state.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
            adapter = _make_adapter(str(runtime_dir))
            with mock.patch.object(adapter, "_regenerate_runtime_via_script", return_value=(True, "")) as regen, \
                mock.patch.object(adapter, "restart", return_value=(False, "boom")) as restart:
                adapter._handle_selector_restart_requests(
                    str(profile_dir),
                    [
                        {
                            "type": "switch",
                            "system_name": "MTRTRS",
                            "previous_site_id": "41154",
                            "selected_site_id": "18863",
                            "reason_code": "site_switch_unproductive",
                            "reason_text": "Current site healthy but unproductive",
                            "selection_mode": "auto",
                        }
                    ],
                )

            regen.assert_called_once()
            restart.assert_called_once()
            updated = _load_selector_state(str(runtime_dir))
            mtrtrs = updated["systems"]["profile::MTRTRS"]
            self.assertEqual(0, mtrtrs["switch_count"])
            self.assertEqual(0, mtrtrs["site_switch_restart_count"])
            self.assertEqual(0, mtrtrs["same_site_restart_count"])
            self.assertEqual(0, int(mtrtrs["_last_restart_time_ms"]))


class SelectorStatePersistenceTests(unittest.TestCase):
    def test_save_selector_state_uses_unique_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            created: list[str] = []
            real_mkstemp = tempfile.mkstemp

            def _wrapped_mkstemp(*args, **kwargs):
                fd, path = real_mkstemp(*args, **kwargs)
                created.append(path)
                return fd, path

            with mock.patch("ui.op25_adapter.tempfile.mkstemp", side_effect=_wrapped_mkstemp) as mocked:
                _save_selector_state(str(runtime_dir), {"systems": {"p::s": {"selected_site_id": "x"}}})

            self.assertEqual(1, mocked.call_count)
            self.assertEqual(
                {"systems": {"p::s": {"selected_site_id": "x"}}},
                _load_selector_state(str(runtime_dir)),
            )
            self.assertTrue(created)
            self.assertTrue(created[0].startswith(str(runtime_dir / "site_selector_state.json.")))
            self.assertFalse((runtime_dir / "site_selector_state.json.tmp").exists())


class StatusTelemetryTests(unittest.TestCase):
    def test_status_metrics_infers_lock_from_fresh_decode_without_control_tag(self):
        adapter = _make_adapter()
        status = {
            "trunk_update": {
                "systems": {
                    "0": {
                        "system": "TACN",
                        "last_tsbk": 1_000_005.0,
                    }
                }
            },
            "channel_update": {
                "0": {
                    "system": "TACN",
                    "tag": "Region 3 Dispatch - Nashville",
                    "tgid": 47152,
                    "freq": 773581250,
                }
            },
            "call_log": [],
        }
        with mock.patch("ui.op25_adapter.time.time", return_value=1_000_010.0):
            metrics = adapter._status_metrics_for_system(
                status,
                "TACN",
                {"47008", "47152"},
                runtime_system_count=1,
            )
        self.assertTrue(metrics["control_decode_available"])
        self.assertTrue(metrics["control_locked"])
        self.assertEqual(5.0, metrics["last_tsbk_age_sec"])

    def test_preflight_exposes_selected_site_and_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            profile_dir = tmp_path / "profile"
            runtime_dir = tmp_path / "runtime"
            _write_profile(
                profile_dir,
                systems_payload={
                    "systems": [
                        {
                            "name": "MTRTRS",
                            "sites": [
                                {
                                    "site_id": "18863",
                                    "site_name": "Davidson County Simulcast",
                                    "control_channels_hz": [856937500],
                                    "enabled": True,
                                },
                                {
                                    "site_id": "41154",
                                    "site_name": "Davidson County Services",
                                    "control_channels_hz": [855912500],
                                    "enabled": False,
                                },
                            ],
                        }
                    ]
                },
                overrides={
                    "MTRTRS": {
                        "site_policy": {
                            "pinned_site_id": "18863",
                        }
                    }
                },
            )
            adapter = _make_adapter(str(runtime_dir))
            status = {
                "trunk_update": {
                    "systems": {
                        "0": {
                            "system": "MTRTRS",
                            "last_tsbk": 1_000_000.0,
                        }
                    }
                },
                "channel_update": {
                    "0": {
                        "system": "MTRTRS",
                        "tag": "Control Channel",
                        "freq": 856937500,
                    }
                },
                "call_log": [
                    {
                        "system": "MTRTRS",
                        "tgid": "3207",
                    }
                ],
            }
            with mock.patch.object(adapter, "_read_active_profile_dir", return_value=str(profile_dir)), \
                mock.patch.object(adapter, "_poll_op25_status", return_value=status), \
                mock.patch.object(adapter, "_handle_selector_restart_requests", return_value=None), \
                mock.patch("ui.op25_adapter.time.time", return_value=1_000_005.0):
                payload = adapter.preflight()

            self.assertIn("op25_site_selection", payload)
            selection = payload["op25_site_selection"]["MTRTRS"]
            self.assertEqual("18863", selection["selected_site_id"])
            self.assertEqual("Davidson County Simulcast", selection["selected_site_name"])
            self.assertTrue(selection["candidate_sites"])
            disabled = next(row for row in selection["candidate_sites"] if row["site_id"] == "41154")
            self.assertEqual("disabled", disabled["state"])
            self.assertEqual("site disabled by profile", disabled["exclusion_reason"])

    def test_preflight_marks_healthy_but_unproductive_site_distinctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            profile_dir = tmp_path / "profile"
            runtime_dir = tmp_path / "runtime"
            _write_profile(
                profile_dir,
                systems_payload={
                    "systems": [
                        {
                            "name": "MTRTRS",
                            "sites": [
                                {
                                    "site_id": "18863",
                                    "site_name": "Davidson County Simulcast",
                                    "control_channels_hz": [856937500],
                                    "enabled": True,
                                },
                                {
                                    "site_id": "41154",
                                    "site_name": "Davidson County Services",
                                    "control_channels_hz": [855912500],
                                    "enabled": True,
                                },
                            ],
                        }
                    ]
                },
            )
            adapter = _make_adapter(str(runtime_dir))
            state = {
                "systems": {
                    "profile::MTRTRS": {
                        "selected_site_id": "18863",
                        "selected_site_name": "Davidson County Simulcast",
                        "selection_mode": "auto",
                        "reason_code": "stay_current_healthy",
                        "reason_text": "",
                        "last_switch_time": _iso_utc(1_000_000),
                        "switch_count": 0,
                        "same_site_restart_count": 0,
                        "site_switch_restart_count": 0,
                        "generic_restart_count": 0,
                        "stale_window_count": 0,
                        "current_site_since": _iso_utc(1_000_000),
                        "unproductive_since": _iso_utc(1_100_000),
                        "revisit_block_until": {},
                        "candidates": [],
                        "_last_restart_time_ms": 0,
                        "_last_stale_window_time_ms": 0,
                        "_stale_window_times_ms": [],
                        "_survey_started_at_ms": 0,
                        "_survey_candidate_index": 0,
                        "_survey_completed": True,
                    }
                }
            }
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "site_selector_state.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
            status = {
                "trunk_update": {
                    "systems": {
                        "0": {
                            "system": "MTRTRS",
                            "last_tsbk": 1_000_005.0,
                        }
                    }
                },
                "channel_update": {
                    "0": {
                        "system": "MTRTRS",
                        "tag": "Control Channel",
                        "freq": 856937500,
                    }
                },
                "call_log": [],
            }
            with mock.patch.object(adapter, "_read_active_profile_dir", return_value=str(profile_dir)), \
                mock.patch.object(adapter, "_poll_op25_status", return_value=status), \
                mock.patch.object(adapter, "_handle_selector_restart_requests", return_value=None), \
                mock.patch("ui.op25_adapter.time.time", return_value=1_000_010.0):
                payload = adapter.preflight()

            selected = next(
                row
                for row in payload["op25_site_selection"]["MTRTRS"]["candidate_sites"]
                if row["site_id"] == "18863"
            )
            self.assertEqual("unproductive", selected["state"])
            self.assertEqual("healthy but unproductive", selected["demotion_reason"])

    def test_legacy_single_site_preflight_exposes_valid_site_selection_telemetry(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            profile_dir = tmp_path / "profile"
            runtime_dir = tmp_path / "runtime"
            _write_profile(
                profile_dir,
                systems_payload={
                    "systems": [
                        {
                            "name": "TACN",
                            "control_channels_hz": [769831250],
                        }
                    ]
                },
            )
            adapter = _make_adapter(str(runtime_dir))
            status = {
                "trunk_update": {
                    "systems": {
                        "0": {
                            "system": "TACN",
                            "last_tsbk": 1_000_005.0,
                        }
                    }
                },
                "channel_update": {
                    "0": {
                        "system": "TACN",
                        "tag": "Control Channel",
                        "freq": 769831250,
                    }
                },
                "call_log": [],
            }
            with mock.patch.object(adapter, "_read_active_profile_dir", return_value=str(profile_dir)), \
                mock.patch.object(adapter, "_poll_op25_status", return_value=status), \
                mock.patch.object(adapter, "_handle_selector_restart_requests", return_value=None), \
                mock.patch("ui.op25_adapter.time.time", return_value=1_000_010.0):
                payload = adapter.preflight()

            selection = payload["op25_site_selection"]["TACN"]
            self.assertEqual("legacy:auto", selection["selected_site_id"])
            self.assertEqual("legacy", selection["selection_mode"])
            self.assertTrue(selection["candidate_sites"])

    def test_digital_status_rows_include_selector_fields(self):
        mgr = digital.DigitalManager.__new__(digital.DigitalManager)
        mgr._scheduler_system_health = {}
        mgr._scheduler_last_apply_error = ""
        mgr._scheduler_last_apply_error_system = ""
        mgr._scheduler_last_switch_time_ms = 0
        mgr.backend = mock.Mock(return_value="op25")
        mgr.isActive = mock.Mock(return_value=True)
        mgr.getProfile = mock.Mock(return_value="p1")
        mgr._scheduler_health_entry = digital.DigitalManager._scheduler_health_entry.__get__(mgr, digital.DigitalManager)
        mgr._op25_per_system_control_truth_locked = mock.Mock(
            return_value={
                "mtrtrs": {
                    "control_decode_available": True,
                    "control_locked": True,
                    "control_last_time": 1234,
                }
            }
        )
        rows = mgr._allocation_system_health_payload_locked(
            systems=["MTRTRS"],
            assignments_by_system={"mtrtrs": {"preferred_tuner_serial": "14306619", "role": "control"}},
            strategy="single_system",
            observed_system="",
            preflight={
                "control_decode_available": True,
                "control_channel_locked": True,
                "op25_site_selection": {
                    "MTRTRS": {
                        "selected_site_id": "18863",
                        "selected_site_name": "Davidson County Simulcast",
                        "selection_mode": "survey",
                        "reason_code": "survey_complete_best_score",
                        "reason_text": "Survey completed; retained best scored site Davidson County Simulcast",
                        "last_switch_time": "2026-04-01T00:00:00+00:00",
                        "switch_count": 1,
                        "same_site_restart_count": 0,
                        "site_switch_restart_count": 1,
                        "generic_restart_count": 0,
                        "stale_window_count": 0,
                        "candidate_sites": [
                            {"site_id": "18863", "site_name": "Davidson County Simulcast", "state": "selected", "score": 140}
                        ],
                    }
                },
            },
            now_ms=2_000_000,
            lock_timeout_ms=30_000,
            missing_serials=set(),
            slow_serials=set(),
        )
        self.assertEqual("18863", rows[0]["selected_site_id"])
        self.assertEqual("survey_complete_best_score", rows[0]["reason_code"])
        self.assertEqual(1, rows[0]["site_switch_restart_count"])
        self.assertEqual("selected", rows[0]["candidate_sites"][0]["state"])


if __name__ == "__main__":
    unittest.main()
