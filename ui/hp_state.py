"""Persistent state for HomePatrol-style scan mode."""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

from .config import HPDB_DB_PATH
from .service_types import get_default_enabled_service_types

logger = logging.getLogger(__name__)


_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DB_PATH = str(Path(HPDB_DB_PATH).expanduser().resolve())
_DEFAULT_STATE_PATH = (_REPO_ROOT / "data" / "hp_state.json").resolve()
_SERVICE_TAG_SCHEMA_VERSION = 2

# One-time migration from the previous, incorrect UI tag map.
_LEGACY_SERVICE_TAG_REMAP = {
    1: 15,   # Aircraft
    5: 4,    # EMS Dispatch
    9: 3,    # Fire Dispatch
    15: 2,   # Law Dispatch
    16: 7,   # Law Tac
    17: 23,  # Law Talk
    24: 14,  # Public Works
    25: 20,  # Railroad
    29: 33,  # Security
    30: 26,  # Transportation
    31: 34,  # Utilities
}


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


def _migrate_legacy_service_tags(tags: list[int], legacy_version: int) -> list[int]:
    if legacy_version >= _SERVICE_TAG_SCHEMA_VERSION:
        return list(tags)
    out: list[int] = []
    seen: set[int] = set()
    for raw_tag in tags:
        tag = int(_LEGACY_SERVICE_TAG_REMAP.get(int(raw_tag), int(raw_tag)))
        if tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


# Per-band classification helpers.  A favorite "has air" channels if any of
# its conventional rows fall in the AM/airband regions used by rtl-airband's
# airband instance (108-137 MHz civilian airband or 225-400 MHz mil-air).
# A favorite "has ground" channels if any conventional row falls outside
# those AM regions (137-225 MHz / 400+ MHz NFM).  Trunked rows do not
# contribute to either band classification — they route through OP25
# regardless of the per-band analog activation flags.
def _favorite_has_air_channels(custom_favorites: list[dict]) -> bool:
    for c in custom_favorites or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("kind") or "").strip().lower() != "conventional":
            continue
        try:
            f = float(c.get("frequency") or 0)
        except Exception:
            continue
        if (108.0 <= f < 137.0) or (225.0 <= f < 400.0):
            return True
    return False


def _favorite_has_ground_channels(custom_favorites: list[dict]) -> bool:
    for c in custom_favorites or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("kind") or "").strip().lower() != "conventional":
            continue
        try:
            f = float(c.get("frequency") or 0)
        except Exception:
            continue
        # Ground band = anything NFM-routed (i.e. non-airband).  Treat
        # 30-108 MHz as ground too (rare but legal).
        if 30.0 <= f and not ((108.0 <= f < 137.0) or (225.0 <= f < 400.0)):
            return True
    return False


def _favorite_has_digital_channels(custom_favorites: list[dict]) -> bool:
    """True if the favorite carries any P25 / trunked channels.

    Digital activation (enabled_digital) is independent of the analog
    AIR / GROUND bands and gates which favorite's trunked systems feed
    the OP25 scan pool.
    """
    for c in custom_favorites or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("kind") or "").strip().lower() == "trunked":
            return True
    return False


