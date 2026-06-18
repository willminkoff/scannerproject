"""SB6 2026-06-18 global-squelch redesign — schema + state tests.

These cover the parts of the global-squelch model that do NOT need GNU Radio
(the cmd-bus arg validator and the persisted-state field), so they run in any
environment. The daemon flowgraph propagation tests live in
``test_global_squelch.py`` (those import chirp.daemon → gnuradio and run on
the Micro).

Model under test:
  - One band-wide squelch threshold replaces per-channel squelch.
  - cmd ``set_global_squelch_dbfs {dbfs}`` validated by SetGlobalSquelchArgs.
  - ChirpState carries an authoritative ``global_squelch_dbfs`` (Optional);
    an older state file with no such field loads as None so the daemon falls
    back to its config default. Per-channel ChannelState.squelch_dbfs is
    retained for backward-compat reads but is non-authoritative.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from chirp.cmd.schema import COMMAND_ARGS, SetGlobalSquelchArgs, parse_args
from chirp.state import ChirpState, StateStore


# ---------------------------------------------------------------------------
# Schema: SetGlobalSquelchArgs
# ---------------------------------------------------------------------------


class TestSetGlobalSquelchArgs:
    def test_registered_in_command_args(self):
        assert COMMAND_ARGS.get("set_global_squelch_dbfs") is SetGlobalSquelchArgs

    @pytest.mark.parametrize("dbfs", [-120.0, -56.0, -1.0, 0.0])
    def test_accepts_in_range(self, dbfs):
        args = parse_args("set_global_squelch_dbfs", {"dbfs": dbfs})
        assert isinstance(args, SetGlobalSquelchArgs)
        assert args.dbfs == dbfs

    @pytest.mark.parametrize("dbfs", [-120.01, 0.01, 5.0, -200.0])
    def test_rejects_out_of_range(self, dbfs):
        with pytest.raises(ValidationError):
            parse_args("set_global_squelch_dbfs", {"dbfs": dbfs})

    def test_rejects_extra_fields(self):
        # No per-channel id allowed — this is a band-wide cmd.
        with pytest.raises(ValidationError):
            parse_args("set_global_squelch_dbfs", {"dbfs": -56.0, "id": "ch01"})


# ---------------------------------------------------------------------------
# State: ChirpState.global_squelch_dbfs
# ---------------------------------------------------------------------------


class TestChirpStateGlobalSquelch:
    def test_default_is_none(self):
        st = ChirpState()
        assert st.global_squelch_dbfs is None

    def test_round_trips_through_store(self, tmp_path):
        store = StateStore(tmp_path / "airband.state.json")
        store.save(ChirpState(band="airband", global_squelch_dbfs=-62.0))
        back = store.load()
        assert back.global_squelch_dbfs == -62.0

    def test_old_state_file_without_field_loads_as_none(self, tmp_path):
        """Backward-compat: a state file written before the field existed
        (per-channel squelch only, no top-level global) must still load — and
        report None so the daemon falls back to its config default."""
        p = tmp_path / "airband.state.json"
        p.write_text(json.dumps({
            "schema_version": 1,
            "band": "airband",
            "master_gain_db": 0.0,
            "channels": [
                {"id": "ch01", "freq_mhz": 119.35, "mode": "am",
                 "squelch_dbfs": -56.0, "gain_db": 0.0, "label": "JWN"},
            ],
        }), encoding="utf-8")
        st = StateStore(p).load()
        assert st.global_squelch_dbfs is None
        # The legacy per-channel value still parses (non-authoritative).
        assert st.channels[0].squelch_dbfs == -56.0

    def test_rejects_out_of_range_global(self, tmp_path):
        """An on-disk global outside [-120, 0] is corrupt — load() falls back
        to empty state (never raises on load, per the state-store contract)."""
        p = tmp_path / "airband.state.json"
        p.write_text(json.dumps({
            "schema_version": 1, "band": "airband",
            "global_squelch_dbfs": 12.0, "channels": [],
        }), encoding="utf-8")
        st = StateStore(p).load()
        # Corrupt value → empty default state, global back to None.
        assert st.global_squelch_dbfs is None
