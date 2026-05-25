"""Per-profile teardown / startup contract reader.

Background
----------
``ui/device_ownership.py`` covers the *implicit* case: it discovers
which decoders are holding the RTL serials about to be claimed by
the new combined config, and stops them.  That is enough to avoid
the LIBUSB_BUSY crash-loop, but it leaves two gaps:

  1. Decoders that don't claim a target serial but ARE redundant on
     the new profile (e.g. acarsdec running against a different
     dongle while the new profile selects a "no decoder" mode)
     remain running.  Operationally we'd rather stop them.
  2. Decoders that the new profile actively *requires* (acarsdec for
     the ACARS profile, radiosonde-auto-rx for the radiosonde
     profile) need to be started after rtl-airband settles.  Today
     this is hard-coded in action_set_profile via the ``_WX_START``
     map.

This module loads the declarative contract from
``profiles/profile_metadata.json`` so callers can get a clean list of
units to stop / start per profile id, without having to bake the
mapping into Python.

Schema
------
::

    {
      "schema_version": 1,
      "profiles": {
        "<profile_id>": {
          "requires_stop":   [unit, ...],
          "starts":          [unit, ...],
          "claims_serials":  [serial, ...]
        }
      }
    }

All three lists are optional and default to ``[]``.  Profile ids not
present in the file return an empty contract (no stops, no starts,
no claims).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_METADATA_PATH = os.getenv(
    "PROFILE_METADATA_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "profiles", "profile_metadata.json"),
)

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"mtime": 0.0, "path": "", "data": None}


def _load_raw(path: str) -> dict:
    """Read the metadata file, returning ``{}`` on any failure."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        logger.debug("profile_metadata: file not found at %s", path)
        return {}
    except Exception as exc:
        logger.warning("profile_metadata: failed to parse %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("profile_metadata: %s did not contain a top-level object", path)
        return {}
    return data


def _load_cached(path: str | None = None) -> dict:
    """Return parsed metadata, reloading on mtime change."""
    resolved = str(path or _DEFAULT_METADATA_PATH)
    try:
        mtime = os.path.getmtime(resolved)
    except FileNotFoundError:
        mtime = 0.0
    with _CACHE_LOCK:
        cached_path = str(_CACHE.get("path") or "")
        cached_mtime = float(_CACHE.get("mtime") or 0.0)
        cached_data = _CACHE.get("data")
        if (
            cached_data is not None
            and cached_path == resolved
            and cached_mtime == mtime
        ):
            return dict(cached_data)
        data = _load_raw(resolved)
        _CACHE["mtime"] = mtime
        _CACHE["path"] = resolved
        _CACHE["data"] = data
        return dict(data)


def _profile_entry(profile_id: str, path: str | None = None) -> dict:
    pid = str(profile_id or "").strip()
    if not pid:
        return {}
    data = _load_cached(path)
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, dict):
        return {}
    entry = profiles.get(pid)
    if not isinstance(entry, dict):
        return {}
    return entry


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        token = str(item or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def get_requires_stop(profile_id: str, path: str | None = None) -> list[str]:
    """Return the list of systemd units that should be stopped when
    switching TO ``profile_id``.

    Caller is expected to stop these via the normal release path
    (``ui.device_ownership._stop_unit`` with sudo).  Order is
    preserved from the metadata file.
    """
    return _string_list(_profile_entry(profile_id, path).get("requires_stop"))


def get_starts(profile_id: str, path: str | None = None) -> list[str]:
    """Return the list of systemd units to start AFTER rtl-airband
    has been restarted onto ``profile_id``.

    Used for declarative decoder activation (ACARS mode → acarsdec +
    dumpvdl2; radiosonde mode → radiosonde-auto-rx).
    """
    return _string_list(_profile_entry(profile_id, path).get("starts"))


def get_claims_serials(profile_id: str, path: str | None = None) -> list[str]:
    """Return additional RTL serials this profile explicitly claims.

    Merged with the serials discovered by parsing the combined config.
    Useful for profiles whose serial is selected dynamically at
    runtime (acarsdec resolves its serial via env vars, not via
    rtl-airband config — declaring it here lets the preflight stop
    other holders of that serial when switching INTO acarsdec mode).
    """
    return _string_list(_profile_entry(profile_id, path).get("claims_serials"))


def reset_cache_for_tests() -> None:
    """Test helper: clear the mtime cache so a fresh file is reloaded."""
    with _CACHE_LOCK:
        _CACHE["mtime"] = 0.0
        _CACHE["path"] = ""
        _CACHE["data"] = None
