"""sb3.op25_applier — apply a digital profile to op25 on Venus.

Reads a digital-role Profile with backend='op25', transforms it into the
op25-log-server /apply-profile blob, and POSTs. Venus writes the profile
directory, flips the active symlink, and restarts scanner-digital-op25.

Digital profile schema (backend='op25' variant):

    {
      "name": "nashville_curated",
      "role": "digital",
      "backend": "op25",
      "target_host_url": "http://100.114.219.115:9200",
      "systems": [
        {
          "name": "MTRTRS",
          "preferred_tuner_serial": "RSPduo Tuner 1 SER#1809063632",
          "control_channels_mhz": [856.4875, 856.7125, 857.0375, 857.4875],
          "gains": "IFGR:20,RFGR:0"
        },
        {
          "name": "TACN",
          "preferred_tuner_serial": "RSPduo Tuner 2 SER#1809063632",
          "control_channels_mhz": [852.9875, 853.7375],
          "gains": "IFGR:20,RFGR:0"
        }
      ],
      "talkgroups": [
        {"dec": 3207,  "hex": "C87",  "mode": "D", "alpha": "Vandy PD"},
        {"dec": 47008, "hex": "B7A0", "mode": "D", "alpha": "THP D3 Disp"}
      ]
    }
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


APPLY_TIMEOUT_SEC = float(os.environ.get("SB3_OP25_APPLY_TIMEOUT_SEC", "45"))

# There is no DEFAULT_TARGET_URL any more, and that is the point. It used to be
#
#     DEFAULT_TARGET_URL = os.environ.get("SB3_OP25_APPLY_URL", "http://<the rack>:9200")
#
# which had an override and still defaulted to a real radio -- and being module
# level, it froze at import, so a caller or a test could not change it without
# reloading. Resolve at call time instead, and refuse when nothing is set.
REFUSAL = ("SB3_OP25_APPLY_URL is not set; refusing to guess a host for "
           "/apply-profile")


def resolve_apply_target(profile: Optional[Dict[str, Any]] = None) -> str:
    """Where /apply-profile should be POSTed, or "" meaning refuse.

    A profile may carry its own ``target_host_url``; otherwise the environment
    decides, and an unset environment means refuse rather than guess. Arriving
    at /apply-profile rewrites talkgroups.csv and control_channels.txt, repoints
    ACTIVE_LINK and runs three ``systemctl restart`` calls, so the request IS
    the action -- the same reasoning as ``_VENUS_POWER_URL`` in sb3/ui/routes.py.
    Loopback is specifically unsafe here: op25-log-server and
    sdrtrunk-log-server both default to port 9200.

    Requiring a scheme is not pedantry. ``.rstrip("/")`` does not strip
    whitespace, so a plist value pasted with padding is truthy and sails past a
    bare ``if not target`` -- and then ``Request("   /apply-profile")`` raises
    ValueError for a missing scheme, from OUTSIDE the try block at both call
    sites, where nothing catches it and the failure surfaces as a cheerful
    HTTP 200. The two sibling guards pin the same case by test:
    tests/test_power_url_not_hardcoded.py and
    tests/test_chirp_host_not_hardcoded.py both cover "", "   " and "\t".
    """
    raw = str((profile or {}).get("target_host_url") or "")
    if not raw.strip():
        raw = os.environ.get("SB3_OP25_APPLY_URL") or ""
    target = raw.strip().rstrip("/")
    if not target.startswith(("http://", "https://")):
        return ""
    return target


def _profile_to_blob(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a scannerproject digital profile into the op25 apply-profile blob."""
    name = str(profile.get("name") or "").strip()
    systems_out = []
    overrides_out: Dict[str, Dict[str, Any]] = {}
    dongles_out = []
    for sys in profile.get("systems") or []:
        s_name = str(sys.get("name") or "").strip()
        if not s_name:
            continue
        systems_out.append({
            "name": s_name,
            "control_channels_mhz": sys.get("control_channels_mhz") or [],
        })
        gains = str(sys.get("gains") or "").strip()
        if gains:
            overrides_out[s_name] = {"gains": gains}
        tuner = str(sys.get("preferred_tuner_serial") or "").strip()
        if tuner:
            dongles_out.append({
                "system_name": s_name,
                "preferred_tuner_serial": tuner,
            })

    return {
        "name": name,
        "systems": systems_out,
        "talkgroups": profile.get("talkgroups") or [],
        "op25_overrides": overrides_out,
        "dongle_assignments": dongles_out,
        "activate": bool(profile.get("activate", True)),
    }


def apply_digital_profile_op25(profile: Dict[str, Any]) -> Dict[str, Any]:
    """POST the profile to op25-log-server on Venus. Returns Venus's response."""
    # Guard the RESOLVED value, not the default. The `or` above used to collapse
    # an unset variable AND a profile carrying "target_host_url": "" or null into
    # the same empty string, then build the Request anyway -- and the try below
    # opens after it, catching only HTTPError/URLError/TimeoutError/OSError, so a
    # ValueError from a schemeless URL escaped this module entirely.
    target_url = resolve_apply_target(profile)
    if not target_url:
        return {"ok": False, "error": REFUSAL}
    blob = _profile_to_blob(profile)
    if not blob.get("name"):
        return {"ok": False, "error": "profile has no name"}

    data = json.dumps(blob).encode()
    req = urllib.request.Request(
        f"{target_url}/apply-profile",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=APPLY_TIMEOUT_SEC) as resp:
            raw = resp.read()
            try:
                out = json.loads(raw.decode("utf-8"))
            except Exception:
                return {"ok": False, "error": f"non-JSON reply: {raw[:200]!r}"}
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        return {"ok": False, "error": f"HTTP {exc.code}: {body[:300]}"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": f"connection failed: {exc!r}"}

    out.setdefault("backend", "op25")
    out.setdefault("target_url", target_url)
    return out
