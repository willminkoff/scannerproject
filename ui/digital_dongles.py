"""Shared digital dongle accounting helpers."""
from __future__ import annotations

import os
from typing import Callable, Iterable

_TRUTHY = {"1", "true", "yes", "on"}
_DEFAULT_VDL2_SENTINEL = "/run/user/1000/vdl2_dongle_reserved"


def _env(env: dict[str, str] | None = None) -> dict[str, str]:
    return os.environ if env is None else env


def vdl2_share_enabled(*, env: dict[str, str] | None = None) -> bool:
    return str(_env(env).get("OP25_VDL2_TRAFFIC_SHARE", "1")).strip().lower() in _TRUTHY


def vdl2_serial(*, env: dict[str, str] | None = None) -> str:
    return str(_env(env).get("VDL2_RTL_SERIAL", "")).strip()


def vdl2_sentinel_path(*, env: dict[str, str] | None = None) -> str:
    return str(_env(env).get("OP25_VDL2_SENTINEL", _DEFAULT_VDL2_SENTINEL)).strip()


def vdl2_reserved(
    *,
    env: dict[str, str] | None = None,
    exists: Callable[[str], bool] | None = None,
) -> bool:
    probe = os.path.exists if exists is None else exists
    serial = vdl2_serial(env=env)
    if not serial or not vdl2_share_enabled(env=env):
        return False
    return bool(probe(vdl2_sentinel_path(env=env)))


def digital_first_serials(
    serials: Iterable[str],
    *,
    env: dict[str, str] | None = None,
    exists: Callable[[str], bool] | None = None,
) -> list[str]:
    """Return unique digital-capacity serials including a free VDL2 serial."""
    result: list[str] = []
    for candidate in serials:
        value = str(candidate or "").strip()
        if value and value not in result:
            result.append(value)
    shared = vdl2_serial(env=env)
    if shared and vdl2_share_enabled(env=env) and not vdl2_reserved(env=env, exists=exists):
        if shared not in result:
            result.append(shared)
    return result
