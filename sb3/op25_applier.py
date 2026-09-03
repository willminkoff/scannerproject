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
from typing import Any, Dict


DEFAULT_TARGET_URL = os.environ.get("SB3_OP25_APPLY_URL", "http://100.114.219.115:9200")
APPLY_TIMEOUT_SEC = float(os.environ.get("SB3_OP25_APPLY_TIMEOUT_SEC", "45"))


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
    target_url = str(profile.get("target_host_url") or DEFAULT_TARGET_URL).rstrip("/")
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
