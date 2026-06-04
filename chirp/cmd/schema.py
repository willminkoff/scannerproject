"""chirp.cmd.schema — UDP JSON command/response validators (Pydantic v2).

Phase 1 schema. See SDR_DEMOD_DESIGN_2026-06-03.md Section 5 for the canonical
command list and event stream definitions. Phase 1 implements a subset:

    add_channel, remove_channel, set_squelch, set_freq, set_gain, get_status

Reject `mode != "am"` in Phase 1 (NFM is Phase 2/4). The `set_mode` runtime
command is reserved by the design doc but not implemented in Phase 1.

Response envelope intentionally differs from the design doc's `{ok, result|error}`
shape: Will's Phase-1 prompt specified `{status, data, error}` with a tri-state
status field (`"ok" | "rejected" | "error"`). Both shapes carry the same
information; the reconciliation is flagged in PROGRESS.md for Will to resolve
before Phase 4 cutover (clients are required by spec to ignore unknown fields,
so a forward-compat bridge is cheap).
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Protocol version. Daemon advertises this via get_status.
# Breaking changes bump this; non-breaking additions do not.
PROTOCOL_VERSION = 1


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


class Envelope(BaseModel):
    """Outer wire envelope for every command datagram."""

    model_config = ConfigDict(extra="forbid")

    v: int = Field(..., description="Protocol version (1 in Phase 1).")
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


class AddChannelArgs(_ArgsBase):
    id: str
    freq_mhz: float
    mode: Literal["am"]  # Phase 1: AM only; NFM is Phase 2/4.
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


class GetStatusArgs(_ArgsBase):
    pass


# Dispatch table: cmd-name → args model. The server uses this to parse args
# defensively before invoking flowgraph callbacks.
COMMAND_ARGS: dict[str, type[_ArgsBase]] = {
    "add_channel": AddChannelArgs,
    "remove_channel": RemoveChannelArgs,
    "set_squelch": SetSquelchArgs,
    "set_freq": SetFreqArgs,
    "set_gain": SetGainArgs,
    "get_status": GetStatusArgs,
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
    "AddChannelArgs",
    "RemoveChannelArgs",
    "SetSquelchArgs",
    "SetFreqArgs",
    "SetGainArgs",
    "GetStatusArgs",
    "COMMAND_ARGS",
    "Event",
    "parse_envelope",
    "parse_args",
]
