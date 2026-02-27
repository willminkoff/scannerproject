"""Manual Expert-mode scan pool builder."""
from __future__ import annotations


class ExpertPoolBuilder:
    @staticmethod
    def _parse_int(value) -> int | None:
        try:
            return int(str(value).strip())
        except Exception:
            return None

    @staticmethod
    def _parse_float(value) -> float | None:
        try:
            out = float(str(value).strip())
        except Exception:
            return None
        if out <= 0:
            return None
        return out

    @classmethod
    def _normalize_controls(cls, values) -> list[float]:
        raw = values if isinstance(values, list) else [values]
        seen: set[float] = set()
        out: list[float] = []
        for item in raw:
            parsed = cls._parse_float(item)
            if parsed is None:
                continue
            value = round(parsed, 6)
            if value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

    @classmethod
    def _normalize_talkgroups(cls, values) -> list[int]:
        raw = values if isinstance(values, list) else [values]
        seen: set[int] = set()
        out: list[int] = []
        for item in raw:
            parsed = cls._parse_int(item)
            if parsed is None or parsed <= 0:
                continue
            if parsed in seen:
                continue
            seen.add(parsed)
            out.append(parsed)
        return out

    @classmethod
    def build_pool(cls, config: dict) -> dict:
        payload = config if isinstance(config, dict) else {}
        manual_trunked = payload.get("manual_trunked")
        manual_conventional = payload.get("manual_conventional")

        trunked_sites: list[dict] = []
        for idx, item in enumerate(manual_trunked if isinstance(manual_trunked, list) else []):
            row = item if isinstance(item, dict) else {}
            system_id = cls._parse_int(row.get("system_id"))
            if system_id is None or system_id <= 0:
                system_id = idx + 1
            site_id = cls._parse_int(row.get("site_id"))
            if site_id is None or site_id <= 0:
                site_id = idx + 1

            control_channels = cls._normalize_controls(row.get("control_channels") or [])
            talkgroups = cls._normalize_talkgroups(row.get("talkgroups") or [])
            if not control_channels and not talkgroups:
                continue

            trunked_sites.append(
                {
                    "system_id": int(system_id),
                    "site_id": int(site_id),
                    "control_channels": control_channels,
                    "talkgroups": talkgroups,
                }
            )

        conventional: list[dict] = []
        for item in manual_conventional if isinstance(manual_conventional, list) else []:
            row = item if isinstance(item, dict) else {}
            freq = cls._parse_float(row.get("frequency"))
            if freq is None:
                continue
            alpha = str(row.get("alpha_tag") or "").strip()
            service_tag = cls._parse_int(row.get("service_tag"))
            if service_tag is None:
                service_tag = 0
            conventional.append(
                {
                    "frequency": round(freq, 6),
                    "alpha_tag": alpha,
                    "service_tag": int(service_tag),
                }
            )

        trunked_sites.sort(key=lambda row: (row["system_id"], row["site_id"]))
        conventional.sort(key=lambda row: (row["frequency"], row["service_tag"], row["alpha_tag"].lower()))
        return {
            "trunked_sites": trunked_sites,
            "conventional": conventional,
        }
