"""Pre-flight validation for rtl-airband combined configs.

Why this exists
---------------
On 2026-05-26 the operator selected the ``bandscan_marine`` profile via
``/api/profile``.  That profile predates the RTL-SDR → RSPduo migration
and still declares::

    {
      type = "rtlsdr";
      serial = "70613472";   # legacy ground RTL — no longer plugged in
      ...
    }

The compose step happily merged this into the combined config.
rtl_airband then crash-looped (``RTLSDR device with serial number
70613472 not found``) and systemd kept restarting it, taking down
analog + ground audio for ~25 minutes before someone noticed.

This module gives the API layer a way to reject such mistakes
*before* they hit the live config + restart path:

    result = validate_combined_config_text(prospective_text)
    if not result["ok"]:
        return 409, {"issues": result["issues"]}

The validation is deliberately conservative — it only flags
configurations that *cannot* possibly succeed:

  - A ``type="rtlsdr"`` block whose serial isn't currently visible
    to ``rtl_test`` (the dongle is unplugged or dead).

  - A ``type="soapysdr"; driver=sdrplay`` block whose serial isn't
    in the RSPduo USB enumeration (the RSPduo isn't plugged in).

  - Two blocks claiming the same ``(serial, tuner)`` pair —
    SoapySDR will fail the second open with ``LIBUSB_BUSY``.

  - More than 2 tuners claimed on a single RSPduo — DT mode
    allows at most 2 (Tuner 1 + Tuner 2).

It does NOT try to validate everything that could go wrong; gain
ranges, squelch values, freq band coverage, etc. are out of scope.
The narrow contract is: reject configs that name nonexistent
hardware or oversubscribe tuners.

Enumeration is dependency-injected so unit tests can pin the device
set without touching ``rtl_test`` / sysfs.  Production callers use
``validate_combined_config_text()`` which discovers devices itself.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, Iterable, List, Optional, Set

try:
    from .combined_status import parse_combined_devices_text
except ImportError:  # pragma: no cover
    from ui.combined_status import parse_combined_devices_text  # type: ignore


logger = logging.getLogger(__name__)


def _normalize_serial(value) -> str:
    """Lowercase + strip whitespace.  RSPduo and RTL-SDR serials are
    ASCII-only in practice so casefold == lower."""
    if value is None:
        return ""
    return str(value).strip().lower()


def _normalize_set(values: Iterable) -> Set[str]:
    return {_normalize_serial(v) for v in (values or []) if _normalize_serial(v)}


def _default_rtl_serials() -> Set[str]:
    try:
        try:
            from .favorites_runtime import _enumerated_rtl_serials
        except ImportError:  # pragma: no cover
            from ui.favorites_runtime import _enumerated_rtl_serials  # type: ignore
    except Exception:
        logger.debug("config_validator: rtl enumerator import failed", exc_info=True)
        return set()
    try:
        result = _enumerated_rtl_serials()
    except Exception:
        logger.debug("config_validator: rtl enumerator raised", exc_info=True)
        return set()
    # ``_enumerated_rtl_serials`` returns ``None`` when ``rtl_test``
    # itself failed (binary missing, timeout); in that case we don't
    # know what's actually plugged in, and refusing to validate would
    # block legitimate config changes.  Treat the set as universal —
    # i.e. don't flag any rtlsdr block.  The mount-publishing /
    # sample-flow signals will catch a downstream failure.
    if result is None:
        return _UNIVERSAL
    return _normalize_set(result)


def _default_sdrplay_serials() -> Set[str]:
    try:
        try:
            from .favorites_runtime import _rspduo_usb_sample
        except ImportError:  # pragma: no cover
            from ui.favorites_runtime import _rspduo_usb_sample  # type: ignore
    except Exception:
        logger.debug("config_validator: rspduo enumerator import failed", exc_info=True)
        return set()
    try:
        _count, serials = _rspduo_usb_sample()
    except Exception:
        logger.debug("config_validator: rspduo enumerator raised", exc_info=True)
        return set()
    return _normalize_set(serials)


# Sentinel: validator should NOT enforce the corresponding membership
# rule (because we couldn't enumerate that device class).  Identity-
# tested via ``is _UNIVERSAL`` so it survives ``_normalize_set``'s
# defensive copy.
class _UniversalSet(frozenset):
    def __contains__(self, _item) -> bool:  # type: ignore[override]
        return True


_UNIVERSAL: Set[str] = _UniversalSet()  # type: ignore[assignment]


def _maybe_universal(value) -> bool:
    """Identity test for the universal sentinel.  Preserves the
    'skip enforcement' semantic across ``_normalize_set`` defensive
    copies that would otherwise replace it with a plain ``set``."""
    return value is _UNIVERSAL or isinstance(value, _UniversalSet)


# Per RSPduo DT-mode limit: at most 2 tuners per physical device.
MAX_TUNERS_PER_RSPDUO = 2


def validate_combined_config_text(
    combined_text: str,
    *,
    plugged_rtl_serials: Optional[Iterable[str]] = None,
    plugged_sdrplay_serials: Optional[Iterable[str]] = None,
    rtl_enumerator: Optional[Callable[[], Set[str]]] = None,
    sdrplay_enumerator: Optional[Callable[[], Set[str]]] = None,
) -> Dict:
    """Validate a prospective combined config for "obviously broken"
    device declarations.

    Parameters
    ----------
    combined_text:
        Raw libconfig text — the contents of the combined config
        rtl-airband would be asked to load.
    plugged_rtl_serials, plugged_sdrplay_serials:
        Pre-discovered device sets, for tests or environments where
        the auto-discoverers are unavailable.  Either may be omitted
        independently; whatever isn't supplied will be discovered via
        the enumerator callbacks (defaulting to the favorites_runtime
        helpers).  Pass the sentinel ``None`` to use defaults.
    rtl_enumerator, sdrplay_enumerator:
        Override callables for the auto-discoverers; primarily for
        tests.

    Returns
    -------
    A dict with:
      - ``ok``     : bool — True iff zero issues
      - ``issues`` : list[dict] — each entry has ``code``, ``detail``,
                     and ``device`` (a dict describing the offending
                     block)
      - ``checked_devices`` : list[dict] — every device block parsed,
                              with the validator's verdict per device
                              for easier dashboard rendering
    """
    devices = parse_combined_devices_text(combined_text or "")

    if plugged_rtl_serials is None:
        rtl_set = (rtl_enumerator or _default_rtl_serials)()
    elif _maybe_universal(plugged_rtl_serials):
        rtl_set = _UNIVERSAL
    else:
        rtl_set = _normalize_set(plugged_rtl_serials)
    if plugged_sdrplay_serials is None:
        sdrplay_set = (sdrplay_enumerator or _default_sdrplay_serials)()
    elif _maybe_universal(plugged_sdrplay_serials):
        sdrplay_set = _UNIVERSAL
    else:
        sdrplay_set = _normalize_set(plugged_sdrplay_serials)

    issues: List[Dict] = []
    checked: List[Dict] = []
    tuner_claims: Dict[tuple, int] = {}      # (serial, tuner) -> count
    rspduo_tuners: Dict[str, Set[str]] = {}  # serial -> set of tuners

    for idx, dev in enumerate(devices):
        dev_type = (dev.get("device_type") or "").strip().lower()
        serial = _normalize_serial(dev.get("serial"))
        soapy_kwargs = dev.get("soapy_kwargs") or {}
        soapy_driver = (dev.get("soapy_driver") or "").strip().lower()
        block_issues: List[Dict] = []

        if dev_type == "rtlsdr":
            if not serial:
                block_issues.append({
                    "code": "rtlsdr_missing_serial",
                    "detail": "rtlsdr block has no serial field",
                    "device_index": idx,
                })
            elif serial not in rtl_set:
                block_issues.append({
                    "code": "rtlsdr_serial_not_enumerated",
                    "detail": (
                        f"rtlsdr serial {serial!r} is not visible to "
                        "rtl_test; dongle not plugged in or driver "
                        "not loaded"
                    ),
                    "device_index": idx,
                    "serial": serial,
                })
        elif dev_type == "soapysdr":
            if soapy_driver == "sdrplay":
                if not serial:
                    block_issues.append({
                        "code": "sdrplay_missing_serial",
                        "detail": (
                            "soapysdr block with driver=sdrplay has no "
                            "serial in device_string"
                        ),
                        "device_index": idx,
                    })
                elif not _maybe_universal(sdrplay_set) and serial not in sdrplay_set:
                    block_issues.append({
                        "code": "sdrplay_serial_not_enumerated",
                        "detail": (
                            f"sdrplay serial {serial!r} is not present "
                            "in the RSPduo USB enumeration; device not "
                            "plugged in"
                        ),
                        "device_index": idx,
                        "serial": serial,
                    })
                tuner = str(soapy_kwargs.get("tuner") or "").strip()
                if tuner:
                    key = (serial, tuner)
                    tuner_claims[key] = tuner_claims.get(key, 0) + 1
                    if serial:
                        rspduo_tuners.setdefault(serial, set()).add(tuner)
            # Other soapysdr drivers (hackrf, airspy, etc.) are not
            # currently used in this stack — pass them through unchecked
            # rather than false-positive.
        elif dev_type:
            # Unknown device_type — surface it as informational rather
            # than blocking, since some experimental configs may use
            # types this validator doesn't know about.
            block_issues.append({
                "code": "unknown_device_type",
                "detail": f"unrecognized device type {dev_type!r}",
                "device_index": idx,
                "blocking": False,
            })
        else:
            block_issues.append({
                "code": "missing_device_type",
                "detail": "device block has no type= field",
                "device_index": idx,
            })

        checked.append({
            "device_index": idx,
            "type": dev_type or "",
            "serial": serial,
            "soapy_kwargs": soapy_kwargs,
            "issues": block_issues,
        })
        issues.extend(block_issues)

    # Tuner-collision check: same (serial, tuner) claimed twice.
    for (serial, tuner), count in tuner_claims.items():
        if count > 1:
            issues.append({
                "code": "duplicate_tuner_claim",
                "detail": (
                    f"two device blocks both claim RSPduo serial "
                    f"{serial!r} tuner {tuner!r}; SoapySDR will fail "
                    f"the second open with LIBUSB_BUSY"
                ),
                "serial": serial,
                "tuner": tuner,
            })

    # DT-mode capacity check: more than 2 tuners on one physical RSPduo.
    for serial, tuners in rspduo_tuners.items():
        if len(tuners) > MAX_TUNERS_PER_RSPDUO:
            issues.append({
                "code": "rspduo_too_many_tuners",
                "detail": (
                    f"RSPduo serial {serial!r} has {len(tuners)} "
                    f"tuners declared ({sorted(tuners)}); DT mode "
                    f"supports a maximum of {MAX_TUNERS_PER_RSPDUO}"
                ),
                "serial": serial,
                "tuners": sorted(tuners),
            })

    # A block-issue with explicit ``blocking: False`` doesn't make the
    # validation fail overall (informational only).
    blocking = [i for i in issues if i.get("blocking", True)]

    return {
        "ok": len(blocking) == 0,
        "issues": issues,
        "checked_devices": checked,
    }


# ---------------------------------------------------------------------------
# MA/SL split-process validation
#
# The split architecture (docs/rspduo_ma_sl_split.md) replaces the one
# combined config with TWO standalone per-service configs.  Each one
# passes the regular ``validate_combined_config_text`` rules individually,
# but there's a new cross-config invariant: together they must declare
# EXACTLY one Master (MA) on Tuner 1 and EXACTLY one Slave (SL) on
# Tuner 2, both on the same RSPduo serial.  A pair with mismatched
# serials, two MAs, two SLs, or a missing band wouldn't crash
# rtl-airband but would silently mis-route audio between bands.
# ---------------------------------------------------------------------------


def validate_dual_service_configs(
    airband_text: str,
    ground_text: str,
    *,
    plugged_rtl_serials: Optional[Iterable[str]] = None,
    plugged_sdrplay_serials: Optional[Iterable[str]] = None,
    rtl_enumerator: Optional[Callable[[], Set[str]]] = None,
    sdrplay_enumerator: Optional[Callable[[], Set[str]]] = None,
) -> Dict:
    """Validate the airband + ground configs as a coordinated pair.

    Runs the per-config validator on each, then checks the cross-
    config invariants the MA/SL architecture demands:

      - Each config declares exactly one device block.
      - airband_text's device is type=soapysdr, driver=sdrplay,
        mode=MA, tuner=1.
      - ground_text's device is type=soapysdr, driver=sdrplay,
        mode=SL, tuner=2.
      - Both reference the SAME RSPduo serial (otherwise MA/SL clock
        coordination won't work — they need to be the same physical
        device).

    Returns a dict shaped like ``validate_combined_config_text`` so
    callers can render it the same way: ``ok``, ``issues``, plus a
    ``per_service`` map showing each config's individual validation
    result.
    """
    per_service = {
        "airband": validate_combined_config_text(
            airband_text,
            plugged_rtl_serials=plugged_rtl_serials,
            plugged_sdrplay_serials=plugged_sdrplay_serials,
            rtl_enumerator=rtl_enumerator,
            sdrplay_enumerator=sdrplay_enumerator,
        ),
        "ground": validate_combined_config_text(
            ground_text,
            plugged_rtl_serials=plugged_rtl_serials,
            plugged_sdrplay_serials=plugged_sdrplay_serials,
            rtl_enumerator=rtl_enumerator,
            sdrplay_enumerator=sdrplay_enumerator,
        ),
    }

    cross_issues: List[Dict] = []

    # 1. Each config must have exactly one device block.
    for service_name, result in per_service.items():
        n_devices = len(result.get("checked_devices") or [])
        if n_devices != 1:
            cross_issues.append({
                "code": "service_config_wrong_device_count",
                "detail": (
                    f"{service_name} config has {n_devices} device "
                    f"blocks; the MA/SL architecture requires exactly 1"
                ),
                "service": service_name,
                "device_count": n_devices,
            })

    # 2. Inspect each service's single device for mode/tuner correctness.
    airband_dev = (per_service["airband"].get("checked_devices") or [None])[0]
    ground_dev = (per_service["ground"].get("checked_devices") or [None])[0]

    def _check_service_device(service_name, dev, expected_mode, expected_tuner):
        if dev is None:
            return None
        kwargs = dev.get("soapy_kwargs") or {}
        actual_mode = str(kwargs.get("mode") or "").upper()
        actual_tuner = str(kwargs.get("tuner") or "").strip()
        actual_driver = str(kwargs.get("driver") or "").lower()
        if dev.get("type") != "soapysdr":
            cross_issues.append({
                "code": "service_wrong_device_type",
                "detail": (
                    f"{service_name} device must be type=soapysdr "
                    f"(MA/SL is sdrplay-only); got type={dev.get('type')!r}"
                ),
                "service": service_name,
            })
            return None
        if actual_driver != "sdrplay":
            cross_issues.append({
                "code": "service_wrong_driver",
                "detail": (
                    f"{service_name} device must use driver=sdrplay; "
                    f"got driver={actual_driver!r}"
                ),
                "service": service_name,
            })
        if actual_mode != expected_mode:
            cross_issues.append({
                "code": "service_wrong_mode",
                "detail": (
                    f"{service_name} device must be mode={expected_mode} "
                    f"(MA/SL architecture); got mode={actual_mode!r}"
                ),
                "service": service_name,
                "expected_mode": expected_mode,
                "actual_mode": actual_mode,
            })
        if actual_tuner != expected_tuner:
            cross_issues.append({
                "code": "service_wrong_tuner",
                "detail": (
                    f"{service_name} device must be tuner={expected_tuner}; "
                    f"got tuner={actual_tuner!r}"
                ),
                "service": service_name,
                "expected_tuner": expected_tuner,
                "actual_tuner": actual_tuner,
            })
        return kwargs.get("serial")

    airband_serial = _check_service_device(
        "airband", airband_dev, "MA", "1"
    )
    ground_serial = _check_service_device(
        "ground", ground_dev, "SL", "2"
    )

    # 3. Both services must point at the same physical RSPduo serial.
    if (
        airband_serial and ground_serial
        and _normalize_serial(airband_serial) != _normalize_serial(ground_serial)
    ):
        cross_issues.append({
            "code": "service_serial_mismatch",
            "detail": (
                f"airband and ground services target different RSPduo "
                f"serials ({airband_serial!r} vs {ground_serial!r}); "
                f"MA/SL clock coordination requires the same physical "
                f"device"
            ),
            "airband_serial": airband_serial,
            "ground_serial": ground_serial,
        })

    all_issues = list(cross_issues)
    for result in per_service.values():
        for issue in result.get("issues") or []:
            # Skip duplicate-tuner / capacity errors that are vacuously
            # true on single-device configs.
            if issue.get("code") in (
                "duplicate_tuner_claim", "rspduo_too_many_tuners"
            ):
                continue
            all_issues.append(issue)

    blocking = [i for i in all_issues if i.get("blocking", True)]
    return {
        "ok": len(blocking) == 0,
        "issues": all_issues,
        "per_service": per_service,
    }
