"""chirp.state — atomic JSON state persistence for daemon channel pool.

Phase 2. Persists the live channel list (id, freq_mhz, squelch_dbfs, gain_db,
label, mode) plus a free-form `presets` map and a `master_gain_db`.

Design constraints (from SDR_DEMOD_DESIGN_2026-06-03.md §6):

  - Path resolved from CHIRP_STATE_PATH or /var/lib/chirp/<band>.state.json.
  - Atomic writes: open tmp in same dir, fsync, rename. Crash-safe.
  - Reads validate the schema; corruption falls back to empty state and
    logs a warning. Never raises on load — boot must succeed.
  - Schema versioned (`schema`) so we can evolve without losing data.

Public surface:
    ChirpState         — pydantic model (frozen=False; mutated by daemon)
    StateStore         — wraps a path + load() / save() / clear()
    default_state_path(band) → Path
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger("chirp.state")

# Bump only on breaking schema changes. Non-breaking additions don't bump.
STATE_SCHEMA_VERSION = 1


class ChannelState(BaseModel):
    """Persisted shape of a single channel entry."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=64)
    freq_mhz: float
    mode: str = "am"
    squelch_dbfs: float = -60.0
    gain_db: float = 0.0
    label: Optional[str] = None
    # SB5 2026-06-11 VAD persistence: per-channel voice-activity gate
    # threshold (0..100) and bypass flag.  Defaults match daemon defaults
    # so older state files continue to load unchanged.
    vad_threshold: float = 50.0
    vad_bypass: bool = False

    @field_validator("freq_mhz")
    @classmethod
    def _v_freq(cls, v: float) -> float:
        if not (v > 0.0):
            raise ValueError("freq_mhz must be > 0")
        return float(v)

    @field_validator("squelch_dbfs")
    @classmethod
    def _v_sq(cls, v: float) -> float:
        if not (-120.0 <= v <= 0.0):
            raise ValueError("squelch_dbfs must be in [-120, 0]")
        return float(v)

    @field_validator("gain_db")
    @classmethod
    def _v_gain(cls, v: float) -> float:
        if not (-20.0 <= v <= 40.0):
            raise ValueError("gain_db must be in [-20, 40]")
        return float(v)

    @field_validator("vad_threshold")
    @classmethod
    def _v_vad_th(cls, v: float) -> float:
        if not (0.0 <= v <= 100.0):
            raise ValueError("vad_threshold must be in [0, 100]")
        return float(v)

    @field_validator("mode")
    @classmethod
    def _v_mode(cls, v: str) -> str:
        if v not in ("am", "nfm"):
            # Persisted shape is permissive (nfm reserved for Phase 4) — but
            # caller code is responsible for rejecting unsupported modes at
            # the apply boundary.
            raise ValueError(f"unsupported mode: {v!r}")
        return v


class ChirpState(BaseModel):
    """Top-level persisted state. Forward-compatible: extra fields allowed
    so a newer daemon can read an even-newer file without losing data on
    rewrite. We strip unknown fields silently on next save."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = STATE_SCHEMA_VERSION
    band: str = "airband"
    master_gain_db: float = 0.0
    channels: list[ChannelState] = Field(default_factory=list)
    presets: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def _v_schema(cls, v: int) -> int:
        if v != STATE_SCHEMA_VERSION:
            # We don't hard-fail on version skew so a daemon upgrade can still
            # boot — but we record the mismatch so the caller can warn.
            log.warning(
                "chirp.state schema_version=%d != expected %d (continuing)",
                v, STATE_SCHEMA_VERSION,
            )
        return int(v)

    @field_validator("master_gain_db")
    @classmethod
    def _v_master(cls, v: float) -> float:
        if not (-20.0 <= v <= 40.0):
            raise ValueError("master_gain_db must be in [-20, 40]")
        return float(v)


def default_state_path(band: str = "airband") -> Path:
    """Resolve the on-disk path. CHIRP_STATE_PATH overrides everything."""
    override = os.environ.get("CHIRP_STATE_PATH")
    if override:
        return Path(override)
    return Path("/var/lib/chirp") / f"{band}.state.json"


class StateStore:
    """Tiny wrapper around a state file. Thread-safe for the daemon's pattern
    (single-writer; concurrent readers tolerated because save is atomic)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # -- load --------------------------------------------------------------

    def load(self) -> ChirpState:
        """Read + validate. Returns empty default state on missing/corrupt."""
        if not self.path.is_file():
            log.info("state file %s missing — starting empty", self.path)
            return ChirpState()
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("state file %s unreadable (%s) — starting empty", self.path, e)
            return ChirpState()
        if not raw.strip():
            log.warning("state file %s empty — starting empty", self.path)
            return ChirpState()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("state file %s corrupt JSON (%s) — starting empty", self.path, e)
            return ChirpState()
        try:
            return ChirpState.model_validate(data)
        except ValidationError as e:
            log.warning(
                "state file %s schema validation failed (%s) — starting empty",
                self.path, _short_validation_msg(e),
            )
            return ChirpState()

    # -- save --------------------------------------------------------------

    def save(self, state: ChirpState) -> None:
        """Atomic write: tmp file in same dir, fsync, rename. Creates parent dirs."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = state.model_dump_json(indent=2).encode("utf-8")
        # Use NamedTemporaryFile in the destination dir so rename() is atomic
        # on the same filesystem (POSIX guarantee).
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    # Best-effort: fsync may fail on overlayfs / tmpfs.
                    pass
            os.replace(tmp_path, self.path)
        except Exception:
            # Clean up tmp on failure so we don't leak.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def clear(self) -> None:
        """Reset to empty state and persist."""
        self.save(ChirpState())


def _short_validation_msg(ve: ValidationError) -> str:
    errs = ve.errors()
    if not errs:
        return "validation failed"
    first = errs[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = first.get("msg", "invalid")
    return f"{loc}: {msg}" if loc else msg


__all__ = [
    "STATE_SCHEMA_VERSION",
    "ChannelState",
    "ChirpState",
    "StateStore",
    "default_state_path",
]
