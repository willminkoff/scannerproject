#!/usr/bin/env python3
"""Build the standalone rtl-airband config for ONE service.

Replaces ``scripts/build-combined-config.py`` for the MA/SL split-
process architecture.  Each rtl-airband systemd unit invokes this
script once as ExecStartPre to produce its own per-service config:

    rtl-airband-airband.service:
        ExecStartPre=... build-service-config.py --service airband
        ExecStart=...    /run/rtl_airband_airband_runtime.conf

    rtl-airband-ground.service:
        ExecStartPre=... build-service-config.py --service ground
        ExecStart=...    /run/rtl_airband_ground_runtime.conf

The script resolves the active profile via the same symlink lookups
build-combined-config.py used (CONFIG_SYMLINK for airband,
GROUND_CONFIG_PATH for ground), then renders a complete rtl-airband
config that targets ONE tuner of the RSPduo in MA mode (airband) or
SL mode (ground).  Service configs are isolated — neither side knows
about the other.

See ``docs/rspduo_ma_sl_split.md`` for the architectural rationale.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from combined_config import (  # noqa: E402 — sys.path setup above
    build_service_config,
    extract_devices_payload,
    profile_ui_disabled,
)

logger = logging.getLogger(__name__)


# Service → defaults table.  Anything overridable from env stays in env
# (CONFIG_SYMLINK, GROUND_CONFIG_PATH) so operators can repoint the
# active-profile-resolver without editing the script; service-specific
# constants (mixer name, mount, stats path, runtime config path) are
# pinned here because they ARE the architectural contract.
SERVICE_DEFAULTS = {
    "airband": {
        "active_profile_symlink_env": "CONFIG_SYMLINK",
        "active_profile_symlink_default": "/usr/local/etc/rtl_airband.conf",
        "fallback_profile_env": "AIRBAND_FALLBACK_PROFILE_PATH",
        "fallback_profile_default": "/usr/local/etc/airband-profiles/rtl_airband_airband.conf",
        "runtime_config_env": "RTL_AIRBAND_AIRBAND_RUNTIME_PATH",
        "runtime_config_default": "/run/rtl_airband_airband_runtime.conf",
        "stats_filepath_env": "RTL_AIRBAND_AIRBAND_STATS_PATH",
        "stats_filepath_default": "/run/rtl_airband_airband_stats.txt",
        "mixer_name": "combined_airband",
        "mount_name": "ANALOG.mp3",
    },
    "ground": {
        "active_profile_symlink_env": "GROUND_CONFIG_PATH",
        "active_profile_symlink_default": "/usr/local/etc/rtl_airband_ground.conf",
        "fallback_profile_env": "GROUND_FALLBACK_PROFILE_PATH",
        "fallback_profile_default": "/usr/local/etc/airband-profiles/rtl_airband_wx.conf",
        "runtime_config_env": "RTL_AIRBAND_GROUND_RUNTIME_PATH",
        "runtime_config_default": "/run/rtl_airband_ground_runtime.conf",
        "stats_filepath_env": "RTL_AIRBAND_GROUND_STATS_PATH",
        "stats_filepath_default": "/run/rtl_airband_ground_stats.txt",
        "mixer_name": "combined_ground",
        "mount_name": "ANALOG_GROUND.mp3",
    },
}


def _env(name: str, default: str) -> str:
    """Trim+default an env var read."""
    raw = os.getenv(name, default)
    if raw is None:
        return default
    value = raw.strip()
    return value or default


def _existing_file(path: str) -> str:
    try:
        if path and os.path.isfile(path):
            return os.path.realpath(path)
    except Exception:
        logger.debug(
            "build_service_config: failed to resolve %s",
            path,
            exc_info=True,
        )
    return ""


def _resolve_profile_path(primary: str, fallback: str) -> str:
    """Match the resolution order build-combined-config.py used: primary
    first (including its realpath), then fallback.  Raises if neither
    file exists."""
    candidates = []
    if primary:
        candidates.append(primary)
        try:
            rp = os.path.realpath(primary)
            if rp and rp not in candidates:
                candidates.append(rp)
        except Exception:
            logger.debug(
                "build_service_config: realpath(%s) failed",
                primary,
                exc_info=True,
            )
    if fallback and fallback not in candidates:
        candidates.append(fallback)
    for candidate in candidates:
        resolved = _existing_file(candidate)
        if resolved:
            return resolved
    raise FileNotFoundError(
        f"No readable profile found. primary={primary!r} fallback={fallback!r}"
    )


def _profile_has_usable_devices(path: str) -> bool:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        logger.debug(
            "build_service_config: failed to read %s",
            path,
            exc_info=True,
        )
        return False
    if profile_ui_disabled(text):
        return False
    return bool(extract_devices_payload(text))


def _bitrate_from_env() -> int:
    raw = os.getenv("ANALOG_STREAM_BITRATE_KBPS", "24")
    try:
        return max(8, min(320, int(raw)))
    except Exception:
        return 24


def _disco_bitrate_from_env() -> int:
    raw = os.getenv("DISCO_STREAM_BITRATE_KBPS", "32")
    try:
        return max(8, min(320, int(raw)))
    except Exception:
        return 32


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--service",
        required=True,
        choices=("airband", "ground"),
        help="Which rtl-airband service config to render.",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Render to stdout instead of writing the runtime config file. "
             "Useful for dry-runs in tests.",
    )
    args = parser.parse_args(argv)

    defaults = SERVICE_DEFAULTS[args.service]

    active_symlink = _env(
        defaults["active_profile_symlink_env"],
        defaults["active_profile_symlink_default"],
    )
    fallback = _env(
        defaults["fallback_profile_env"],
        defaults["fallback_profile_default"],
    )
    runtime_config_path = _env(
        defaults["runtime_config_env"],
        defaults["runtime_config_default"],
    )
    stats_filepath = _env(
        defaults["stats_filepath_env"],
        defaults["stats_filepath_default"],
    )

    profile_path = _resolve_profile_path(active_symlink, fallback)
    # If the active profile is ui_disabled and the fallback is also
    # broken, fall through to the fallback resolution to avoid
    # crashing rtl-airband on startup.
    if not _profile_has_usable_devices(profile_path):
        try:
            profile_path = _resolve_profile_path("", fallback)
            if not _profile_has_usable_devices(profile_path):
                raise RuntimeError(
                    f"both active profile and fallback {fallback!r} are "
                    f"unusable for service={args.service!r}"
                )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"no usable profile for service={args.service!r}: {exc}"
            )

    rendered = build_service_config(
        profile_path=profile_path,
        service=args.service,
        mixer_name=defaults["mixer_name"],
        mount_name=os.getenv(
            "MOUNT_NAME_OVERRIDE_" + args.service.upper(),
            defaults["mount_name"],
        ),
        stats_filepath=stats_filepath,
        analog_continuous=os.getenv("ANALOG_CONTINUOUS", "1").strip().lower()
                          in ("1", "true", "yes", "on"),
        mixer_output_continuous=os.getenv("MIXER_OUTPUT_CONTINUOUS", "1")
                                .strip().lower() in ("1", "true", "yes", "on"),
        analog_bitrate_kbps=_bitrate_from_env(),
        include_disco_mixer=(args.service == "airband"),  # Listen feature is airband-only for v1
        disco_mount_name=os.getenv("DISCO_MOUNT_NAME", "disco.mp3").strip()
                          or "disco.mp3",
        disco_bitrate_kbps=_disco_bitrate_from_env(),
    )

    if args.print_only:
        sys.stdout.write(rendered)
        return 0

    out_dir = os.path.dirname(runtime_config_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp = runtime_config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(rendered)
    os.replace(tmp, runtime_config_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
