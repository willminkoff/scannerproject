"""Phase 2 state-persistence tests.

Covers chirp.state — atomic JSON I/O, schema validation, corruption fallback,
default-path resolution, multi-save atomicity (no partial files).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from chirp.state import (
    STATE_SCHEMA_VERSION,
    ChannelState,
    ChirpState,
    StateStore,
    default_state_path,
)


class TestChannelState:
    def test_round_trip(self):
        ch = ChannelState(id="ch01", freq_mhz=121.025, mode="am",
                          squelch_dbfs=-68.0, gain_db=0.0, label="TWR")
        d = ch.model_dump()
        ch2 = ChannelState.model_validate(d)
        assert ch2 == ch

    def test_rejects_bad_freq(self):
        with pytest.raises(Exception):
            ChannelState(id="x", freq_mhz=-1.0, mode="am", squelch_dbfs=-60.0)

    def test_rejects_bad_squelch(self):
        with pytest.raises(Exception):
            ChannelState(id="x", freq_mhz=121.0, mode="am", squelch_dbfs=-500.0)

    def test_rejects_unknown_mode(self):
        with pytest.raises(Exception):
            ChannelState(id="x", freq_mhz=121.0, mode="wsprlite", squelch_dbfs=-60.0)


class TestStateStore:
    def test_load_missing_file_returns_empty(self, tmp_path):
        s = StateStore(tmp_path / "does-not-exist.json").load()
        assert isinstance(s, ChirpState)
        assert s.channels == []
        assert s.master_gain_db == 0.0
        assert s.schema_version == STATE_SCHEMA_VERSION

    def test_save_then_load_round_trip(self, tmp_path):
        store = StateStore(tmp_path / "a.json")
        state = ChirpState(
            band="airband",
            master_gain_db=3.5,
            channels=[
                ChannelState(id="tower", freq_mhz=121.025, mode="am",
                             squelch_dbfs=-68.0, gain_db=0.0, label="TWR"),
                ChannelState(id="ground", freq_mhz=121.9, mode="am",
                             squelch_dbfs=-66.0, gain_db=-2.0, label="GND"),
            ],
            presets={"airband_31": "default"},
        )
        store.save(state)
        got = store.load()
        assert got.band == "airband"
        assert got.master_gain_db == 3.5
        assert len(got.channels) == 2
        assert got.channels[0].id == "tower"
        assert got.presets == {"airband_31": "default"}

    def test_save_is_atomic(self, tmp_path):
        """The save path must not leave a half-written file behind even if
        the rename target already exists. We can't easily simulate a crash,
        but we CAN assert: file always parses cleanly after every save."""
        store = StateStore(tmp_path / "b.json")
        for i in range(50):
            st = ChirpState(channels=[
                ChannelState(id=f"ch{j:02d}", freq_mhz=120.0 + j * 0.025,
                             mode="am", squelch_dbfs=-60.0)
                for j in range(i % 12 + 1)
            ])
            store.save(st)
            # Always parseable. No partial JSON ever.
            assert json.loads(store.path.read_text())

    def test_load_corrupt_json_falls_back_to_empty(self, tmp_path, caplog):
        p = tmp_path / "corrupt.json"
        p.write_text("{not valid json at all")
        s = StateStore(p).load()
        assert s.channels == []
        assert any("corrupt" in r.message.lower() for r in caplog.records)

    def test_load_empty_file_falls_back_to_empty(self, tmp_path):
        p = tmp_path / "empty.json"
        p.write_text("")
        s = StateStore(p).load()
        assert s.channels == []

    def test_load_schema_violation_falls_back_to_empty(self, tmp_path):
        p = tmp_path / "bad-schema.json"
        # squelch out of bounds → ValidationError → fallback to empty.
        p.write_text(json.dumps({
            "schema_version": STATE_SCHEMA_VERSION,
            "band": "airband",
            "master_gain_db": 0.0,
            "channels": [{"id": "x", "freq_mhz": 121.0, "mode": "am",
                          "squelch_dbfs": -999.0, "gain_db": 0.0, "label": None}],
            "presets": {},
        }))
        s = StateStore(p).load()
        assert s.channels == []

    def test_load_future_schema_version_still_loads(self, tmp_path, caplog):
        """Forward-compat: a daemon should not refuse to boot just because
        the state file came from a newer version. We log + accept."""
        p = tmp_path / "future.json"
        p.write_text(json.dumps({
            "schema_version": STATE_SCHEMA_VERSION + 99,
            "band": "airband",
            "master_gain_db": 0.0,
            "channels": [],
            "presets": {},
        }))
        s = StateStore(p).load()
        assert s.channels == []

    def test_load_ignores_unknown_top_level_keys(self, tmp_path):
        """Forward-compat: extra=ignore lets a newer file's extra fields
        pass through without breaking the parse."""
        p = tmp_path / "extra.json"
        p.write_text(json.dumps({
            "schema_version": STATE_SCHEMA_VERSION,
            "band": "airband",
            "master_gain_db": 0.0,
            "channels": [],
            "presets": {},
            "future_field_we_dont_know_about": {"x": 1},
        }))
        s = StateStore(p).load()
        # Loaded successfully — the unknown key was dropped.
        assert s.band == "airband"

    def test_clear_resets_to_empty(self, tmp_path):
        store = StateStore(tmp_path / "c.json")
        store.save(ChirpState(channels=[
            ChannelState(id="x", freq_mhz=121.0, mode="am", squelch_dbfs=-60.0),
        ]))
        assert len(store.load().channels) == 1
        store.clear()
        assert store.load().channels == []

    def test_save_creates_parent_dirs(self, tmp_path):
        store = StateStore(tmp_path / "deeply" / "nested" / "path" / "x.json")
        store.save(ChirpState())
        assert store.path.is_file()

    def test_no_orphan_tmp_files_on_success(self, tmp_path):
        store = StateStore(tmp_path / "ok.json")
        for _ in range(5):
            store.save(ChirpState())
        # After successful saves, only the final file should exist (no
        # leftover .tmp files in the directory).
        contents = list(tmp_path.iterdir())
        assert len(contents) == 1
        assert contents[0].name == "ok.json"


class TestDefaultPath:
    def test_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CHIRP_STATE_PATH", str(tmp_path / "x.json"))
        assert default_state_path("airband") == tmp_path / "x.json"

    def test_no_override_uses_band(self, monkeypatch):
        monkeypatch.delenv("CHIRP_STATE_PATH", raising=False)
        assert default_state_path("airband") == Path("/var/lib/chirp/airband.state.json")
        assert default_state_path("ground") == Path("/var/lib/chirp/ground.state.json")
