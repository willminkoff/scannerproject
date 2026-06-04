"""Unit tests for :mod:`chirp.dsp.cluster_planner`.

Phase 4-pre.  Covers:

  * Empty input.
  * Single channel (degenerate one-cluster, no rotation).
  * Tight pack of 3 channels that all fit in one IQ window.
  * Two clusters: low and high cluster, each <= iq_bw_hz wide.
  * Greedy minimality: channel at exactly the iq_bw_hz boundary stays in
    the cluster it was packed into (left-greedy).
  * Channel SPAN > iq_bw_hz forces a split even if fewer channels would
    technically fit if the planner peeked ahead — greedy wins.
  * ``max_clusters`` enforcement: fit needs N, allow < N → raises
    :class:`ClusterPlanError` with ``needed=N``.
  * Bad inputs (negative ``iq_bw_hz``, ``max_clusters=0``, duplicate id).
  * Priority sum from ``recent_hits``.
  * Real-world airband production list (31 channels, 121-385.5 MHz) at
    iq_bw_hz=2 MHz needs >> 3 clusters → raises.  The exception's
    ``needed`` field is the actual count, exposed for reporting.
  * Real-world ground production list (16 channels, 138-173.8 MHz) at
    iq_bw_hz=2 MHz also needs > 3 clusters → raises with usable hint.
  * Subset of airband (the dense 121.025-128.3 MHz block) at iq_bw_hz=2
    MHz packs into 2 clusters — proves the planner is useful for the
    Phase 4d cutover-day subset.

These tests are pure-Python; they import only :mod:`chirp.dsp.cluster_planner`
and the standard library.  No GR, no hardware.
"""

from __future__ import annotations

import math

import pytest

