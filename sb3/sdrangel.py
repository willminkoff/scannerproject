"""sb3.sdrangel — the MUTATING SDRangel REST client.

Kept OUT of sb3/backends.py deliberately: backends.py is the read-only observer
module `status` and `kill` depend on, and it declares that nothing in it may
grow a write path. Writes live here instead. (The Phase 2 brief said to extend
backends.py; this is the one place that guidance is not followed, because doing
so would break the read-only guarantee the kill-switch relies on. Flagged, not
silently diverged.)

Everything here is distilled from macos/bin/sdrangel-restore.py and
fix-neptune-angel.sh — the only recipes SDRangel's device path is known to
tolerate. Two hard-won rules are baked in and must not be "optimised" away:

  1. **PATCH a RUNNING device.** A settings PATCH on a freshly-(re)loaded device
     is silently ignored. So `run` first, then PATCH, then verify the center
     actually converged.
  2. **One channel at a time, with delays.** Rapid bulk channel ops crash
     SDRangel outright. Every add/delete is spaced by CHANNEL_DELAY, and every
     op first checks the REST endpoint is still alive.

A dry-run client records the calls it WOULD make and performs no network I/O,
so `profile load` (no --execute) can print the exact REST sequence.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

BASE = "http://127.0.0.1:8091/sdrangel"

CHANNEL_DELAY = 0.4     # spacing between channel ops (bulk = crash)
DEVICE_SETTLE = 2.0     # after a device settings PATCH, before reading back
RUN_SETTLE = 3.0        # after a run/rebind, before touching settings
CENTER_TOL_HZ = 5000    # RTL center readback tolerance


class SDRangelClient:
    def __init__(self, *, execute: bool, emit: Callable[[str], None],
                 base: str = BASE, sleep=time.sleep):
        self.execute = execute
        self.emit = emit
        self.base = base
        self._sleep = sleep
        self.calls: List[Tuple[str, str, Optional[dict]]] = []   # (method, path, body)

    # -- transport --------------------------------------------------------

    def _req(self, method: str, path: str, body: Optional[dict] = None,
             timeout: float = 8.0):
        self.calls.append((method, path, body))
        if not self.execute:
            shown = f" {json.dumps(body)}" if body is not None else ""
            self.emit(f"would: {method} {path}{shown}")
            return 200, {}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as x:
                raw = x.read()
                return x.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            return e.code, {}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return None, {"error": str(e)}

    def _wait(self, seconds: float):
        if self.execute:
            self._sleep(seconds)

    # -- reads (used even in execute to verify) ---------------------------

    def alive(self) -> bool:
        if not self.execute:
            return True
        s, _ = self._req("GET", "", timeout=10)
        return s == 200

    def devicesets(self) -> List[dict]:
        _, d = self._req("GET", "")
        return (d.get("devicesetlist", {}) or {}).get("deviceSets", [])

    def deviceset(self, idx: int) -> dict:
        _, d = self._req("GET", f"/deviceset/{idx}")
        return d or {}

    def device_center(self, idx: int) -> Optional[int]:
        _, s = self._req("GET", f"/deviceset/{idx}/device/settings")
        for key in ("rtlSdrSettings", "sdrPlayV3Settings", "hackRFInputSettings"):
            if isinstance(s, dict) and key in s:
                return (s[key] or {}).get("centerFrequency")
        return None

    def ds_serial(self, idx: int) -> Optional[str]:
        ds = self.deviceset(idx)
        return (ds.get("samplingDevice", {}) or {}).get("serial")

    # -- writes -----------------------------------------------------------

    def rebind_device(self, idx: int, hw: str, serial: str) -> bool:
        """PUT a device onto a deviceset. Stop it first, settle after."""
        self.emit(f"rebind ds{idx} → {hw} {serial}")
        self._req("DELETE", f"/deviceset/{idx}/device/run")
        self._wait(1.0)
        s, _ = self._req("PUT", f"/deviceset/{idx}/device",
                         {"hwType": hw, "serial": serial, "direction": 0})
        self._wait(RUN_SETTLE)
        return s in (200, 202) or not self.execute

    def run(self, idx: int) -> None:
        self._req("POST", f"/deviceset/{idx}/device/run")
        self._wait(RUN_SETTLE)

    def stop(self, idx: int) -> None:
        self._req("DELETE", f"/deviceset/{idx}/device/run")
        self._wait(1.0)

    def apply_device_settings(self, idx: int, settings_key: str,
                              settings: dict, hw: str, center_hz: int) -> bool:
        """PATCH device settings onto a RUNNING device; verify center converges.

        Returns True on convergence (or in dry-run). The device MUST already be
        running — call run() first.
        """
        body = {"deviceHwType": hw, "direction": 0, settings_key: settings}
        for attempt in range(3):
            if not self.alive():
                return False
            self._req("PATCH", f"/deviceset/{idx}/device/settings", body)
            self._wait(DEVICE_SETTLE)
            if not self.execute:
                return True
            c = self.device_center(idx)
            if c is not None and abs(c - center_hz) < CENTER_TOL_HZ:
                return True
        return False

    def clear_channels(self, idx: int) -> bool:
        """DELETE every channel, back to front, one at a time."""
        ds = self.deviceset(idx)
        n = len(ds.get("channels", [])) if self.execute else 0
        for i in range(n - 1, -1, -1):
            if not self.alive():
                return False
            self._req("DELETE", f"/deviceset/{idx}/channel/{i}")
            self._wait(CHANNEL_DELAY)
        if not self.execute:
            # still record the intent for the dry-run trace
            self._req("DELETE", f"/deviceset/{idx}/channel/*  (each existing, back-to-front)")
        return True

    def add_channel(self, idx: int, ctype: str, settings_key: str,
                    settings: dict) -> bool:
        """POST a channel then PATCH its settings. One at a time."""
        if not self.alive():
            return False
        self._req("POST", f"/deviceset/{idx}/channel",
                  {"channelType": ctype, "direction": 0})
        self._wait(CHANNEL_DELAY)
        ch_idx = 0
        if self.execute:
            ds = self.deviceset(idx)
            ch_idx = len(ds.get("channels", [])) - 1
        self._req("PATCH", f"/deviceset/{idx}/channel/{ch_idx}/settings",
                  {"channelType": ctype, "direction": 0, settings_key: settings})
        self._wait(CHANNEL_DELAY)
        return True

    def set_copy_to_udp(self, *, address: str, port: int,
                        audio_index: int = 0) -> None:
        """Toggle copyToUDP 0→1 on the idx0 output device to (re)start the sender.

        Plain arming does NOT start the sender thread — the toggle does. This is
        the REST-armed-but-not-emitting bug fix-neptune-angel.sh exists for.
        """
        self._req("PATCH", "/audio/output/parameters",
                  {"index": audio_index, "copyToUDP": 0})
        self._wait(1.0)
        self._req("PATCH", "/audio/output/parameters",
                  {"index": audio_index, "copyToUDP": 1,
                   "udpAddress": address, "udpPort": port,
                   "udpChannelMode": 2, "sampleRate": 48000})
        self._wait(1.0)
