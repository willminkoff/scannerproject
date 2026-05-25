"""Preflight: free RTL serials before rtl-airband restart.

Background
----------
``acarsdec``, ``dumpvdl2``, ``radiosonde-auto-rx``, and similar
single-dongle decoders take exclusive ownership of an RTL device via
librtlsdr.  When the operator switches rtl-airband onto a profile that
claims the same serial, ``rtl_airband`` fails with ``LIBUSB_BUSY``,
and ``Restart=on-failure`` then crash-loops the unit silently.

This module figures out which already-running services are claiming
the serials about-to-be-claimed by rtl-airband, and stops them before
the restart.  The function is idempotent (a no-op when there is no
conflict) and emits a structured log entry for each stop.

Discovery contract
------------------
The mapping of "claimant service" → "RTL serial it is holding" is
read from the airband-ui environment file (``/etc/airband-ui.conf``
by default).  The fallback chain mirrors each unit's ExecStart:

  acarsdec.service       : ACARS_RTL_SERIAL  ⇢ GROUND_RTL_SERIAL
  dumpvdl2.service       : VDL2_RTL_SERIAL
  radiosonde-auto-rx.service : RADIOSONDE_RTL_SERIAL

If neither configured env var resolves a serial AND the service is
active, the service is treated as a *potential* claimant (stopped
conservatively) because the unit's ExecStart contains its own
hard-coded default that may collide.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Iterable

try:
    from .systemd import unit_active, _stop_unit
except ImportError:  # pragma: no cover - test/dev fallback
    from ui.systemd import unit_active, _stop_unit  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


# (unit_name, primary env var holding the serial, optional fallback env var)
KNOWN_RTL_CLAIMANT_SERVICES: tuple[tuple[str, str, str | None], ...] = (
    ("acarsdec.service", "ACARS_RTL_SERIAL", "GROUND_RTL_SERIAL"),
    ("dumpvdl2.service", "VDL2_RTL_SERIAL", None),
    ("radiosonde-auto-rx.service", "RADIOSONDE_RTL_SERIAL", None),
)

DEFAULT_ENV_PATH = os.getenv("AIRBAND_UI_ENV_PATH", "/etc/airband-ui.conf")
USB_RELEASE_GRACE_SEC = max(0.1, float(os.getenv("RTL_RELEASE_GRACE_SEC", "0.5")))


def _read_env_file(path: str) -> dict[str, str]:
    """Parse a shell/systemd-style EnvironmentFile into a dict.

    Returns an empty dict on FileNotFoundError or permission denial.
    Honors ``key=value`` lines, ignores comments and blanks.  Strips
    surrounding single / double quotes from values.
    """
    env: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "=" not in stripped:
                    continue
                key, _, value = stripped.partition("=")
                value = value.strip()
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]
                env[key.strip()] = value
    except (FileNotFoundError, PermissionError):
        logger.debug("device_ownership: env file %s unreadable", path, exc_info=True)
    return env


def _resolve_claimant_serial(
    env: dict[str, str],
    primary: str,
    fallback: str | None,
) -> str:
    """Resolve a service's claimed serial via its env-var chain.

    Empty string means "unresolved" (caller may treat as potential
    conflict for active services).
    """
    for key in (primary, fallback):
        if not key:
            continue
        value = (env.get(key) or "").strip()
        if value:
            return value
    return ""


def discover_active_claimants(
    target_serials: Iterable[str],
    env_path: str = DEFAULT_ENV_PATH,
) -> list[tuple[str, str, str]]:
    """Return active services whose claimed serial is in ``target_serials``.

    Result shape: ``[(unit_name, claimed_serial, resolution_source)]``
    where ``resolution_source`` is "env" (serial came from env var) or
    "unknown" (env unresolved; treat as potential claimant for safety).
    """
    target_set = {str(s).strip() for s in target_serials if str(s).strip()}
    if not target_set:
        return []
    env = _read_env_file(env_path)
    out: list[tuple[str, str, str]] = []
    for unit, primary, fallback in KNOWN_RTL_CLAIMANT_SERVICES:
        if not unit_active(unit):
            continue
        serial = _resolve_claimant_serial(env, primary, fallback)
        if serial:
            if serial in target_set:
                out.append((unit, serial, "env"))
        else:
            # Active service, no env-resolved serial: conservatively
            # flag as potential claimant.  Caller can decide whether
            # to stop it based on the operating mode.
            out.append((unit, "", "unknown"))
    return out


def release_rtl_serials(
    target_serials: Iterable[str],
    *,
    env_path: str = DEFAULT_ENV_PATH,
    include_unknown: bool = True,
    grace_sec: float = USB_RELEASE_GRACE_SEC,
) -> list[dict[str, str]]:
    """Stop services holding any of ``target_serials``, return action log.

    Idempotent: if no claimants are running, returns ``[]`` without
    side effects.  When ``include_unknown`` is True (default), active
    services whose serial could not be resolved from env are also
    stopped — this is the safer default because their unit ExecStart
    typically embeds a hard-coded fallback that may collide.

    Sleeps ``grace_sec`` after stops to give librtlsdr time to release
    the USB endpoint before the caller restarts rtl-airband.

    Returns: list of ``{"unit", "serial", "source", "stopped", "error"}``
    dicts in the order the stops were attempted.  Suitable for direct
    inclusion in an API response payload.
    """
    actions: list[dict[str, str]] = []
    claimants = discover_active_claimants(target_serials, env_path=env_path)
    for unit, serial, source in claimants:
        if source == "unknown" and not include_unknown:
            actions.append({
                "unit": unit,
                "serial": "",
                "source": source,
                "stopped": False,
                "error": "skipped (include_unknown=False)",
            })
            continue
        try:
            # Decoder units (acarsdec / dumpvdl2 / radiosonde-auto-rx)
            # require sudo to stop; the airband-ui user holds NOPASSWD
            # sudoers entries for exactly these units.
            ok, err = _stop_unit(unit, use_sudo=True)
        except Exception as exc:  # pragma: no cover - belt-and-suspenders
            ok = False
            err = str(exc)
        if ok:
            logger.info(
                "device_ownership: stopped %s (serial=%s, source=%s) "
                "to free RTL for rtl-airband restart",
                unit,
                serial or "(unknown)",
                source,
            )
        else:
            logger.warning(
                "device_ownership: failed to stop %s (serial=%s, source=%s): %s",
                unit,
                serial or "(unknown)",
                source,
                err,
            )
        actions.append({
            "unit": unit,
            "serial": serial,
            "source": source,
            "stopped": bool(ok),
            "error": (err or "") if not ok else "",
        })
    if actions and grace_sec > 0:
        time.sleep(grace_sec)
    return actions


def serials_from_combined_config(conf_path: str) -> list[str]:
    """Return the list of RTL serials declared in a combined config.

    Tiny adapter over ui.combined_status.read_combined_devices so
    action paths can resolve target serials without duplicating
    config-parsing code.
    """
    try:
        from .combined_status import read_combined_devices
    except ImportError:  # pragma: no cover
        from ui.combined_status import read_combined_devices  # type: ignore[no-redef]
    out: list[str] = []
    for dev in read_combined_devices(conf_path):
        serial = str(dev.get("serial") or "").strip()
        if serial and serial not in out:
            out.append(serial)
    return out