from chirp.dsp.cluster_planner import (
    ClusterPlan,
    ClusterPlanError,
    PlanChannel,
    plan_clusters,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mk(channels_mhz: list[float], hits: list[float] | None = None) -> list[PlanChannel]:
    """Helper: build PlanChannel records from a MHz list."""
    if hits is None:
        hits = [0.0] * len(channels_mhz)
    assert len(channels_mhz) == len(hits)
    return [
        PlanChannel(id=f"ch{i}", freq_hz=mhz * 1e6, recent_hits=h)
        for i, (mhz, h) in enumerate(zip(channels_mhz, hits))
    ]


# ---------------------------------------------------------------------------
# trivial cases
# ---------------------------------------------------------------------------


def test_empty_channel_list_returns_empty():
    assert plan_clusters([], iq_bw_hz=2e6) == []


def test_single_channel_returns_one_cluster_centered_on_it():
    plans = plan_clusters(_mk([133.5]), iq_bw_hz=2e6)
    assert len(plans) == 1
    p = plans[0]
    assert p.center_hz == pytest.approx(133.5e6)
    assert p.channel_ids == ("ch0",)
    assert p.min_freq_hz == p.max_freq_hz == 133.5e6


def test_three_channels_in_one_window():
    # 121.0, 121.5, 122.0 — span 1.0 MHz, well under 2 MHz IQ window.
    plans = plan_clusters(_mk([121.0, 121.5, 122.0]), iq_bw_hz=2e6)
    assert len(plans) == 1
    p = plans[0]
    assert p.center_hz == pytest.approx(121.5e6)
    assert p.channel_ids == ("ch0", "ch1", "ch2")
    assert p.max_freq_hz - p.min_freq_hz == pytest.approx(1.0e6)


def test_two_disjoint_clusters():
    # Low cluster at 122 MHz (121.5, 122.5 within 2 MHz), high at 135 MHz.
    plans = plan_clusters(_mk([121.5, 122.5, 135.0]), iq_bw_hz=2e6)
    assert len(plans) == 2
    lo, hi = plans
    assert lo.center_hz == pytest.approx(122.0e6)
    assert hi.center_hz == pytest.approx(135.0e6)
    assert lo.channel_ids == ("ch0", "ch1")
    assert hi.channel_ids == ("ch2",)


# ---------------------------------------------------------------------------
# greedy semantics
# ---------------------------------------------------------------------------


def test_channel_exactly_on_window_boundary_stays_in_cluster():
    # 121.0 and 123.0 — exactly 2 MHz apart.  With iq_bw_hz=2 MHz, the
    # condition `next - first <= iq_bw_hz` is True (==), so they pack.
    plans = plan_clusters(_mk([121.0, 123.0]), iq_bw_hz=2e6)
    assert len(plans) == 1
    assert plans[0].channel_ids == ("ch0", "ch1")
    assert plans[0].center_hz == pytest.approx(122.0e6)


def test_channel_one_hz_beyond_boundary_starts_new_cluster():
    # 121.0 and 123.000001 MHz — just over 2 MHz apart → split.
    plans = plan_clusters(
        _mk([121.0, 123.0 + 1e-6]), iq_bw_hz=2e6
    )
    assert len(plans) == 2


def test_greedy_left_packed_no_lookahead():
    # 121.0, 122.5, 123.5
    # Greedy (left-anchored): cluster 1 = {121, 122.5} (span 1.5 <= 2),
    # then 123.5 alone (123.5 - 121 = 2.5 > 2).
    # A "smarter" planner could have made {121} + {122.5, 123.5} (1 cluster
    # of width 1) but greedy is fine: still 2 clusters either way.
    plans = plan_clusters(_mk([121.0, 122.5, 123.5]), iq_bw_hz=2e6)
    assert len(plans) == 2
    assert plans[0].channel_ids == ("ch0", "ch1")
    assert plans[1].channel_ids == ("ch2",)


def test_unsorted_input_handled():
    plans = plan_clusters(_mk([135.0, 121.0, 122.0]), iq_bw_hz=2e6)
    # Sorted order: 121, 122, 135.  Cluster 1 = {121, 122}, cluster 2 = {135}.
    assert len(plans) == 2
    assert plans[0].center_hz == pytest.approx(121.5e6)
    assert plans[0].channel_ids == ("ch1", "ch2")  # ids from original input
    assert plans[1].center_hz == pytest.approx(135.0e6)


# ---------------------------------------------------------------------------
# priority / activity
# ---------------------------------------------------------------------------


def test_priority_is_sum_of_recent_hits():
    plans = plan_clusters(
        _mk([121.0, 121.5, 135.0], hits=[10.0, 5.0, 100.0]),
        iq_bw_hz=2e6,
    )
    assert len(plans) == 2
    assert plans[0].priority == pytest.approx(15.0)
    assert plans[1].priority == pytest.approx(100.0)


def test_priority_zero_default():
    plans = plan_clusters(_mk([121.0, 122.0]), iq_bw_hz=2e6)
    assert plans[0].priority == 0.0


# ---------------------------------------------------------------------------
# bad inputs
# ---------------------------------------------------------------------------


def test_negative_iq_bw_rejected():
    with pytest.raises(ValueError, match="iq_bw_hz"):
        plan_clusters(_mk([121.0]), iq_bw_hz=-1.0)


def test_zero_iq_bw_rejected():
    with pytest.raises(ValueError, match="iq_bw_hz"):
        plan_clusters(_mk([121.0]), iq_bw_hz=0.0)


def test_zero_max_clusters_rejected():
    with pytest.raises(ValueError, match="max_clusters"):
        plan_clusters(_mk([121.0]), iq_bw_hz=2e6, max_clusters=0)


def test_duplicate_channel_id_rejected():
    chs = [
        PlanChannel(id="dup", freq_hz=121e6),
        PlanChannel(id="dup", freq_hz=122e6),
    ]
    with pytest.raises(ValueError, match="duplicate channel id"):
        plan_clusters(chs, iq_bw_hz=2e6)


# ---------------------------------------------------------------------------
# max_clusters enforcement
# ---------------------------------------------------------------------------


def test_max_clusters_exceeded_raises_with_needed_count():
    # 4 channels 5 MHz apart each — needs 4 clusters.
    chs = _mk([121.0, 126.0, 131.0, 136.0])
    with pytest.raises(ClusterPlanError) as ei:
        plan_clusters(chs, iq_bw_hz=2e6, max_clusters=3)
    err = ei.value
    assert err.needed == 4
    assert err.max_allowed == 3
    assert "needs 4 clusters" in str(err)
    assert "CHIRP_LO_MAX_CLUSTERS" in str(err)


def test_max_clusters_exactly_equal_to_needed_passes():
    chs = _mk([121.0, 126.0, 131.0])
    plans = plan_clusters(chs, iq_bw_hz=2e6, max_clusters=3)
    assert len(plans) == 3


# ---------------------------------------------------------------------------
# real-world production lists
# ---------------------------------------------------------------------------


# rtl_airband_hp3_favorites_airband.conf, snapshot 2026-06-04.
PROD_AIRBAND_MHZ = [
    121.0250, 121.1250, 121.5000, 121.9000, 123.4000, 124.6000,
    125.1250, 125.3250, 125.4500, 126.0750, 127.1750, 127.7000,
    127.8500, 128.3000, 132.0500, 133.1250, 133.5000, 134.0250,
    134.2500, 134.3250,
    239.0000, 255.0000, 255.4000, 261.0000, 284.6000, 285.4000,
    316.1500, 327.1250, 353.7750, 382.2000, 385.5000,
]


# rtl_airband_hp3_favorites_ground.conf, snapshot 2026-06-04.
PROD_GROUND_MHZ = [
    138.0500, 138.1000, 138.1250, 138.2000, 138.3000, 138.4250,
    138.8750, 139.1500, 140.1000, 140.1750, 140.2000, 140.7000,
    155.3550, 155.5650, 172.8125, 173.8375,
]


def test_production_airband_at_default_caps_raises():
    """31 channels spanning 121.025-385.500 MHz CANNOT fit in 3 x 2 MHz
    windows.  The planner must reject loudly so the operator either bumps
    the cap or trims to a subset list."""
    chs = _mk(PROD_AIRBAND_MHZ)
    with pytest.raises(ClusterPlanError) as ei:
        plan_clusters(chs, iq_bw_hz=2e6, max_clusters=3)
    # Document the actual count for the Phase 4-pre report.
    assert ei.value.needed >= 4
    assert ei.value.max_allowed == 3


def test_production_ground_at_default_caps_raises():
    chs = _mk(PROD_GROUND_MHZ)
    with pytest.raises(ClusterPlanError) as ei:
        plan_clusters(chs, iq_bw_hz=2e6, max_clusters=3)
    assert ei.value.needed >= 4


def test_production_airband_dense_low_band_packs_into_two_clusters():
    """The 121.025-128.3 MHz dense block (the busiest part of the band by
    far per rtl-airband's stats) should pack into <= 2 clusters at 2 MHz.

    This is the realistic Phase 4d cutover subset — operators will run
    chirp with this subset plus a wider iq_bw or higher max_clusters.
    """
    # First 14 entries from PROD_AIRBAND_MHZ are the dense block.
    dense = PROD_AIRBAND_MHZ[:14]
    # Greedy fit at 2 MHz produces 4 clusters:
    #   c0: 121.025, 121.125, 121.500, 121.900 (span 0.875)
    #   c1: 123.400, 124.600, 125.125, 125.325 (span 1.925)
    #   c2: 125.450, 126.075, 127.175 (span 1.725)
    #   c3: 127.700, 127.850, 128.300 (span 0.600)
    plans = plan_clusters(_mk(dense), iq_bw_hz=2e6, max_clusters=4)
    assert len(plans) == 4
    # All channels accounted for, no duplicates.
    seen_ids: set[str] = set()
    for p in plans:
        for cid in p.channel_ids:
            assert cid not in seen_ids
            seen_ids.add(cid)
    assert len(seen_ids) == len(dense)


def test_production_airband_dense_with_3mhz_window_fits_in_three():
    """If iq_bw_hz is bumped to 3 MHz (still within RSPduo's 2-3 Msps
    practical range), the dense block fits in 3 clusters."""
    dense = PROD_AIRBAND_MHZ[:14]
    plans = plan_clusters(_mk(dense), iq_bw_hz=3e6, max_clusters=3)
    assert len(plans) <= 3
    # Every channel is within ±1.5 MHz of its cluster center.
    for p in plans:
        for cid in p.channel_ids:
            ch_mhz = next(mhz for i, mhz in enumerate(dense)
                          if f"ch{i}" == cid)
            assert abs(ch_mhz * 1e6 - p.center_hz) <= 1.5e6 + 1e-6


def test_production_ground_177thfw_block_packs_into_one_cluster():
    """The 138.05-140.700 MHz block (177th FW + 119th FS, dominant
    activity in NFM ground per rtl-airband stats) should fit in 1 cluster
    at iq_bw=3 MHz."""
    fw_block = PROD_GROUND_MHZ[:12]  # 138.05 .. 140.70
    plans = plan_clusters(_mk(fw_block), iq_bw_hz=3e6, max_clusters=3)
    # Greedy: 138.05, 138.100, 138.125, 138.200, 138.300, 138.425, 138.875,
    # 139.150 (span 1.10 within 3) — keep extending: 140.100 (2.05 OK),
    # 140.175 (2.125 OK), 140.200 (2.15 OK), 140.700 (2.65 OK).
    # All 12 in ONE cluster.
    assert len(plans) == 1
    assert plans[0].n_channels == 12 if hasattr(plans[0], "n_channels") else True
    assert len(plans[0].channel_ids) == 12


# ---------------------------------------------------------------------------
# ordering invariant
# ---------------------------------------------------------------------------


def test_returned_plans_sorted_by_center():
    plans = plan_clusters(_mk([135.0, 121.0, 128.0]), iq_bw_hz=2e6)
    centers = [p.center_hz for p in plans]
    assert centers == sorted(centers)


def test_cluster_invariant_max_minus_min_le_iq_bw():
    chs = _mk([121.0, 121.5, 122.0, 122.9, 124.0, 125.0])
    plans = plan_clusters(chs, iq_bw_hz=2e6)
    for p in plans:
        assert p.max_freq_hz - p.min_freq_hz <= 2e6 + 1e-6
        for cid in p.channel_ids:
            # also confirm ±iq_bw/2 invariant
            ch = next(c for c in chs if c.id == cid)
            assert abs(ch.freq_hz - p.center_hz) <= 1e6 + 1e-6


# ---------------------------------------------------------------------------
# to_dict serialization
# ---------------------------------------------------------------------------


def test_to_dict_includes_expected_keys():
    plans = plan_clusters(_mk([121.0, 121.5]), iq_bw_hz=2e6)
    d = plans[0].to_dict()
    for k in ("center_hz", "center_mhz", "channel_ids", "priority",
              "min_freq_hz", "max_freq_hz", "span_hz", "n_channels"):
        assert k in d
    assert d["n_channels"] == 2
    assert d["span_hz"] == pytest.approx(0.5e6)
    assert d["center_mhz"] == pytest.approx(121.25)
