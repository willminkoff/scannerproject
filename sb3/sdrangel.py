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

DEVICE_READY_TIMEOUT = 10.0   # max wait for a rebound device to enumerate
DEVICE_READY_POLL = 1.0       # between device-readiness polls
REST_BACKOFF_CAP = 30.0       # total seconds to wait for a wedged REST
REST_BACKOFF_START = 0.5      # first backoff interval (doubles each try)


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

    def wait_rest_healthy(self, timeout: float = REST_BACKOFF_CAP) -> bool:
        """Poll GET /sdrangel with exponential backoff until 200, or give up.

        The rapid device path can wedge SDRangel's REST server (HTTP 000 / 5xx)
        while the process stays alive — this is what wedged it 2026-07-19. Rather
        than fire more requests at a struggling server (which makes it worse),
        back off and only proceed once it answers 200. Fails fast+clean if it
        never recovers, so the caller can abort instead of piling on.
        """
        if not self.execute:
            return True
        if self.alive():
            return True
        waited, interval = 0.0, REST_BACKOFF_START
        while waited < timeout:
            self.emit(f"REST unhealthy — backing off {interval:g}s "
                      f"({waited:.0f}/{timeout:.0f}s)")
            self._sleep(interval)
            waited += interval
            if self.alive():
                self.emit("REST recovered")
                return True
            interval = min(interval * 2, timeout - waited if timeout > waited else interval)
        self.emit("REST did not recover within budget — aborting")
        return False

    def sampling_device(self, idx: int) -> dict:
        ds = self.deviceset(idx)
        return ds.get("samplingDevice", {}) or {}

    def wait_device_ready(self, idx: int, expected_hw: str, expected_serial: str,
                          timeout: float = DEVICE_READY_TIMEOUT) -> bool:
        """Poll until a rebound device has actually enumerated on the deviceset.

        Readiness = the samplingDevice reports the expected hwType AND serial AND
        a non-error state. Configuring a device that has not finished enumerating
        (hwType still "Unknown"/phantom, or state "error") is the likely trigger
        of the 2026-07-19 REST wedge — the restore script's find_or_assign waits
        for the device first, and this restores that discipline.
        """
        if not self.execute:
            self.emit(f"would: wait for {expected_hw} {expected_serial} ready on ds{idx}")
            return True
        waited = 0.0
        while waited <= timeout:
            if not self.alive():
                if not self.wait_rest_healthy():
                    return False
            sd = self.sampling_device(idx)
            hw, serial, state = sd.get("hwType"), sd.get("serial"), sd.get("state")
            if hw == expected_hw and serial == expected_serial and state != "error":
                self.emit(f"device ready: {hw} {serial} (state={state})")
                return True
            self._sleep(DEVICE_READY_POLL)
            waited += DEVICE_READY_POLL
        self.emit(f"device NOT ready after {timeout:g}s "
                  f"(hw={sd.get('hwType')}, serial={sd.get('serial')}, "
                  f"state={sd.get('state')})")
        return False

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

    def ensure_deviceset(self, idx: int) -> bool:
        """Guarantee deviceset `idx` exists, adding Rx devicesets as needed.

        A fresh SDRangel has one deviceset (DS0). Loading a second role onto DS1
        requires the deviceset to exist first — `PUT /deviceset/1/device` 404s
        otherwise. `POST /deviceset?direction=0` appends an Rx deviceset (verified
        202 on the box, non-wedging). Idempotent: a no-op when idx already exists.
        """
        if not self.execute:
            self.emit(f"would: ensure deviceset {idx} exists "
                      f"(POST /deviceset?direction=0 if missing)")
            return True
        sets = self.devicesets()
        tries = 0
        while len(sets) <= idx and tries < idx + 3:
            if not self.wait_rest_healthy():
                return False
            self.emit(f"adding deviceset (have {len(sets)}, need index {idx})")
            self._req("POST", "/deviceset?direction=0")
            self._wait(2.0)
            sets = self.devicesets()
            tries += 1
        return len(sets) > idx

    def rebind_device(self, idx: int, hw: str, serial: str) -> bool:
        """PUT a device onto a deviceset, then WAIT for it to enumerate.

        The wait is the fix for the 2026-07-19 wedge: PUT returns before the
        device is actually ready, and configuring it too early wedges REST.
        """
        self.emit(f"rebind ds{idx} → {hw} {serial}")
        self._req("DELETE", f"/deviceset/{idx}/device/run")
        self._wait(1.0)
        s, _ = self._req("PUT", f"/deviceset/{idx}/device",
                         {"hwType": hw, "serial": serial, "direction": 0})
        self._wait(RUN_SETTLE)
        if s not in (200, 202) and self.execute:
            self.emit(f"PUT device failed (HTTP {s})")
            return False
        return self.wait_device_ready(idx, hw, serial)

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
            # Only PATCH a healthy REST — back off if it is struggling rather
            # than adding load, and abort cleanly if it never recovers.
            if not self.wait_rest_healthy():
                return False
            self._req("PATCH", f"/deviceset/{idx}/device/settings", body)
            self._wait(DEVICE_SETTLE)
            if not self.execute:
                return True
            if not self.wait_rest_healthy():
                return False
            c = self.device_center(idx)
            if c is not None and abs(c - center_hz) < CENTER_TOL_HZ:
                return True
            self.emit(f"center readback {c} != {center_hz} (attempt {attempt+1}/3)")
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
        """Toggle copyToUDP 0→1 on ONE output device; disable it on all others.

        Two lessons, both measured on Neptune 2026-07-19:
          * Plain arming does NOT start the sender thread — the 0→1 toggle does
            (the REST-armed-but-not-emitting bug fix-neptune-angel.sh exists for).
          * If MORE THAN ONE output device has copyToUDP on the SAME UDP port,
            two senders write to one socket and the bridge gets a corrupt,
            flapping stream. So disable copyToUDP on every other device SENDING
            TO THIS PORT — but leave devices on OTHER ports alone. This is what
            lets Air (idx-1 → :9998) and Ground (idx0 → :9999) coexist: two
            mounts, two ports, one sender each. (Disabling all-other-devices
            would kill Air's tap when Ground armed — Phase 3.3 fix.)
        """
        for dev in self.audio_outputs():
            idx = dev.get("index")
            same_port = dev.get("udpPort") == port
            if (idx is not None and idx != audio_index and dev.get("copyToUDP")
                    and same_port):
                self._req("PATCH", "/audio/output/parameters",
                          {"index": idx, "copyToUDP": 0})
                self._wait(0.3)
        self._req("PATCH", "/audio/output/parameters",
                  {"index": audio_index, "copyToUDP": 0})
        self._wait(1.0)
        self._req("PATCH", "/audio/output/parameters",
                  {"index": audio_index, "copyToUDP": 1,
                   "udpAddress": address, "udpPort": port,
                   "udpChannelMode": 2, "sampleRate": 48000})
        self._wait(1.0)

    def audio_outputs(self) -> list:
        _, d = self._req("GET", "/audio")
        return (d or {}).get("outputDevices", []) or []

    # -- granular live setters (Phase 3.2 UI write path) ------------------
    # These PATCH a single field onto a RUNNING device/channel — light-touch
    # edits for the UI, distinct from apply_device_settings()'s full rewrite.
    # SDRangel merges the provided subkeys, so a partial patch leaves the rest.

    def list_channels(self, idx: int) -> list:
        return self.deviceset(idx).get("channels", []) or []

    def patch_device(self, idx: int, hw: str, settings_key: str, patch: dict) -> bool:
        if not self.wait_rest_healthy():
            return False
        s, _ = self._req("PATCH", f"/deviceset/{idx}/device/settings",
                         {"deviceHwType": hw, "direction": 0, settings_key: patch})
        return s in (200, 202) or not self.execute

    def patch_channel(self, idx: int, ch: int, ctype: str,
                      settings_key: str, patch: dict) -> bool:
        if not self.wait_rest_healthy():
            return False
        s, _ = self._req("PATCH", f"/deviceset/{idx}/channel/{ch}/settings",
                         {"channelType": ctype, "direction": 0, settings_key: patch})
        return s in (200, 202) or not self.execute

    def device_state(self, idx: int) -> Optional[str]:
        return self.sampling_device(idx).get("state")

    def ensure_running(self, idx: int, hw: str, serial: str,
                       budget_sec: float = 30.0) -> bool:
        """Guarantee the device is in 'running' state — GENTLY.

        A run POST can return 200 yet leave the RTL in 'error'/'idle' (the
        83241970 flakiness). Recovery order, softest first, because the AGGRESSIVE
        path (rapid rebind) is what crashed SDRangel 2026-07-19 — the cure was
        worse than the disease:

          1. run, check.
          2. up to 2× : stop → wait 3s → run → check.   (soft: no rebind)
          3. ONCE, only if soft failed: full rebind → run → check.

        A REST-health gate precedes every op, and a wall-clock budget (~30s)
        caps the whole thing so it unwinds instead of hammering a struggling
        backend. One shot at recovery, not a cascade.
        """
        if not self.execute:
            self.run(idx)
            return True
        start = self._now()

        def budget_left() -> bool:
            return (self._now() - start) < budget_sec

        if not self.wait_rest_healthy():
            return False
        self.run(idx)
        if self.device_state(idx) == "running":
            return True

        # 2. soft recovery: stop + settle + run, up to twice. No rebind.
        for attempt in range(2):
            if not budget_left():
                self.emit("ensure_running: budget exhausted — giving up (soft)")
                return False
            st = self.device_state(idx)
            self.emit(f"device not running (state={st}) — soft retry "
                      f"{attempt+1}/2 (stop→wait→run)")
            if not self.wait_rest_healthy():
                return False
            self.stop(idx)
            self._wait(3.0)
            if not self.wait_rest_healthy():
                return False
            self.run(idx)
            if self.device_state(idx) == "running":
                self.emit("device running after soft retry")
                return True

        # 3. last resort: ONE rebind. This is the op that can crash SDRangel, so
        #    it is reached only after the soft path failed, and only once.
        if not budget_left():
            self.emit("ensure_running: budget exhausted — not rebinding")
            return False
        self.emit("soft retries failed — ONE rebind as last resort")
        if not self.wait_rest_healthy():
            return False
        if not self.rebind_device(idx, hw, serial):
            return False
        self.run(idx)
        st = self.device_state(idx)
        if st == "running":
            self.emit("device running after rebind")
            return True
        self.emit(f"device STILL not running (state={st}) — giving up")
        return False

    def _now(self) -> float:
        # Wall clock for the budget. Overridable in tests (monotonic in prod).
        import time as _t
        return _t.monotonic()
