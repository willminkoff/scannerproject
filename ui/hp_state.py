"""Persistent state for HomePatrol-style scan mode."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from .service_types import get_default_enabled_service_types


_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DB_PATH = str((_REPO_ROOT / "data" / "homepatrol.db").resolve())
_DEFAULT_STATE_PATH = (_REPO_ROOT / "data" / "hp_state.json").resolve()


def _coerce_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _coerce_float(value, default: float) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return float(default)


def _coerce_service_tags(value) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    if isinstance(value, list):
        raw = value
    else:
        raw = []
    for item in raw:
        try:
            tag = int(str(item).strip())
        except Exception:
            continue
        if tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


def _coerce_favorites(value) -> list[dict]:
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "id": str(item.get("id") or "").strip(),
                "type": str(item.get("type") or "").strip().lower(),
                "target": str(item.get("target") or "").strip().lower(),
                "profile_id": str(item.get("profile_id") or item.get("profileId") or "").strip(),
                "label": str(item.get("label") or item.get("name") or "").strip(),
                "enabled": _coerce_bool(item.get("enabled"), default=False),
            }
        )
    return out


def _coerce_avoid_list(value) -> list[dict]:
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    for index, item in enumerate(value):
        if isinstance(item, dict):
            out.append(
                {
                    "id": str(item.get("id") or f"item-{index}").strip(),
                    "label": str(
                        item.get("label")
                        or item.get("alpha_tag")
                        or item.get("name")
                        or f"Avoid {index + 1}"
                    ).strip(),
                    "type": str(item.get("type") or "item").strip(),
                }
            )
            continue
        token = str(item or "").strip()
        if not token:
            continue
        out.append(
            {
                "id": f"item-{index}",
                "label": token,
                "type": "item",
            }
        )
    return out


def _coerce_custom_favorites(value) -> list[dict]:
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in {"trunked", "conventional"}:
            continue
        entry = {
            "id": str(item.get("id") or f"fav-{index+1}").strip() or f"fav-{index+1}",
            "kind": kind,
            "system_name": str(item.get("system_name") or "").strip(),
            "department_name": str(item.get("department_name") or "").strip(),
            "alpha_tag": str(item.get("alpha_tag") or item.get("channel_name") or "").strip(),
            "service_tag": 0,
            "talkgroup": "",
            "control_channels": [],
            "frequency": 0.0,
        }
        try:
            entry["service_tag"] = int(str(item.get("service_tag") or "0").strip())
        except Exception:
            entry["service_tag"] = 0

        if kind == "trunked":
            tg = str(item.get("talkgroup") or item.get("tgid") or "").strip()
            if tg and tg.isdigit():
                entry["talkgroup"] = tg
            raw_controls = item.get("control_channels")
            controls_in = raw_controls if isinstance(raw_controls, list) else []
            controls: list[float] = []
            seen: set[float] = set()
            for raw in controls_in:
                try:
                    mhz = float(str(raw).strip())
                except Exception:
                    continue
                if not math.isfinite(mhz) or mhz <= 0:
                    continue
                mhz = round(mhz, 6)
                if mhz in seen:
                    continue
                seen.add(mhz)
                controls.append(mhz)
            entry["control_channels"] = controls
            if not entry["system_name"] and not controls:
                continue
            if not entry["talkgroup"]:
                continue
        else:
            try:
                mhz = float(str(item.get("frequency") or "0").strip())
            except Exception:
                mhz = 0.0
            if not math.isfinite(mhz) or mhz <= 0:
                continue
            entry["frequency"] = round(mhz, 6)
        out.append(entry)
    return out


@dataclass
class HPState:
    mode: str = "full_database"
    use_location: bool = False
    lat: float = 0.0
    lon: float = 0.0
    range_miles: float = 15.0
    nationwide_systems: bool = False
    enabled_service_tags: list[int] = field(default_factory=list)
    favorites: list[dict] = field(default_factory=list)
    favorites_name: str = "My Favorites"
    custom_favorites: list[dict] = field(default_factory=list)
    avoid_list: list[dict] = field(default_factory=list)

    @classmethod
    def default(cls, db_path: str = _DEFAULT_DB_PATH) -> "HPState":
        try:
            defaults = list(get_default_enabled_service_types(db_path=db_path))
        except Exception:
            defaults = [1, 2, 3, 4]
        return cls(
            mode="full_database",
            use_location=False,
            lat=0.0,
            lon=0.0,
            range_miles=15.0,
            nationwide_systems=False,
            enabled_service_tags=defaults,
            favorites=[],
            favorites_name="My Favorites",
            custom_favorites=[],
            avoid_list=[],
        )

    def to_dict(self) -> dict:
        return {
            "mode": str(self.mode or "full_database"),
            "use_location": bool(self.use_location),
            "lat": float(self.lat),
            "lon": float(self.lon),
            "range_miles": float(self.range_miles),
            "nationwide_systems": bool(self.nationwide_systems),
            "enabled_service_tags": [int(tag) for tag in (self.enabled_service_tags or [])],
            "favorites": _coerce_favorites(self.favorites),
            "favorites_name": str(self.favorites_name or "My Favorites").strip() or "My Favorites",
            "custom_favorites": _coerce_custom_favorites(self.custom_favorites),
            "avoid_list": _coerce_avoid_list(self.avoid_list),
        }

    def save(self, path: str = str(_DEFAULT_STATE_PATH)) -> None:
        out_path = Path(path).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)

    @classmethod
    def load(
        cls,
        path: str = str(_DEFAULT_STATE_PATH),
        db_path: str = _DEFAULT_DB_PATH,
    ) -> "HPState":
        in_path = Path(path).expanduser().resolve()
        if not in_path.is_file():
            return cls.default(db_path=db_path)

        try:
            with in_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return cls.default(db_path=db_path)

        if not isinstance(payload, dict):
            return cls.default(db_path=db_path)

        default_state = cls.default(db_path=db_path)
        mode = str(payload.get("mode") or default_state.mode).strip().lower()
        if mode not in {"full_database", "favorites"}:
            mode = default_state.mode

        service_tags = _coerce_service_tags(payload.get("enabled_service_tags"))
        if not service_tags:
            service_tags = list(default_state.enabled_service_tags)

        return cls(
            mode=mode,
            use_location=_coerce_bool(payload.get("use_location"), default=default_state.use_location),
            lat=_coerce_float(payload.get("lat"), default=default_state.lat),
            lon=_coerce_float(payload.get("lon"), default=default_state.lon),
            range_miles=max(0.0, _coerce_float(payload.get("range_miles"), default=default_state.range_miles)),
            nationwide_systems=_coerce_bool(
                payload.get("nationwide_systems"),
                default=default_state.nationwide_systems,
            ),
            enabled_service_tags=service_tags,
            favorites=_coerce_favorites(payload.get("favorites")),
            favorites_name=str(payload.get("favorites_name") or default_state.favorites_name).strip()
            or default_state.favorites_name,
            custom_favorites=_coerce_custom_favorites(payload.get("custom_favorites")),
            avoid_list=_coerce_avoid_list(payload.get("avoid_list")),
        )
