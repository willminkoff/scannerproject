"""sb3.backends — READ-ONLY observers of live backend state.

Every function here observes and returns.  Nothing in this module starts,
stops, or reconfigures anything, and nothing in it may ever grow the ability
to: `sb3-ctl status` must be safe to run against a live box at any time, and
`kill` depends on these to *verify* its invariant rather than assume it.

The observations are deliberately cheap and bounded — a status call that hangs
on a wedged backend is a status call nobody runs.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from typing import Dict, List, NamedTuple, Optional

DEFAULT_TIMEOUT_SEC = 5.0
ICECAST_STATUS_URL = "http://127.0.0.1:8000/status-json.xsl"
SDRANGEL_REST = "http://127.0.0.1:8091/sdrangel"


class MountState(NamedTuple):
    mount: str
    http_status: Optional[int]   # None = unreachable
    present: bool                # present in icecast's mount list


class DevicesetState(NamedTuple):
    index: int
    hw_type: str
    serial: Optional[str]
    state: str
    center_hz: Optional[int]
    channels: List[str]

    @property
    def is_phantom(self) -> bool:
        """True when a deviceset has no real device bound.

        SDRangel falls back to its index-0 pseudo-device (AaroniaRTSA) when the
        intended device is not bound, keeping the channel config attached to a
        deviceset that cannot produce samples.  Observed live on Neptune
        2026-07-16: DS0 hw=AaroniaRTSA serial=None state=idle @1.45 MHz while
        the airband channel config survived on it.  That state reports healthy
        to every process check and produces no audio, so name it explicitly.
        """
        return self.serial is None and self.hw_type in {
            "AaroniaRTSA", "FileInput", "TestSource", "SigMFFileInput",
        }


def _get_json(url: str, timeout: float = DEFAULT_TIMEOUT_SEC):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def launchctl_loaded(prefix: str = "com.scannerproject.") -> List[str]:
    """Labels currently loaded in the user's launchd GUI domain."""
    try:
        out = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=DEFAULT_TIMEOUT_SEC,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    labels = []
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].startswith(prefix):
            labels.append(parts[2].strip())
    return sorted(labels)


def mount_state(mount: str, timeout: float = DEFAULT_TIMEOUT_SEC) -> MountState:
    """HTTP status + icecast presence for one mount.

    Distinguishes 404-because-absent from 200-but-silent: an ffmpeg bridge that
    never receives a byte never connects to icecast, so the mount is never
    created at all.  That is why `present` is tracked separately from status.

    ⚠️  MUST use GET, not HEAD.  **Icecast answers HEAD on a mount with 400 Bad
    Request** (verified against Icecast 2.4.4 on Neptune 2026-07-16: HEAD -> 400,
    GET -> 200 live / 404 absent).  A HEAD-based probe therefore reports 400 for
    *every* mount, live or dead — which would make `present` uniformly False and
    silently neuter the invariant check in killswitch.verify_mounts(): a mount
    that genuinely dropped would compare False->False and register as "was
    already down; not ours".  The check would pass while the thing it exists to
    catch happened.  Exactly the "useful liar" shape this project keeps finding.

    A ``Range: bytes=0-0`` header keeps the GET from pulling a live stream: we
    want the status line, not the audio.  Icecast ignores the range on a live
    mount and would start streaming, so the response is closed without reading.
    """
    url = f"http://127.0.0.1:8000/{mount}"
    status: Optional[int] = None
    try:
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        resp = urllib.request.urlopen(req, timeout=timeout)
        try:
            status = resp.status
        finally:
            resp.close()   # never consume a live mount's stream
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
    except (urllib.error.URLError, OSError):
        status = None
    return MountState(mount=mount, http_status=status,
                      present=(status is not None and 200 <= status < 300))


def icecast_mounts(timeout: float = DEFAULT_TIMEOUT_SEC) -> List[str]:
    """Mount names icecast currently serves (dynamic — sources create them)."""
    data = _get_json(ICECAST_STATUS_URL, timeout)
    if not data:
        return []
    source = data.get("icestats", {}).get("source")
    if source is None:
        return []
    if isinstance(source, dict):
        source = [source]
    mounts = []
    for s in source:
        listenurl = s.get("listenurl", "")
        if listenurl:
            mounts.append(listenurl.rsplit("/", 1)[-1])
    return sorted(mounts)


def sdrangel_devicesets(timeout: float = DEFAULT_TIMEOUT_SEC) -> List[DevicesetState]:
    """Observe SDRangel's devicesets. Empty list if SDRangel is unreachable."""
    data = _get_json(SDRANGEL_REST, timeout)
    if not data:
        return []
    out = []
    for ds in data.get("devicesetlist", {}).get("deviceSets", []):
        sd = ds.get("samplingDevice", {})
        serial = sd.get("serial")
        out.append(DevicesetState(
            index=sd.get("index", -1),
            hw_type=sd.get("hwType", "?"),
            serial=None if serial in (None, "None", "") else serial,
            state=sd.get("state", "?"),
            center_hz=sd.get("centerFrequency"),
            channels=[c.get("title", c.get("id", "?")) for c in ds.get("channels", [])],
        ))
    return out


def sdrangel_deviceset_detail(index: int, timeout: float = DEFAULT_TIMEOUT_SEC) -> Optional[Dict]:
    """Full deviceset record (channels included). None if unreachable."""
    return _get_json(f"{SDRANGEL_REST}/deviceset/{index}", timeout)


