"""Unit tests for chirp/scripts/migrate_state.py."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "chirp" / "scripts" / "migrate_state.py"

# Make sure the script's parent on sys.path so we can import it.
sys.path.insert(0, str(REPO / "chirp" / "scripts"))
import migrate_state as ms  # noqa: E402


# --- fixtures ---------------------------------------------------------------


@pytest.fixture()
def sample_hp_state(tmp_path) -> Path:
    """Two activated favorites (one per band) + a small custom_favorites
    list spanning the airband/ground boundary."""
    state = {
        "favorites": [
            {"id": "fav-air", "label": "Airband Primary",
             "enabled_air": True, "enabled_ground": False},
            {"id": "fav-gnd", "label": "Ground Primary",
             "enabled_air": False, "enabled_ground": True},
            {"id": "fav-other", "label": "Other",
             "enabled_air": False, "enabled_ground": False},
        ],
        "custom_favorites": [
            {"id": "freq:121.5", "frequency": 121.5,
             "alpha_tag": "Emergency"},
            {"id": "freq:125.325", "frequency": 125.325,
             "alpha_tag": "PHL Tower"},
            {"id": "freq:134.325", "frequency": 134.325,
             "alpha_tag": "Center"},
            {"id": "freq:138.05", "frequency": 138.05,
             "alpha_tag": "177th Tactical"},
            {"id": "freq:138.1", "frequency": 138.1,
             "alpha_tag": "177th Ops"},
            {"id": "bad", "frequency": "not-a-number",
             "alpha_tag": "Junk"},
        ],
    }
    p = tmp_path / "hp_state.json"
    p.write_text(json.dumps(state))
    return p


@pytest.fixture()
def sample_controls(tmp_path) -> Path:
    controls = {
        "version": 1,
        "targets": {
            "airband": {
                "override": {
                    "gain": 32.8,
                    "squelch_auto": True,
                    "squelch_dbfs": -55.0,
                    "squelch_mode": "dbfs",
                    "squelch_preset": "balanced",
                    "squelch_preset_margin_db": 6.0,
                    "squelch_preset_noise_floor_dbfs": -61.0,
                    "squelch_preset_computed_at_ms": 1780000000000,
                    "squelch_tracker_applied_at_ms": 1779999999000,
                    "squelch_snr": 10.0,
                },
                "profile_path": "/dev/null",
                "updated_at_ms": 1780000000000,
            },
            "ground": {
                "override": {
                    "gain": 28.0,
                    "squelch_auto": False,
                    "squelch_dbfs": -34.0,
                    "squelch_mode": "dbfs",
                    "squelch_preset": "sensitive",
                    "squelch_preset_margin_db": 3.0,
                    "squelch_preset_noise_floor_dbfs": -37.0,
                    "squelch_preset_computed_at_ms": 1780000000000,
                    "squelch_snr": 10.0,
                },
                "profile_path": "/dev/null",
                "updated_at_ms": 1780000000000,
            },
        },
    }
    p = tmp_path / "managed_analog_controls.json"
    p.write_text(json.dumps(controls))
    return p


# --- unit tests for the building blocks -------------------------------------


def test_active_favorite_picks_per_band():
    hp = {"favorites": [
        {"id": "a", "enabled_air": True, "enabled_ground": False},
        {"id": "b", "enabled_air": False, "enabled_ground": True},
    ]}
    assert ms.active_favorite(hp, "airband")["id"] == "a"
    assert ms.active_favorite(hp, "ground")["id"] == "b"


def test_active_favorite_none_when_no_activation():
    hp = {"favorites": [
        {"id": "a", "enabled_air": False, "enabled_ground": False},
    ]}
    assert ms.active_favorite(hp, "airband") is None
    assert ms.active_favorite(hp, "ground") is None


def test_custom_favorite_freqs_filters_by_band(sample_hp_state):
    hp = json.loads(sample_hp_state.read_text())
    air = ms.custom_favorite_freqs(hp, "airband")
    gnd = ms.custom_favorite_freqs(hp, "ground")
    air_freqs = sorted([c["freq_mhz"] for c in air])
    gnd_freqs = sorted([c["freq_mhz"] for c in gnd])
    assert air_freqs == [121.5, 125.325, 134.325]
    assert gnd_freqs == [138.05, 138.1]
    # Mode tagging
    assert all(c["mode"] == "am" for c in air)
    assert all(c["mode"] == "nfm" for c in gnd)
    # Junk row dropped
    ids = {c["id"] for c in air + gnd}
    assert "bad" not in ids


def test_custom_favorite_freqs_truncates_long_ids():
    long_id = "x" * 200
    hp = {"custom_favorites": [{
        "id": long_id, "frequency": 121.5, "alpha_tag": "x",
    }]}
    out = ms.custom_favorite_freqs(hp, "airband")
    assert len(out[0]["id"]) <= 64


def test_load_preset_override_missing_returns_empty(tmp_path):
    assert ms.load_preset_override(tmp_path / "nope.json", "airband") == {}


def test_load_preset_override_reads_block(sample_controls):
    ov = ms.load_preset_override(sample_controls, "airband")
    assert ov["squelch_preset"] == "balanced"
    assert ov["gain"] == 32.8


# --- build_state ------------------------------------------------------------


def test_build_state_includes_preset_metadata(sample_hp_state, sample_controls):
    hp = json.loads(sample_hp_state.read_text())
    ov = ms.load_preset_override(sample_controls, "airband")
    st = ms.build_state("airband", hp, ov)
    assert st["band"] == "airband"
    assert st["schema_version"] == 1
    assert st["master_gain_db"] == 0.0
    assert st["presets"]["squelch_preset"] == "balanced"
    assert st["presets"]["squelch_preset_margin_db"] == 6.0
    assert len(st["channels"]) == 3
    for c in st["channels"]:
        assert c["mode"] == "am"
        # Phase 4d: per-channel ``gain_db`` is an audio TRIM, not the
        # SDR RF gain.  Migration no longer copies the override's RF
        # ``gain`` field (32.8 dB) into per-channel state — that value
        # belongs in the per-band sdr.gain_db config.
        assert c["gain_db"] == 0.0
        assert c["squelch_dbfs"] == -55.0


def test_build_state_ground_uses_nfm(sample_hp_state, sample_controls):
    hp = json.loads(sample_hp_state.read_text())
    ov = ms.load_preset_override(sample_controls, "ground")
    st = ms.build_state("ground", hp, ov)
    assert all(c["mode"] == "nfm" for c in st["channels"])
    # Phase 4d: per-channel gain_db is audio trim, defaults to 0.0
    # regardless of the operator's RF-gain override.
    assert all(c["gain_db"] == 0.0 for c in st["channels"])
    assert all(c["squelch_dbfs"] == -34.0 for c in st["channels"])


def test_build_state_no_freqs_returns_empty_channels(tmp_path, sample_controls):
    hp = {"favorites": [], "custom_favorites": []}
    ov = ms.load_preset_override(sample_controls, "airband")
    st = ms.build_state("airband", hp, ov)
    assert st["channels"] == []


# --- idempotency ------------------------------------------------------------


def test_state_matches_byte_equiv(sample_hp_state, sample_controls):
    hp = json.loads(sample_hp_state.read_text())
    ov = ms.load_preset_override(sample_controls, "airband")
    planned = ms.build_state("airband", hp, ov)
    # Existing == planned -> match
    assert ms.state_matches(planned, planned) is True


def test_state_matches_ignores_extra_keys(sample_hp_state, sample_controls):
    """A state file with daemon-side ephemeral fields (e.g.
    last_squelch_dbfs) should still compare equal to the planned shape
    as long as the channel set + presets match."""
    hp = json.loads(sample_hp_state.read_text())
    ov = ms.load_preset_override(sample_controls, "airband")
    planned = ms.build_state("airband", hp, ov)
    existing = dict(planned)
    existing["last_runtime_marker"] = 12345
    existing["channels"] = [dict(c, runtime_x="ignored") for c in planned["channels"]]
    assert ms.state_matches(planned, existing) is True


def test_state_matches_detects_channel_diff(sample_hp_state, sample_controls):
    hp = json.loads(sample_hp_state.read_text())
    ov = ms.load_preset_override(sample_controls, "airband")
    planned = ms.build_state("airband", hp, ov)
    existing = dict(planned)
    existing["channels"] = planned["channels"][:1]  # missing 2
    assert ms.state_matches(planned, existing) is False


def test_state_matches_none_existing(sample_hp_state, sample_controls):
    hp = json.loads(sample_hp_state.read_text())
    ov = ms.load_preset_override(sample_controls, "airband")
    planned = ms.build_state("airband", hp, ov)
    assert ms.state_matches(planned, None) is False


# --- CLI: dry-run -----------------------------------------------------------


def _run_cli(*args, env=None):
    env_full = dict(os.environ)
    if env:
        env_full.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + list(args),
        capture_output=True, text=True, env=env_full, timeout=30,
    )


def test_cli_dry_run_makes_no_writes(tmp_path, sample_hp_state, sample_controls):
    state_dir = tmp_path / "var-lib-chirp"
    out = _run_cli(
        "--dry-run",
        "--hp-state", str(sample_hp_state),
        "--controls", str(sample_controls),
        "--state-dir", str(state_dir),
    )
    assert out.returncode == 0
    # No files created
    assert not state_dir.exists() or list(state_dir.iterdir()) == []
    assert "DRY-RUN" in out.stdout
    assert "no changes written" in out.stdout.lower()


def test_cli_dry_run_is_default_mode(tmp_path, sample_hp_state, sample_controls):
    """Run without --dry-run or --apply -> dry-run mode."""
    state_dir = tmp_path / "var-lib-chirp"
    out = _run_cli(
        "--hp-state", str(sample_hp_state),
        "--controls", str(sample_controls),
        "--state-dir", str(state_dir),
    )
    assert out.returncode == 0
    assert "DRY-RUN" in out.stdout
    assert not state_dir.exists() or list(state_dir.iterdir()) == []


def test_cli_apply_writes_both_files(tmp_path, sample_hp_state, sample_controls):
    state_dir = tmp_path / "var-lib-chirp"
    out = _run_cli(
        "--apply",
        "--hp-state", str(sample_hp_state),
        "--controls", str(sample_controls),
        "--state-dir", str(state_dir),
    )
    assert out.returncode == 0, out.stderr
    air = state_dir / "airband.state.json"
    gnd = state_dir / "ground.state.json"
    assert air.exists()
    assert gnd.exists()
    air_state = json.loads(air.read_text())
    gnd_state = json.loads(gnd.read_text())
    assert air_state["band"] == "airband"
    assert gnd_state["band"] == "ground"
    assert len(air_state["channels"]) == 3
    assert len(gnd_state["channels"]) == 2


def test_cli_idempotent_second_apply_no_writes(tmp_path, sample_hp_state, sample_controls):
    """Second --apply against unchanged inputs must be a no-op."""
    state_dir = tmp_path / "var-lib-chirp"
    args = (
        "--apply",
        "--hp-state", str(sample_hp_state),
        "--controls", str(sample_controls),
        "--state-dir", str(state_dir),
    )
    # First run — writes
    r1 = _run_cli(*args)
    assert r1.returncode == 0
    air = state_dir / "airband.state.json"
    mtime1 = air.stat().st_mtime
    # Sleep a tick so mtime would change if we re-wrote
    import time as _t
    _t.sleep(0.05)
    # Second run — must be no-op
    r2 = _run_cli(*args)
    assert r2.returncode == 0
    mtime2 = air.stat().st_mtime
    assert mtime1 == mtime2, "second --apply should not rewrite the file"
    assert "no writes needed" in r2.stdout.lower()


def test_cli_missing_hp_state_returns_1(tmp_path, sample_controls):
    out = _run_cli(
        "--apply",
        "--hp-state", str(tmp_path / "nonexistent.json"),
        "--controls", str(sample_controls),
        "--state-dir", str(tmp_path / "out"),
    )
    assert out.returncode == 1
    assert "hp_state file not found" in out.stderr


def test_cli_output_schema_matches_chirp_state():
    """The output must be loadable by chirp.state.StateStore on the
    daemon side.  Spot-check the schema_version + required keys."""
    # Don't run the CLI; just call build_state and validate.
    hp = {"favorites": [], "custom_favorites": [
        {"id": "x", "frequency": 121.5, "alpha_tag": "T"},
    ]}
    st = ms.build_state("airband", hp, {})
    for k in ("schema_version", "band", "master_gain_db", "channels", "presets"):
        assert k in st
    for c in st["channels"]:
        for k in ("id", "freq_mhz", "mode", "squelch_dbfs", "gain_db", "label"):
            assert k in c


def test_atomic_write_creates_dirs(tmp_path):
    p = tmp_path / "a" / "b" / "c.json"
    ms.atomic_write(p, '{"hello": "world"}')
    assert p.exists()
    assert json.loads(p.read_text()) == {"hello": "world"}


def test_atomic_write_no_partial_on_overwrite(tmp_path):
    """If a write happens, the file is fully replaced — no half-written
    content visible to a concurrent reader."""
    p = tmp_path / "x.json"
    ms.atomic_write(p, "first")
    ms.atomic_write(p, '{"second":  true}')
    assert p.read_text() == '{"second":  true}'
    # tmp file cleaned up
    assert not (tmp_path / ".x.json.tmp").exists()