def _coerce_favorites(value) -> list[dict]:
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or "").strip()
        custom_favorites = _coerce_custom_favorites(item.get("custom_favorites"))
        enabled = _coerce_bool(item.get("enabled"), default=False)
        # ---- Per-band migration (idempotent) -----------------------------
        # New schema: each favorite has independent enabled_air /
        # enabled_ground flags so AIR + GROUND cards can pick different
        # favorites.  Legacy state has only `enabled` (a single flag that
        # toggled both bands at once).  On first read, derive the per-band
        # flags from the legacy `enabled` plus the favorite's channel
        # composition; persisted state will then carry the per-band flags
        # forward verbatim (so this branch becomes a no-op on subsequent
        # reads).  `enabled` itself is kept in sync as OR(enabled_air,
        # enabled_ground) so older tooling that only knows about `enabled`
        # keeps working.
        has_air_field = "enabled_air" in item
        has_ground_field = "enabled_ground" in item
        has_digital_field = "enabled_digital" in item
        if has_air_field:
            enabled_air = _coerce_bool(item.get("enabled_air"), default=False)
        else:
            enabled_air = bool(enabled and _favorite_has_air_channels(custom_favorites))
        if has_ground_field:
            enabled_ground = _coerce_bool(item.get("enabled_ground"), default=False)
        else:
            enabled_ground = bool(enabled and _favorite_has_ground_channels(custom_favorites))
        # Phase 7g (digital): each favorite carries its own enabled_digital
        # flag that gates whether its trunked / P25 systems feed the OP25
        # scan pool.  Derive from legacy `enabled` AND presence of trunked
        # channels on first read; persist verbatim thereafter.
        if has_digital_field:
            enabled_digital = _coerce_bool(item.get("enabled_digital"), default=False)
        else:
            enabled_digital = bool(enabled and _favorite_has_digital_channels(custom_favorites))
        # Keep legacy `enabled` consistent: any per-band activation implies
        # the favorite is "enabled" for tools that ignore the per-band split.
        enabled = bool(enabled_air or enabled_ground or enabled_digital)
        out.append(
            {
                "id": str(item.get("id") or "").strip(),
                "type": str(item.get("type") or "").strip().lower(),
                "target": str(item.get("target") or "").strip().lower(),
                "profile_id": str(item.get("profile_id") or item.get("profileId") or "").strip(),
                "label": label,
                "enabled": enabled,
                "enabled_air": enabled_air,
                "enabled_ground": enabled_ground,
                "enabled_digital": enabled_digital,
                "custom_favorites": custom_favorites,
            }
        )
    # Enforce mutex: only one favorite may hold enabled_air=True (same for
    # ground).  Migration of a state that historically had multiple
    # `enabled=true` rows (shouldn't happen but we defend anyway) leaves
    # the first as the active per-band fav and clears the rest.
    air_seen = False
    ground_seen = False
    digital_seen = False
    for row in out:
        if row.get("enabled_air"):
            if air_seen:
                row["enabled_air"] = False
            else:
                air_seen = True
        if row.get("enabled_ground"):
            if ground_seen:
                row["enabled_ground"] = False
            else:
                ground_seen = True
        if row.get("enabled_digital"):
            if digital_seen:
                row["enabled_digital"] = False
            else:
                digital_seen = True
        row["enabled"] = bool(
            row.get("enabled_air")
            or row.get("enabled_ground")
            or row.get("enabled_digital")
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
            "system_id": 0,
            "system_key": "",
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
            system_id = 0
            try:
                system_id = int(str(item.get("system_id") or "0").strip())
            except Exception:
                system_id = 0
            if system_id > 0:
                entry["system_id"] = int(system_id)
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
            entry["system_key"] = str(item.get("system_key") or "").strip()
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
    strict_location: bool = False
    zip: str = ""
    lat: float = 0.0
    lon: float = 0.0
    range_miles: float = 15.0
    nationwide_systems: bool = False
    enabled_service_tags: list[int] = field(default_factory=list)
    favorites: list[dict] = field(default_factory=list)
    favorites_name: str = "My Favorites"
    custom_favorites: list[dict] = field(default_factory=list)
    avoid_list: list[dict] = field(default_factory=list)
    travel_mode_enabled: bool = False
    service_tag_schema_version: int = _SERVICE_TAG_SCHEMA_VERSION

    @classmethod
    def default(cls, db_path: str = _DEFAULT_DB_PATH) -> "HPState":
        try:
            defaults = list(get_default_enabled_service_types(db_path=db_path))
        except Exception:
            logger.debug("hp_state: failed to load default enabled service types from %s", db_path, exc_info=True)
            defaults = [2, 3, 4]
        return cls(
            mode="full_database",
            use_location=False,
            strict_location=False,
            zip="",
            lat=0.0,
            lon=0.0,
            range_miles=15.0,
            nationwide_systems=False,
            enabled_service_tags=defaults,
            favorites=[],
            favorites_name="My Favorites",
            custom_favorites=[],
            avoid_list=[],
            travel_mode_enabled=False,
            service_tag_schema_version=_SERVICE_TAG_SCHEMA_VERSION,
        )

    def to_dict(self) -> dict:
        return {
            "mode": str(self.mode or "full_database"),
            "use_location": bool(self.use_location),
            "strict_location": bool(self.strict_location),
            "zip": str(self.zip or "").strip(),
            "lat": float(self.lat),
            "lon": float(self.lon),
            "range_miles": float(self.range_miles),
            "nationwide_systems": bool(self.nationwide_systems),
            "enabled_service_tags": [int(tag) for tag in (self.enabled_service_tags or [])],
            "favorites": _coerce_favorites(self.favorites),
            "favorites_name": str(self.favorites_name or "My Favorites").strip() or "My Favorites",
            "custom_favorites": _coerce_custom_favorites(self.custom_favorites),
            "avoid_list": _coerce_avoid_list(self.avoid_list),
            "travel_mode_enabled": bool(self.travel_mode_enabled),
            "service_tag_schema_version": int(self.service_tag_schema_version or _SERVICE_TAG_SCHEMA_VERSION),
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
            logger.debug("hp_state: failed to load state from %s; falling back to defaults", in_path, exc_info=True)
            return cls.default(db_path=db_path)

        if not isinstance(payload, dict):
            return cls.default(db_path=db_path)

        default_state = cls.default(db_path=db_path)
        mode = str(payload.get("mode") or default_state.mode).strip().lower()
        if mode not in {"full_database", "favorites"}:
            mode = default_state.mode

        legacy_version = 1
        try:
            legacy_version = int(payload.get("service_tag_schema_version") or 1)
        except Exception:
            logger.debug("hp_state: invalid service_tag_schema_version in %s", in_path, exc_info=True)
            legacy_version = 1

        service_tags = _coerce_service_tags(payload.get("enabled_service_tags"))
        service_tags = _migrate_legacy_service_tags(service_tags, legacy_version)
        if not service_tags:
            service_tags = list(default_state.enabled_service_tags)

        return cls(
            mode=mode,
            use_location=_coerce_bool(payload.get("use_location"), default=default_state.use_location),
            strict_location=_coerce_bool(
                payload.get("strict_location"),
                default=default_state.strict_location,
            ),
            zip=str(payload.get("zip") or payload.get("postal_code") or "").strip(),
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
            travel_mode_enabled=_coerce_bool(
                payload.get("travel_mode_enabled"),
                default=default_state.travel_mode_enabled,
            ),
            service_tag_schema_version=_SERVICE_TAG_SCHEMA_VERSION,
        )