def sdrangel_channels(index: int, timeout: float = DEFAULT_TIMEOUT_SEC) -> List[Dict]:
    """Channel list for a deviceset — [{index, id, title, ...}, …]. Read-only."""
    ds = sdrangel_deviceset_detail(index, timeout)
    if not ds:
        return []
    return ds.get("channels", []) or []


def sdrangel_channel_settings(ds_index: int, ch_index: int,
                              timeout: float = DEFAULT_TIMEOUT_SEC) -> Optional[Dict]:
    """One channel's live settings blob. Read-only GET, None if unreachable.

    Returns the raw REST body, e.g. ``{"NFMDemodSettings": {"squelch": -55,
    "volume": 3.0, ...}}`` — the caller unwraps by demod key, because the key
    name varies per demod type and this module does not interpret.

    Added for the Phase 4.1 reconciler, which needs per-channel squelch/volume to
    tell "the user is tuning" (BENIGN) from "the profile is not live"
    (RECOVERABLE).  ``sdrangel_channels()`` returns only the channel LIST
    (index/id/title/deltaFrequency); the settings live at this sub-resource.
    """
    return _get_json(f"{SDRANGEL_REST}/deviceset/{ds_index}/channel/{ch_index}/settings",
                     timeout)


def channel_settings_body(raw: Optional[Dict]) -> Dict:
    """Unwrap the ``<Demod>Settings`` sub-dict from a channel-settings body. Pure.

    SDRangel nests the real fields one level down under a demod-specific key
    (``AMDemodSettings``, ``NFMDemodSettings``, …).  Returns {} for anything
    unrecognisable so callers never have to null-check a nest.
    """
    if not isinstance(raw, dict):
        return {}
    for key, val in raw.items():
        if key.endswith("Settings") and isinstance(val, dict):
            return val
    return {}


def sdrangel_audio_outputs(timeout: float = DEFAULT_TIMEOUT_SEC) -> List[Dict]:
    """SDRangel output audio devices (from GET /sdrangel/audio). Read-only.

    The endpoint is `/sdrangel/audio`, NOT `/sdrangel/audio/devices` (which 404s
    — verified on the box 2026-07-19).
    """
    data = _get_json(f"{SDRANGEL_REST}/audio", timeout)
    if not data:
        return []
    return data.get("outputDevices", []) or []


def resolve_audio_tap(strategy: str = "system_default",
                      timeout: float = DEFAULT_TIMEOUT_SEC):
    """Resolve the (name, index) of the output device to carry the copyToUDP tap.

    Returns (None, None) if unreachable.

    ⚠️  MEASURED ON NEPTUNE 2026-07-19, and it overturns the brief's assumption.
    The brief specified `dynamic_idx0` — route channel audio to the index-0
    device ("Mac mini Speakers"). Tested live, that path yields a STEADY 404:
    routing demod audio to a real hardware output and tapping it does not
    reliably reach the UDP bridge. Routing to the VIRTUAL "System default
    device" (index -1) and tapping THAT gives a stable 200 for 30 s straight.
    This matches macos/bin/sdrangel-restore.py (which uses "System default
    device" and runs every 10 min on this box), so it is the trustworthy recipe.

      * strategy "system_default" → the "System default device" (index -1)
      * strategy "dynamic_idx0"   → the index-0 device (kept for other hosts,
                                     e.g. Venus, where the exact hardware name is
                                     what works)
    """
    outs = sdrangel_audio_outputs(timeout)
    if not outs:
        return None, None
    if strategy == "system_default":
        for dev in outs:
            if dev.get("name") == "System default device":
                return dev.get("name"), dev.get("index")
    # "name:X" — pick a specific output device by NAME (Phase 3.3: Ground routes
    # to "BlackHole 2ch", a virtual sink that sustains copyToUDP like the System
    # default device does — the physical idx0 only bursts). By-name is robust to
    # the index BlackHole lands at after install/reboot.
    if strategy.startswith("name:"):
        want = strategy.split(":", 1)[1]
        for dev in outs:
            if dev.get("name") == want:
                return dev.get("name"), dev.get("index")
        return None, None   # named device not present — do not silently fall back
    # "index:N" — pick a specific output device by index (Phase 3.3: a second
    # role needs a DIFFERENT output device than Air's, for an independent
    # copyToUDP port). Falls back to idx 0.
    if strategy.startswith("index:"):
        try:
            want = int(strategy.split(":", 1)[1])
        except ValueError:
            want = 0
        for dev in outs:
            if dev.get("index") == want:
                return dev.get("name"), dev.get("index")
    for dev in outs:
        if dev.get("index") == 0:
            return dev.get("name"), dev.get("index")
    return None, None


def resolve_idx0_audio_name(timeout: float = DEFAULT_TIMEOUT_SEC) -> Optional[str]:
    """Back-compat shim → name from resolve_audio_tap('dynamic_idx0')."""
    return resolve_audio_tap("dynamic_idx0", timeout)[0]


def snapshot(mounts) -> Dict:
    """One bounded read of everything status needs."""
    return {
        "loaded": launchctl_loaded(),
        "mounts": [mount_state(m) for m in mounts],
        "icecast_mounts": icecast_mounts(),
        "devicesets": sdrangel_devicesets(),
    }
