"""chirp.cmd.schema — UDP JSON command/response validators (Pydantic v2).

Phase 1 schema, extended in Phase 2. See SDR_DEMOD_DESIGN_2026-06-03.md
Section 5 for the canonical command list.

Phase 1 commands (kept): add_channel, remove_channel, set_squelch, set_freq,
set_gain, get_status.

Phase 2 additions:
  - `add_channel` now accepts a BATCH: either the legacy single-channel arg
    shape OR `{"channels": [ChannelArgs, ...]}`. Both wire through a
    `channels: list[...]` field of length >= 1 internally.
  - `set_master_gain { db }` — adjusts post-mixer trim.
  - `reset { }` — clears all channels + resets state.

Response envelope keeps the `{status, data, error}` tri-state shape from
Phase 1.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Protocol version. Daemon advertises this via get_status.
# Phase 1 = 1. Phase 2 ADDS commands (non-breaking), so version stays at 1.
PROTOCOL_VERSION = 1


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


class Envelope(BaseModel):
    """Outer wire envelope for every command datagram."""

    model_config = ConfigDict(extra="forbid")

    v: int = Field(..., description="Protocol version (1).")
    id: str = Field(..., min_length=1, description="Client-chosen correlation id.")
    cmd: str = Field(..., min_length=1, description="Command name.")
    args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("v")
    @classmethod
    def _check_version(cls, v: int) -> int:
        if v != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol version: {v}")
        return v


# ---------------------------------------------------------------------------
# Response (per Will's Phase-1 prompt)
# ---------------------------------------------------------------------------


ResponseStatus = Literal["ok", "rejected", "error"]


class Response(BaseModel):
    """Response sent back to the requesting client on the same packet.

    `status`:
        - "ok"        — command accepted and applied.
        - "rejected"  — command was well-formed but refused by policy
                        (e.g. unknown channel id, pool full, unsupported mode).
        - "error"     — schema validation failure or internal exception.
    """

    model_config = ConfigDict(extra="forbid")

    v: int = PROTOCOL_VERSION
    id: str
    status: ResponseStatus
    data: Optional[dict[str, Any]] = None
    error: Optional[str] = None

    # NOTE: classmethod names intentionally avoid colliding with the `error`
    # field — pydantic v2 reserves field names on the class namespace.
    @classmethod
    def make_ok(cls, req_id: str, data: Optional[dict[str, Any]] = None) -> "Response":
        return cls(v=PROTOCOL_VERSION, id=req_id, status="ok", data=data, error=None)

    @classmethod
    def make_rejected(cls, req_id: str, reason: str) -> "Response":
        return cls(v=PROTOCOL_VERSION, id=req_id, status="rejected", data=None, error=reason)

    @classmethod
    def make_error(cls, req_id: str, reason: str) -> "Response":
        return cls(v=PROTOCOL_VERSION, id=req_id, status="error", data=None, error=reason)


# ---------------------------------------------------------------------------
# Per-command argument schemas
# ---------------------------------------------------------------------------


class _ArgsBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_id(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("id must be a non-empty string")
    if len(v) > 64:
        raise ValueError("id must be <= 64 chars")
    return v


def _check_squelch(dbfs: float) -> float:
    if not (-120.0 <= dbfs <= 0.0):
        raise ValueError("squelch_dbfs must be in [-120, 0]")
    return float(dbfs)


def _check_gain(db: float) -> float:
    if not (-20.0 <= db <= 40.0):
        raise ValueError("gain_db must be in [-20, 40]")
    return float(db)


def _check_freq_mhz(mhz: float) -> float:
    if not (mhz > 0.0):
        raise ValueError("freq_mhz must be > 0")
    return float(mhz)


class ChannelArgs(_ArgsBase):
    """One channel description. Used both as a standalone request body and
    as an element of an `add_channel` batch."""

    id: str
    freq_mhz: float
    mode: Literal["am"]  # Phase 1/2: AM only; NFM is Phase 4.
    squelch_dbfs: float
    gain_db: float = 0.0
    label: Optional[str] = None

    @field_validator("id")
    @classmethod
    def _v_id(cls, v: str) -> str:
        return _check_id(v)

    @field_validator("freq_mhz")
    @classmethod
    def _v_freq(cls, v: float) -> float:
        return _check_freq_mhz(v)

    @field_validator("squelch_dbfs")
    @classmethod
    def _v_sq(cls, v: float) -> float:
        return _check_squelch(v)

    @field_validator("gain_db")
    @classmethod
    def _v_gain(cls, v: float) -> float:
        return _check_gain(v)


class AddChannelArgs(_ArgsBase):
    """Phase 2 batched form. Wire shape is one of:

        {"id": "...", "freq_mhz": ..., "mode": "am", ...}   # legacy single
        {"channels": [ {ChannelArgs}, ... ]}                # batch

    Both normalise to `self.channels` of length >= 1.
    """

    channels: list[ChannelArgs] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _wrap_single(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "channels" in data:
            # Batch form. Forbid mixing batch + legacy keys.
            other_keys = set(data.keys()) - {"channels"}
            if other_keys:
                raise ValueError(
                    f"add_channel batch form must not mix with single-channel "
                    f"keys: extra={sorted(other_keys)}"
                )
            return data
        # Legacy single-channel form: wrap it.
        return {"channels": [data]}

    @model_validator(mode="after")
    def _non_empty(self) -> "AddChannelArgs":
        if not self.channels:
            raise ValueError("add_channel: channels list must be non-empty")
        # Disallow duplicate ids within a single batch.
        ids = [c.id for c in self.channels]
        if len(set(ids)) != len(ids):
            dups = sorted({x for x in ids if ids.count(x) > 1})
            raise ValueError(f"add_channel batch contains duplicate ids: {dups}")
        return self


class RemoveChannelArgs(_ArgsBase):
    id: str

    @field_validator("id")
    @classmethod
    def _v_id(cls, v: str) -> str:
        return _check_id(v)


class SetSquelchArgs(_ArgsBase):
    id: str
    dbfs: float

    @field_validator("id")
    @classmethod
    def _v_id(cls, v: str) -> str:
        return _check_id(v)

    @field_validator("dbfs")
    @classmethod
    def _v_sq(cls, v: float) -> float:
        return _check_squelch(v)


class SetFreqArgs(_ArgsBase):
    id: str
    mhz: float

    @field_validator("id")
    @classmethod
    def _v_id(cls, v: str) -> str:
        return _check_id(v)

    @field_validator("mhz")
    @classmethod
    def _v_freq(cls, v: float) -> float:
        return _check_freq_mhz(v)


class SetGainArgs(_ArgsBase):
    id: str
    db: float

    @field_validator("id")
    @classmethod
    def _v_id(cls, v: str) -> str:
        return _check_id(v)

    @field_validator("db")
    @classmethod
    def _v_gain(cls, v: float) -> float:
        return _check_gain(v)


class SetMasterGainArgs(_ArgsBase):
    """Phase 2. Post-mixer master gain trim, in dB."""

    db: float

    @field_validator("db")
    @classmethod
    def _v_gain(cls, v: float) -> float:
        return _check_gain(v)


class ResetArgs(_ArgsBase):
    """Phase 2. Clears all channels + zeros master_gain + resets state file."""
    pass


class GetStatusArgs(_ArgsBase):
    pass


class SubscribeArgs(_ArgsBase):
    """Phase 3. Subscribe the source address to async event datagrams.

    `events` filters which event types to receive (empty list = all).
    Subscription persists until the client sends `unsubscribe` or the daemon
    restarts. There is no auto-expiry — short-lived clients should call
    `unsubscribe` before closing.
    """

    events: list[str] = Field(default_factory=list)


class UnsubscribeArgs(_ArgsBase):
    """Phase 3. Removes the source address from the event subscriber set."""
    pass


# Dispatch table: cmd-name → args model. The server uses this to parse args
# defensively before invoking flowgraph callbacks.
COMMAND_ARGS: dict[str, type[_ArgsBase]] = {
    "add_channel": AddChannelArgs,
    "remove_channel": RemoveChannelArgs,
    "set_squelch": SetSquelchArgs,
    "set_freq": SetFreqArgs,
    "set_gain": SetGainArgs,
    "set_master_gain": SetMasterGainArgs,
    "reset": ResetArgs,
    "get_status": GetStatusArgs,
    "subscribe": SubscribeArgs,
    "unsubscribe": UnsubscribeArgs,
}


# ---------------------------------------------------------------------------
# Event stream (daemon → subscribers)
# ---------------------------------------------------------------------------


class Event(BaseModel):
    """Async event pushed to subscribers (no correlation id)."""

    model_config = ConfigDict(extra="allow")

    v: int = PROTOCOL_VERSION
    evt: str
    ts: float


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------


def parse_envelope(raw: Union[str, bytes]) -> Envelope:
    """Parse + validate a raw JSON datagram into an Envelope.

    Raises pydantic.ValidationError on any schema problem (including unknown
    extra fields at the top level).
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8")
    return Envelope.model_validate_json(raw)


def parse_args(cmd: str, args: dict[str, Any]) -> _ArgsBase:
    """Validate args for a known command. Caller has already checked cmd is known."""
    model = COMMAND_ARGS[cmd]
    return model.model_validate(args)


__all__ = [
    "PROTOCOL_VERSION",
    "Envelope",
    "Response",
    "ResponseStatus",
    "ChannelArgs",
    "AddChannelArgs",
    "RemoveChannelArgs",
    "SetSquelchArgs",
    "SetFreqArgs",
    "SetGainArgs",
    "SetMasterGainArgs",
    "ResetArgs",
    "GetStatusArgs",
    "SubscribeArgs",
    "UnsubscribeArgs",
    "COMMAND_ARGS",
    "Event",
    "parse_envelope",
    "parse_args",
]
