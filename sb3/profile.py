"""sb3.profile — the SB3 profile schema, loader, and validator.

A profile is a declarative statement of what one SB3 role should have SDRangel
(or, later, SDRTrunk) doing: which device, tuned where, with which channels,
streaming to which mount. SB3 *asserts* a profile onto the backend and then
leaves — it never holds the audio state itself (§4.2).

The validator is deliberately strict and fail-CLOSED (§4.6): a profile that is
internally contradictory is rejected here, at load, with a specific reason —
never half-applied and discovered on the box. The most important contradiction
it catches is the **camp-mode baseband fit**: every channel must land inside the
one RF window the device actually delivers. A channel list that spills outside
that window is the classic "looks fine in JSON, silently drops audio on the
hardware" bug, so it is a hard error, computed from the numbers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

VALID_ROLES = {"air", "ground", "digital", "acars", "survey", "disco"}
VALID_MODES = {"camp", "hunt"}
VALID_DEMODS = {"AM", "NFM", "WFM", "SSB", "DSB"}

# SDRangel channel type + settings-key per demod (the REST shapes).
DEMOD_CHANNEL = {
    "AM": ("AMDemod", "AMDemodSettings"),
    "NFM": ("NFMDemod", "NFMDemodSettings"),
    "WFM": ("WFMDemod", "WFMDemodSettings"),
    "SSB": ("SSBDemod", "SSBDemodSettings"),
    "DSB": ("DSBDemod", "DSBDemodSettings"),
}

# SDRangel device settings key per hardware id.
HW_SETTINGS_KEY = {
    "RTLSDR": "rtlSdrSettings",
    "SDRplayV3": "sdrPlayV3Settings",
    "HackRF": "hackRFInputSettings",
}


class ProfileError(Exception):
    """A profile is missing, malformed, or internally contradictory.

    Carries a stable code + human detail, matching broker/policy.py's hard-fail
    convention. A profile problem is a refusal to act, never a silent default.
    """

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}")


@dataclass(frozen=True)
class Channel:
    freq_hz: int
    title: str
    demod: str
    rf_bw_hz: int
    squelch_db: float
    volume: float
    keepalive: bool = False

    def offset_from(self, center_hz: int) -> int:
        return self.freq_hz - center_hz


@dataclass(frozen=True)
class Profile:
    name: str
    role: str
    mode: str
    deviceset_index: int
    # device
    hardware_id: str
    serial: str
    center_freq_hz: int
    sample_rate_hz: int
    log2_decim: int
    gain_tenths_db: int
    agc: bool
    dc_block: bool
    # channels + egress
    channels: List[Channel]
    audio_strategy: str
    udp_address: str
    udp_port: int
    mount: str
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    # -- derived ----------------------------------------------------------

    @property
    def baseband_hz(self) -> int:
        """RF window the device actually delivers to the channelizer."""
        return self.sample_rate_hz // (2 ** self.log2_decim)

    @property
    def half_window_hz(self) -> float:
        return self.baseband_hz / 2.0

    @property
    def channel_type(self) -> str:
        return DEMOD_CHANNEL[self.channels[0].demod][0]

    @property
    def settings_key(self) -> str:
        return DEMOD_CHANNEL[self.channels[0].demod][1]

    @property
    def device_settings_key(self) -> str:
        return HW_SETTINGS_KEY[self.hardware_id]

    @property
    def keepalive_channels(self) -> List[Channel]:
        return [c for c in self.channels if c.keepalive]

    def channels_apply_order(self) -> List[Channel]:
        """Canonical apply order: keepalive channels FIRST, then the rest.

        The mount must come up on an always-open keepalive before any squelched
        real channel can gate it closed (§ translator invariants).
        """
        return (self.keepalive_channels
                + [c for c in self.channels if not c.keepalive])


# ---------------------------------------------------------------------------
# loading + validation
# ---------------------------------------------------------------------------

def load_profile(path: Path) -> Profile:
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise ProfileError("profile-missing", f"no profile at {path}")
    except (ValueError, OSError) as exc:
        raise ProfileError("profile-unreadable", f"{path}: {exc}")
    return parse_profile(data, path=str(path))


def _req(d: Dict, key: str, ctx: str):
    if key not in d:
        raise ProfileError("profile-missing-key", f"{ctx}: required key {key!r} missing")
    return d[key]


def parse_profile(data: Any, path: str = "<data>") -> Profile:
    if not isinstance(data, dict):
        raise ProfileError("profile-bad-shape", f"{path}: top level must be an object")

    name = _req(data, "name", path)
    role = _req(data, "role", path)
    mode = _req(data, "mode", path)
    if role not in VALID_ROLES:
        raise ProfileError("profile-bad-role",
                           f"{path}: role {role!r} not in {sorted(VALID_ROLES)}")
    if mode not in VALID_MODES:
        raise ProfileError("profile-bad-mode",
                           f"{path}: mode {mode!r} not in {sorted(VALID_MODES)}")

    dev = _req(data, "device", path)
    hw = _req(dev, "hardware_id", f"{path}.device")
    if hw not in HW_SETTINGS_KEY:
        raise ProfileError("profile-bad-hw",
                           f"{path}: hardware_id {hw!r} not in {sorted(HW_SETTINGS_KEY)}")

    raw_channels = _req(data, "channels", path)
    if not isinstance(raw_channels, list) or not raw_channels:
        raise ProfileError("profile-no-channels", f"{path}: channels must be a non-empty list")

    channels: List[Channel] = []
    for i, ch in enumerate(raw_channels):
        cctx = f"{path}.channels[{i}]"
        demod = _req(ch, "demod", cctx)
        if demod not in VALID_DEMODS:
            raise ProfileError("profile-bad-demod",
                               f"{cctx}: demod {demod!r} not in {sorted(VALID_DEMODS)}")
        channels.append(Channel(
            freq_hz=int(_req(ch, "freq_hz", cctx)),
            title=str(_req(ch, "title", cctx)),
            demod=demod,
            rf_bw_hz=int(_req(ch, "rf_bw_hz", cctx)),
            squelch_db=float(_req(ch, "squelch_db", cctx)),
            volume=float(_req(ch, "volume", cctx)),
            keepalive=bool(ch.get("keepalive", False)),
        ))

    audio = data.get("audio_device", {})
    udp = data.get("copy_to_udp", {})

    prof = Profile(
        name=str(name), role=role, mode=mode,
        deviceset_index=int(data.get("deviceset_index", 0)),
        hardware_id=hw,
        serial=str(_req(dev, "serial", f"{path}.device")),
        center_freq_hz=int(_req(dev, "center_freq_hz", f"{path}.device")),
        sample_rate_hz=int(_req(dev, "sample_rate_hz", f"{path}.device")),
        log2_decim=int(dev.get("log2_decim", 0)),
        gain_tenths_db=int(dev.get("gain_tenths_db", 0)),
        agc=bool(dev.get("agc", False)),
        dc_block=bool(dev.get("dc_block", False)),
        channels=channels,
        audio_strategy=str(audio.get("strategy", "dynamic_idx0")),
        udp_address=str(udp.get("address", "127.0.0.1")),
        udp_port=int(udp.get("port", 9998)),
        mount=str(_req(data, "mount", path)),
        raw=data,
    )
    _validate(prof, path)
    return prof


def _validate(p: Profile, path: str) -> None:
    # one demod family per deviceset (SDRangel: a deviceset's channels share the
    # channelizer; mixing AM and NFM in one camp window is not what this models).
    demods = {c.demod for c in p.channels}
    if len(demods) != 1:
        raise ProfileError("profile-mixed-demods",
                           f"{path}: all channels in one profile must share a demod; "
                           f"got {sorted(demods)}")

    # no duplicate channel frequencies
    freqs = [c.freq_hz for c in p.channels]
    if len(set(freqs)) != len(freqs):
        dupes = sorted({f for f in freqs if freqs.count(f) > 1})
        raise ProfileError("profile-duplicate-freq",
                           f"{path}: duplicate channel frequencies {dupes}")

    if p.deviceset_index < 0:
        raise ProfileError("profile-bad-deviceset", f"{path}: deviceset_index must be >= 0")
    if p.sample_rate_hz <= 0 or p.log2_decim < 0:
        raise ProfileError("profile-bad-device", f"{path}: sample_rate_hz>0, log2_decim>=0")

    # camp-mode constraints
    if p.mode == "camp":
        ka = p.keepalive_channels
        if len(ka) != 1:
            raise ProfileError(
                "profile-keepalive-count",
                f"{path}: camp mode requires EXACTLY ONE keepalive channel "
                f"(the always-open channel that holds the mount up); got {len(ka)}. "
                f"Without it the mount drops whenever every real channel is squelched.")

        # THE fit check — every channel must land inside the delivered RF window.
        half = p.half_window_hz
        for c in p.channels:
            off = c.offset_from(p.center_freq_hz)
            edge = abs(off) + c.rf_bw_hz / 2.0
            if edge > half:
                raise ProfileError(
                    "profile-channel-outside-baseband",
                    f"{path}: channel {c.title!r} at {c.freq_hz} Hz is "
                    f"{off:+d} Hz from center; its edge ({edge:.0f} Hz) exceeds the "
                    f"half-window ({half:.0f} Hz = sample_rate {p.sample_rate_hz} / "
                    f"2^{p.log2_decim} / 2). Camp mode needs every channel inside ONE "
                    f"baseband window. Lower log2_decim or raise sample_rate.")
