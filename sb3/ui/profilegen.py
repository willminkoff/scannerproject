"""sb3.ui.profilegen — turn a wizard channel pick into a loadable SB3 profile.

Sub-phase 1: **analog only**. The Favorites wizard can now browse real
RadioReference data; this is what makes the Save button mean something — the
picked channels become a profile file that ``sb3-ctl profile load`` can apply.
Digital (P25 → an SDRTrunk playlist) is sub-phase 2 and is deliberately absent.

The whole module is pure: it takes a request dict and returns the profile dict
(or raises with a specific reason). Nothing here touches the filesystem or a
backend — :mod:`sb3.ui.routes` does the writing, so the arithmetic below is
unit-testable without a box.

**The generated profile is validated with the real parser before it is
returned.** ``sb3.profile.parse_profile`` is the same code ``profile load``
runs, and it enforces things this generator must not get wrong:

  * one demod family per profile (mixing AM and NFM is rejected outright),
  * exactly one keepalive channel in camp mode,
  * and THE fit check — every channel must land inside the RF window the device
    actually delivers.

That last one is why the centre/sample-rate arithmetic exists at all. Writing a
file that looks fine as JSON and silently drops half its channels on the
hardware is the failure mode this project keeps finding; running the real
validator here means a profile that gets written is a profile that loads.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence

from ..profile import ProfileError, parse_profile

# ---------------------------------------------------------------------------
# fleet + hardware facts
# ---------------------------------------------------------------------------

#: serial → what SB3 knows about that device.  ``deviceset_index`` and ``role``
#: decide WHAT A LOAD TAKES OVER, so they are returned to the caller and shown
#: in the UI: loading an RSP1B profile replaces Air on DS0, loading an RTL
#: profile replaces the VFO on DS1.  Nothing here guesses — these are the
#: current Neptune bindings.
DEVICES: Dict[str, Dict] = {
    "2405265A60": {"hardware_id": "SDRplayV3", "label": "RSP1B",
                   "deviceset_index": 0, "role": "air",
                   "replaces": "Air (DS0)"},
    "95339533":   {"hardware_id": "RTLSDR", "label": "RTL NESDR SMArTee",
                   "deviceset_index": 1, "role": "vfo",
                   "replaces": "VFO (DS1)"},
    "61108285":   {"hardware_id": "RTLSDR", "label": "RTL (spare)",
                   "deviceset_index": 1, "role": "vfo",
                   "replaces": "VFO (DS1)"},
    "56919602":   {"hardware_id": "RTLSDR", "label": "RTL Nooelec (spare)",
                   "deviceset_index": 1, "role": "vfo",
                   "replaces": "VFO (DS1)"},
}

#: Sample rates each hardware actually supports, ascending.  We snap UP to the
#: first rate whose window holds the span — never down, which would clip.
SUPPORTED_RATES = {
    "SDRplayV3": (2_048_000, 4_000_000, 6_000_000, 8_000_000, 10_000_000),
    "RTLSDR": (250_000, 1_024_000, 2_048_000, 2_400_000),
}

#: Tunable span per hardware (Hz).
TUNE_RANGE = {
    "RTLSDR": (24_000_000, 1_766_000_000),
    "SDRplayV3": (1_000_000, 2_000_000_000),
}

#: Airband is AM; everything else on these receivers is narrowband FM.
AIRBAND_LO_HZ, AIRBAND_HI_HZ = 118_000_000, 137_000_000

AM_RF_BW = 8_000
NFM_RF_BW = 12_500
NFM_AF_BW = 3_000

#: Only 80% of the sampled window is usable (filter roll-off at the edges), so
#: the span is inflated before choosing a rate.
USABLE_FRACTION = 1.25

#: The keepalive channel is always-open (squelch at the floor, low volume) and
#: holds the icecast mount up when every real channel is squelched shut. Camp
#: mode requires exactly one; without it the mount drops the moment the band
#: goes quiet — the proven pattern from the airband profiles.
KEEPALIVE_SQUELCH_DB = -100.0
KEEPALIVE_VOLUME = 0.4
CHANNEL_SQUELCH_DB = -55.0
CHANNEL_VOLUME = 3.0

MAX_CHANNELS = 32          # SDRangel gets unhappy well before this
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class GenError(Exception):
    """A request that cannot become a valid profile. Carries a human reason."""

    def __init__(self, code: int, reason: str):
        self.code = code
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def safe_name(raw: str) -> str:
    """Normalise a user-supplied profile name to a safe filename stem.

    **Rejects rather than silently mangles.** Only case-folding, trimming, and
    space→dash are applied; anything else that would have to be stripped is an
    error instead. Stripping quietly is how ``../../etc/passwd`` becomes a file
    cheerfully saved as ``etcpasswd.json`` — no traversal, but the user's
    profile is now under a name they never chose and cannot find. It also
    means a rejected name says so instead of half-working.
    """
    name = str(raw or "").strip().lower().replace(" ", "-")
    name = re.sub(r"-{2,}", "-", name)
    if not name:
        raise GenError(400, "profile name is empty — give it a name "
                            "(e.g. davidson-fire)")
    if not NAME_RE.match(name) or "/" in name or "\\" in name or ".." in name:
        raise GenError(400, f"profile name {raw!r} is not usable — use only "
                            f"letters, digits, dot, dash or underscore, starting "
                            f"with a letter or digit (e.g. davidson-fire)")
    return name


def demod_for(freq_hz: int) -> str:
    return "AM" if AIRBAND_LO_HZ <= freq_hz <= AIRBAND_HI_HZ else "NFM"


def pick_sample_rate(hardware_id: str, span_hz: int) -> int:
    """Smallest supported rate whose usable window covers the span."""
    rates = SUPPORTED_RATES[hardware_id]
    need = max(int(span_hz * USABLE_FRACTION), 1)
    for r in rates:
        if r >= need:
            return r
    raise GenError(
        400,
        f"channels span {span_hz/1e6:.3f} MHz, which needs about "
        f"{need/1e6:.3f} MHz of bandwidth — more than the {rates[-1]/1e6:.3f} "
        f"Msps maximum for this device. Pick channels closer together, or "
        f"split them into two profiles.")


# ---------------------------------------------------------------------------
# the generator
# ---------------------------------------------------------------------------

def build_profile(req: Dict) -> Dict:
    """Request dict → profile dict. Raises GenError with a specific reason.

    Expected request::

        {"name": "...", "device_serial": "...",
         "channels": [{"freq_hz": 155775000, "label": "..."}, ...],
         "description": "optional"}
    """
    name = safe_name(req.get("name", ""))

    serial = str(req.get("device_serial", "")).strip()
    dev = DEVICES.get(serial)
    if dev is None:
        raise GenError(400, f"unknown device serial {serial!r} — expected one of: "
                            + ", ".join(sorted(DEVICES)))
    hw = dev["hardware_id"]

    raw_channels = req.get("channels")
    if not isinstance(raw_channels, list) or not raw_channels:
        raise GenError(400, "no channels selected — pick at least one channel")
    if len(raw_channels) > MAX_CHANNELS:
        raise GenError(400, f"{len(raw_channels)} channels selected; the limit is "
                            f"{MAX_CHANNELS} per profile")

    lo_hz, hi_hz = TUNE_RANGE[hw]
    seen = set()
    chans: List[Dict] = []
    for i, c in enumerate(raw_channels):
        if not isinstance(c, dict):
            raise GenError(400, f"channel {i} is not an object")
        try:
            f = int(round(float(c.get("freq_hz"))))
        except (TypeError, ValueError):
            raise GenError(400, f"channel {i} has no usable freq_hz "
                                f"({c.get('freq_hz')!r})")
        if not (lo_hz <= f <= hi_hz):
            raise GenError(400, f"{f/1e6:.4f} MHz is outside the {dev['label']}'s "
                                f"tunable range ({lo_hz/1e6:.0f}-{hi_hz/1e6:.0f} MHz)")
        if f in seen:
            raise GenError(400, f"{f/1e6:.4f} MHz appears twice — remove the duplicate")
        seen.add(f)
        label = str(c.get("label") or c.get("title") or "").strip() or f"{f/1e6:.4f} MHz"
        chans.append({"freq_hz": f, "title": label[:64]})

    # One demod family per profile — the parser rejects a mix, so catch it here
    # with a reason the user can act on rather than a schema error.
    demods = {demod_for(c["freq_hz"]) for c in chans}
    if len(demods) != 1:
        am = sorted(c["freq_hz"] / 1e6 for c in chans if demod_for(c["freq_hz"]) == "AM")
        fm = sorted(c["freq_hz"] / 1e6 for c in chans if demod_for(c["freq_hz"]) == "NFM")
        raise GenError(
            400,
            "this pick mixes AM airband with FM channels, which one profile "
            "cannot do (a deviceset has a single demodulator). "
            f"AM: {', '.join(f'{f:.4f}' for f in am[:4])}; "
            f"FM: {', '.join(f'{f:.4f}' for f in fm[:4])}. "
            "Save them as two profiles.")
    demod = demods.pop()
    rf_bw = AM_RF_BW if demod == "AM" else NFM_RF_BW

    freqs = sorted(c["freq_hz"] for c in chans)
    span = freqs[-1] - freqs[0]
    center = (freqs[0] + freqs[-1]) // 2
    sample_rate = pick_sample_rate(hw, span + rf_bw)   # include channel width

    chans.sort(key=lambda c: c["freq_hz"])
    out_channels = []
    for i, c in enumerate(chans):
        ch = {
            "freq_hz": c["freq_hz"],
            "title": c["title"],
            "demod": demod,
            "rf_bw_hz": rf_bw,
            "squelch_db": KEEPALIVE_SQUELCH_DB if i == 0 else CHANNEL_SQUELCH_DB,
            "volume": KEEPALIVE_VOLUME if i == 0 else CHANNEL_VOLUME,
        }
        if i == 0:
            ch["keepalive"] = True
        out_channels.append(ch)

    device = {
        "hardware_id": hw,
        "serial": serial,
        "center_freq_hz": center,
        "sample_rate_hz": sample_rate,
        "log2_decim": 0,
        "agc": False,
        "dc_block": True,
    }
    if hw == "SDRplayV3":
        # SDRplay does not take the RTL's single gain field — mirrors the
        # proven airband RSP recipe.
        device.update({"if_gain_db": -30, "lna_index": 3,
                       "bandwidth_index": 3, "tuner": 0})
        device.pop("dc_block", None)
    else:
        device["gain_tenths_db"] = 350

    desc = str(req.get("description", "") or "").strip()[:300]
    profile = {
        "_comment": (
            f"Generated by the SB3 Favorites wizard (analog, sub-phase 1). "
            f"{len(out_channels)} channels spanning {span/1e6:.3f} MHz on the "
            f"{dev['label']} ({serial}); centre {center/1e6:.4f} MHz at "
            f"{sample_rate/1e6:.3f} Msps, demod {demod}. The first channel is the "
            f"keepalive (squelch {KEEPALIVE_SQUELCH_DB:g}, volume {KEEPALIVE_VOLUME}) "
            f"— it stays open so the icecast mount survives a quiet band. "
            f"LOADING THIS REPLACES {dev['replaces']}."
            + (f" User note: {desc}" if desc else "")),
        "name": name,
        "role": dev["role"],
        "sub_role": "wizard",
        "mode": "camp",
        "deviceset_index": dev["deviceset_index"],
        "device": device,
        "channels": out_channels,
        "audio_device": {"strategy": "system_default"},
        "copy_to_udp": {"address": "127.0.0.1", "port": 9998},
        "mount": "neptune-analog.mp3",
        "_generated": {"source": "favorites-wizard", "sub_phase": 1,
                       "description": desc, "span_hz": span,
                       "replaces": dev["replaces"]},
    }
    if demod == "NFM":
        for ch in profile["channels"]:
            ch["af_bw_hz"] = NFM_AF_BW

    # Prove it loads BEFORE anyone writes it to disk.
    try:
        parse_profile(profile, path=f"<generated {name}>")
    except ProfileError as exc:
        raise GenError(400, f"generated profile is not valid ({exc.code}): {exc.detail}")

    return profile


def describe_devices() -> List[Dict]:
    """Device list for the UI dropdown."""
    return [{"serial": s, "label": d["label"], "hardware_id": d["hardware_id"],
             "replaces": d["replaces"]} for s, d in sorted(DEVICES.items())]
