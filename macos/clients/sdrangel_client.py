#!/usr/bin/env python3
"""sdrangel_client.py — thin wrapper around the SDRangel REST API (default :8091).

SDRangel exposes a full Swagger/OpenAPI surface under /sdrangel. This wraps the
endpoints sb3-ui + Claude need: instance/deviceset enumeration, device tuning,
channel (demod) settings, and Frequency-Scanner control.

STATUS: written from the documented API shape BEFORE testing against a live
instance. Field/endpoint names marked (verify) should be confirmed against the
running Swagger UI at http://<host>:8091/api/ once SDRangel is up.

Usage as a library:
    sa = SDRangel("http://127.0.0.1:8091")
    print(sa.instance_summary())
    print(sa.devicesets())

CLI:
    python3 sdrangel_client.py status
    python3 sdrangel_client.py devicesets
    python3 sdrangel_client.py channel 0 0          # deviceset 0, channel 0 settings
    python3 sdrangel_client.py set-squelch 0 0 -55  # ds 0, ch 0, squelch dBFS  (verify field)
"""
from __future__ import annotations
import json, os, sys, urllib.request, urllib.error

DEFAULT_BASE = os.environ.get("SDRANGEL_REST", "http://127.0.0.1:8091")


class SDRangel:
    def __init__(self, base: str = DEFAULT_BASE, timeout: float = 6.0):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict | None = None):
        url = f"{self.base}/sdrangel{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}", "detail": e.read().decode()[:300], "url": url}
        except Exception as e:
            return {"error": str(e), "url": url}

    # ---- read ----
    def instance_summary(self):                 return self._req("GET", "")
    def devicesets(self):                        return self._req("GET", "/devicesets")
    def deviceset(self, i: int):                 return self._req("GET", f"/deviceset/{i}")
    def device_settings(self, i: int):           return self._req("GET", f"/deviceset/{i}/device/settings")
    def channel_settings(self, i: int, c: int):  return self._req("GET", f"/deviceset/{i}/channel/{c}/settings")

    # ---- write (verify exact JSON keys against live Swagger) ----
    def set_center_freq(self, i: int, hz: int):
        # device settings payload is device-type-specific; this is the SDRplayV3 shape (verify)
        return self._req("PATCH", f"/deviceset/{i}/device/settings",
                         {"deviceHwType": "SDRplayV3",
                          "sdrPlayV3Settings": {"centerFrequency": int(hz)}})

    def set_squelch(self, i: int, c: int, dbfs: float, demod: str = "AMDemod"):
        # channel settings keyed by demod type; AMDemod/NFMDemod have a 'squelch' field (verify units)
        key = {"AMDemod": "AMDemodSettings", "NFMDemod": "NFMDemodSettings"}.get(demod, "AMDemodSettings")
        return self._req("PATCH", f"/deviceset/{i}/channel/{c}/settings",
                         {"channelType": demod, key: {"squelch": float(dbfs)}})

    def device_run(self, i: int, run: bool):
        return self._req("POST" if run else "DELETE", f"/deviceset/{i}/device/run")

    # ---- frequency scanner (channel plugin "FreqScanner") ----
    def scanner_settings(self, i: int, c: int):
        return self._req("GET", f"/deviceset/{i}/channel/{c}/settings")

    def scanner_set_running(self, i: int, c: int, running: bool):
        # FreqScanner has a run/scan state in its settings (verify field name e.g. 'running'/'mode')
        return self._req("PATCH", f"/deviceset/{i}/channel/{c}/settings",
                         {"channelType": "FreqScanner", "FreqScannerSettings": {"running": 1 if running else 0}})


def _main(argv):
    sa = SDRangel()
    if not argv or argv[0] in ("status", "instance"):
        print(json.dumps(sa.instance_summary(), indent=2)); return
    cmd = argv[0]
    if cmd == "devicesets":
        print(json.dumps(sa.devicesets(), indent=2))
    elif cmd == "deviceset":
        print(json.dumps(sa.deviceset(int(argv[1])), indent=2))
    elif cmd == "channel":
        print(json.dumps(sa.channel_settings(int(argv[1]), int(argv[2])), indent=2))
    elif cmd == "set-squelch":
        print(json.dumps(sa.set_squelch(int(argv[1]), int(argv[2]), float(argv[3])), indent=2))
    elif cmd == "set-freq":
        print(json.dumps(sa.set_center_freq(int(argv[1]), int(float(argv[2]))), indent=2))
    elif cmd == "scan":
        print(json.dumps(sa.scanner_set_running(int(argv[1]), int(argv[2]), argv[3] == "on"), indent=2))
    else:
        print(__doc__)


if __name__ == "__main__":
    _main(sys.argv[1:])
