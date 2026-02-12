"""HTTP request handlers."""
import json
import os
import sys
import time
from datetime import datetime
import queue
import shutil
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
def combined_num_devices(conf_path=None) -> int:
    """Count devices declared in the combined rtl_airband config.

    More stable than probing USB at runtime (devices may be busy/in-use).
    """
    try:
        if not conf_path:
            conf_path = COMBINED_CONFIG_PATH
        with open(conf_path, "r") as f:
            txt = f.read()
        return txt.count('serial = "')
    except Exception:
        return 0

try:
    from .config import (
        CONFIG_SYMLINK,
        GROUND_CONFIG_PATH,
        PROFILES_DIR,
        UI_PORT,
        UNITS,
        COMBINED_CONFIG_PATH,
        DIGITAL_MIXER_ENABLED,
        DIGITAL_MIXER_AIRBAND_MOUNT,
        DIGITAL_MIXER_DIGITAL_MOUNT,
        DIGITAL_MIXER_OUTPUT_MOUNT,
        AIRBAND_RTL_SERIAL,
        GROUND_RTL_SERIAL,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_HINT,
        ICECAST_PORT,
        PLAYER_MOUNT,
    )
    from .profile_config import (
        read_active_config_path, parse_controls, split_profiles,
        guess_current_profile, summarize_avoids, parse_filter,
        load_profiles_registry, find_profile, validate_profile_id, safe_profile_path,
        enforce_profile_index, set_profile, save_profiles_registry, write_airband_flag,
        parse_freqs_labels, parse_freqs_text, write_freqs_labels, write_combined_config
    )
    from .combined_status import combined_device_summary, combined_config_stale
    from .scanner import (
        read_last_hit_airband, read_last_hit_ground, read_hit_list_cached
    )
    from .icecast import (
        icecast_up,
        fetch_local_icecast_status,
        list_icecast_mounts,
    )
    from .systemd import unit_active, unit_exists, restart_rtl, unit_active_enter_epoch
    from .server_workers import enqueue_action, enqueue_apply
    from .diagnostic import write_diagnostic_log
    from .spectrum import get_spectrum_bins, spectrum_to_json, start_spectrum
    from .system_stats import get_system_stats
    from .vlc import start_vlc, stop_vlc, vlc_running
    from .digital import (
        get_digital_manager,
        validate_digital_profile_id,
        create_digital_profile_dir,
        delete_digital_profile_dir,
        inspect_digital_profile,
        read_digital_talkgroups,
        write_digital_listen,
    )
except ImportError:
    from ui.config import (
        CONFIG_SYMLINK,
        GROUND_CONFIG_PATH,
        PROFILES_DIR,
        UI_PORT,
        UNITS,
        COMBINED_CONFIG_PATH,
        DIGITAL_MIXER_ENABLED,
        DIGITAL_MIXER_AIRBAND_MOUNT,
        DIGITAL_MIXER_DIGITAL_MOUNT,
        DIGITAL_MIXER_OUTPUT_MOUNT,
        AIRBAND_RTL_SERIAL,
        GROUND_RTL_SERIAL,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_HINT,
        ICECAST_PORT,
        PLAYER_MOUNT,
    )
    from ui.profile_config import (
        read_active_config_path, parse_controls, split_profiles,
        guess_current_profile, summarize_avoids, parse_filter,
        load_profiles_registry, find_profile, validate_profile_id, safe_profile_path,
        enforce_profile_index, set_profile, save_profiles_registry, write_airband_flag,
        parse_freqs_labels, parse_freqs_text, write_freqs_labels, write_combined_config
    )
    from ui.combined_status import combined_device_summary, combined_config_stale
    from ui.scanner import (
        read_last_hit_airband, read_last_hit_ground, read_hit_list_cached
    )
    from ui.icecast import (
        icecast_up,
        fetch_local_icecast_status,
        list_icecast_mounts,
    )
    from ui.systemd import unit_active, unit_exists, restart_rtl, unit_active_enter_epoch
    from ui.server_workers import enqueue_action, enqueue_apply
    from ui.diagnostic import write_diagnostic_log
    from ui.spectrum import get_spectrum_bins, spectrum_to_json, start_spectrum
    from ui.system_stats import get_system_stats
    from ui.vlc import start_vlc, stop_vlc, vlc_running
    from ui.digital import (
        get_digital_manager,
        validate_digital_profile_id,
        create_digital_profile_dir,
        delete_digital_profile_dir,
        inspect_digital_profile,
        read_digital_talkgroups,
        write_digital_listen,
    )


def _read_html_template():
    """Read the static HTML template."""
    ui_dir = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(ui_dir, "static", "index.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<!doctype html><html><body>Static files not found</body></html>"


HTML_TEMPLATE = _read_html_template()
# Digital call-event logs can emit rapid "grant/continue" updates for the same talkgroup.
# Use a wider default coalesce window to align UI hits with perceived audible traffic.
DIGITAL_HIT_COALESCE_SEC = max(0.0, float(os.getenv("DIGITAL_HIT_COALESCE_SEC", "20")))


def _coalesce_digital_hits(items: list[dict], window_sec: float = DIGITAL_HIT_COALESCE_SEC) -> list[dict]:
    """Collapse repeated digital updates for the same talkgroup/label within a short window."""
    if not items or window_sec <= 0:
        return items
    kept = []
    last_by_key: dict[str, float] = {}
    for item in sorted(items, key=lambda row: float(row.get("_ts", 0.0))):
        ts = float(item.get("_ts", 0.0))
        tgid = str(item.get("tgid") or "").strip()
        label = str(item.get("label") or item.get("freq") or "").strip().lower()
        if tgid:
            key = f"tgid:{tgid}"
        elif label:
            key = f"label:{label}"
        else:
            continue
        prev = last_by_key.get(key)
        if prev is not None and (ts - prev) < window_sec:
            continue
        last_by_key[key] = ts
        kept.append(item)
    return kept


class Handler(BaseHTTPRequestHandler):
    """HTTP request handler for the UI."""

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        """Send an HTTP response."""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.wfile.write(body)

    def do_GET(self):
        """Handle GET requests."""
        u = urlparse(self.path)
        p = u.path
        if p == "/":
            return self._send(200, HTML_TEMPLATE)
        
        # Serve SB3 UI
        if p == "/sb3" or p == "/sb3.html":
            ui_dir = os.path.dirname(os.path.abspath(__file__))
            mockup_path = os.path.join(ui_dir, "sb3.html")
            try:
                with open(mockup_path, "r", encoding="utf-8") as f:
                    return self._send(200, f.read())
            except FileNotFoundError:
                return self._send(404, "SB3 UI not found", "text/plain; charset=utf-8")
        
        # Serve static files
        if p.startswith("/static/"):
            ui_dir = os.path.dirname(os.path.abspath(__file__))
            static_dir = os.path.realpath(os.path.join(ui_dir, "static"))
            file_path = os.path.realpath(os.path.join(ui_dir, p.lstrip("/")))
            if not (file_path == static_dir or file_path.startswith(static_dir + os.sep)):
                return self._send(403, "Forbidden", "text/plain; charset=utf-8")
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
                # Determine content type
                if file_path.endswith(".css"):
                    ctype = "text/css; charset=utf-8"
                elif file_path.endswith(".js"):
                    ctype = "application/javascript; charset=utf-8"
                else:
                    ctype = "application/octet-stream"
                return self._send(200, content, ctype)
            except FileNotFoundError:
                return self._send(404, "Not found", "text/plain; charset=utf-8")
        
        if p == "/api/profile":
            q = parse_qs(u.query or "")
            profile_id = (q.get("id") or [""])[0].strip()
            if not profile_id:
                return self._send(400, json.dumps({"ok": False, "error": "missing id"}), "application/json; charset=utf-8")
            profiles = load_profiles_registry()
            prof = find_profile(profiles, profile_id)
            if not prof:
                return self._send(404, json.dumps({"ok": False, "error": "profile not found"}), "application/json; charset=utf-8")
            path = prof.get("path", "")
            exists = bool(path) and os.path.exists(path)
            freqs = []
            labels = []
            if exists:
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                    fvals, labs = parse_freqs_labels(text)
                    freqs = [f"{v:.4f}" for v in (fvals or [])]
                    labels = labs or []
                except Exception as e:
                    return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            payload = {
                "ok": True,
                "profile": {
                    "id": prof.get("id", ""),
                    "label": prof.get("label", ""),
                    "path": path,
                    "airband": bool(prof.get("airband")),
                    "exists": exists,
                },
                "freqs": freqs,
                "labels": labels,
            }
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/system":
            try:
                payload = get_system_stats()
            except Exception as e:
                payload = {"ok": False, "error": str(e)}
                return self._send(500, json.dumps(payload), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/status":
            conf_path = read_active_config_path()
            ground_conf_path = os.path.realpath(GROUND_CONFIG_PATH)
            combined_conf_path = COMBINED_CONFIG_PATH
            airband_gain, airband_snr, airband_dbfs, airband_mode = parse_controls(conf_path)
            ground_gain, ground_snr, ground_dbfs, ground_mode = parse_controls(GROUND_CONFIG_PATH)
            airband_filter = parse_filter("airband")
            ground_filter = parse_filter("ground")
            rtl_ok = unit_active(UNITS["rtl"])
            rtl_unit_active = unit_active(UNITS["rtl"])
            ground_unit_active = unit_active(UNITS["ground"])
            combined_info = combined_device_summary()
            airband_device = combined_info.get("airband")
            ground_device = combined_info.get("ground")
            expected_serials = dict(combined_info.get("expected_serials") or {})
            if AIRBAND_RTL_SERIAL:
                expected_serials["airband"] = AIRBAND_RTL_SERIAL
            if GROUND_RTL_SERIAL:
                expected_serials["ground"] = GROUND_RTL_SERIAL
            expected_serials["digital"] = DIGITAL_RTL_SERIAL or ""
            serial_mismatch_detail = []
            if AIRBAND_RTL_SERIAL:
                actual = airband_device.get("serial") if airband_device else ""
                if not actual:
                    serial_mismatch_detail.append({
                        "device": "airband",
                        "expected": AIRBAND_RTL_SERIAL,
                        "actual": "",
                        "reason": "airband device not found in combined config",
                    })
                elif actual != AIRBAND_RTL_SERIAL:
                    serial_mismatch_detail.append({
                        "device": "airband",
                        "expected": AIRBAND_RTL_SERIAL,
                        "actual": actual,
                        "reason": "airband serial mismatch",
                    })
            if GROUND_RTL_SERIAL:
                actual = ground_device.get("serial") if ground_device else ""
                if not actual:
                    serial_mismatch_detail.append({
                        "device": "ground",
                        "expected": GROUND_RTL_SERIAL,
                        "actual": "",
                        "reason": "ground device not found in combined config",
                    })
                elif actual != GROUND_RTL_SERIAL:
                    serial_mismatch_detail.append({
                        "device": "ground",
                        "expected": GROUND_RTL_SERIAL,
                        "actual": actual,
                        "reason": "ground serial mismatch",
                    })
            airband_present = airband_device is not None
            ground_present = ground_device is not None
            rtl_ok = rtl_unit_active
            ground_ok = rtl_ok and ground_present
            ice_ok = icecast_up()
            icecast_mounts = []
            if ice_ok:
                try:
                    status_text = fetch_local_icecast_status()
                    icecast_mounts = list_icecast_mounts(status_text)
                except Exception:
                    icecast_mounts = []
            combined_stale = combined_config_stale()

            prof_payload, profiles_airband, profiles_ground = split_profiles()
            missing = [p["path"] for p in prof_payload if not p.get("exists")]
            profile_airband = guess_current_profile(conf_path, [(p["id"], p["label"], p["path"]) for p in profiles_airband])
            profile_ground = guess_current_profile(ground_conf_path, [(p["id"], p["label"], p["path"]) for p in profiles_ground])
            last_hit_airband = read_last_hit_airband()
            last_hit_ground = read_last_hit_ground()
            hit_items = read_hit_list_cached(limit=20)
            latest_hit = hit_items[0].get("freq") if hit_items else ""
            config_mtimes = {}
            for key, path in (("airband", conf_path), ("ground", ground_conf_path), ("combined", combined_conf_path)):
                try:
                    config_mtimes[key] = os.path.getmtime(path)
                except Exception:
                    config_mtimes[key] = None
            rtl_active_enter = unit_active_enter_epoch(UNITS["rtl"])
            rtl_restart_required = False
            if rtl_active_enter and config_mtimes.get("combined"):
                rtl_restart_required = config_mtimes["combined"] > rtl_active_enter

            payload = {
                "rtl_active": rtl_ok,
                "ground_active": ground_ok,
                "ground_exists": ground_present,
                "rtl_unit_active": rtl_unit_active,
                "ground_unit_active": ground_unit_active,
                "combined_config_stale": combined_stale,
                "combined_devices": len(combined_info.get("devices") or []),
                "combined_devices_detail": combined_info.get("devices") or [],
                "airband_present": airband_present,
                "ground_present": ground_present,
                "icecast_active": ice_ok,
                "icecast_mounts": icecast_mounts,
                "icecast_port": ICECAST_PORT,
                "stream_mount": PLAYER_MOUNT,
                "icecast_expected_mounts": (
                    [f"/{DIGITAL_MIXER_AIRBAND_MOUNT}", f"/{DIGITAL_MIXER_DIGITAL_MOUNT}", f"/{DIGITAL_MIXER_OUTPUT_MOUNT}"]
                    if DIGITAL_MIXER_ENABLED else [f"/{DIGITAL_MIXER_OUTPUT_MOUNT}"]
                ),
                "expected_serials": expected_serials,
                "serial_mismatch": bool(serial_mismatch_detail),
                "serial_mismatch_detail": serial_mismatch_detail,
                "keepalive_active": unit_active(UNITS["keepalive"]),
                "server_time": time.time(),
                "rtl_active_enter": rtl_active_enter,
                "rtl_restart_required": rtl_restart_required,
                "config_paths": {
                    "airband": conf_path,
                    "ground": ground_conf_path,
                    "combined": combined_conf_path,
                },
                "config_mtimes": config_mtimes,
                "profile_airband": profile_airband,
                "profile_ground": profile_ground,
                "profiles_airband": profiles_airband,
                "profiles_ground": profiles_ground,
                "missing_profiles": missing,
                "gain": float(airband_gain),
                "squelch": float(airband_snr),
                "airband_gain": float(airband_gain),
                "airband_squelch": float(airband_snr),
                "airband_squelch_mode": airband_mode,
                "airband_squelch_snr": float(airband_snr),
                "airband_squelch_dbfs": float(airband_dbfs),
                "airband_filter": float(airband_filter),
                "ground_gain": float(ground_gain),
                "ground_squelch": float(ground_snr),
                "ground_squelch_mode": ground_mode,
                "ground_squelch_snr": float(ground_snr),
                "ground_squelch_dbfs": float(ground_dbfs),
                "ground_filter": float(ground_filter),
                "airband_applied_gain": airband_device.get("gain") if airband_device else None,
                "airband_applied_squelch_dbfs": airband_device.get("squelch_dbfs") if airband_device else None,
                "ground_applied_gain": ground_device.get("gain") if ground_device else None,
                "ground_applied_squelch_dbfs": ground_device.get("squelch_dbfs") if ground_device else None,
                "last_hit": latest_hit or last_hit_airband or last_hit_ground or "",
                "last_hit_airband": last_hit_airband,
                "last_hit_ground": last_hit_ground,
                "avoids_airband": summarize_avoids(conf_path, "airband"),
                "avoids_ground": summarize_avoids(os.path.realpath(GROUND_CONFIG_PATH), "ground"),
            }
            digital_payload = {
                "digital_active": False,
                "digital_backend": "",
                "digital_profile": "",
                "digital_muted": False,
                "digital_last_label": "",
                "digital_last_time": 0,
                "digital_last_warning": "",
            }
            try:
                digital_payload = get_digital_manager().status_payload()
            except Exception as e:
                digital_payload["digital_last_error"] = str(e)
            digital_payload["digital_mixer_active"] = unit_active(UNITS["digital_mixer"])
            payload.update(digital_payload)
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        if p == "/api/profiles":
            profiles = load_profiles_registry()
            prof_payload, profiles_airband, profiles_ground = split_profiles()
            airband_conf = read_active_config_path()
            ground_conf = os.path.realpath(GROUND_CONFIG_PATH)
            active_airband_id = ""
            active_ground_id = ""
            for pitem in profiles:
                path = pitem.get("path")
                if path and os.path.realpath(path) == os.path.realpath(airband_conf):
                    active_airband_id = pitem.get("id", "")
                if path and os.path.realpath(path) == os.path.realpath(ground_conf):
                    active_ground_id = pitem.get("id", "")
            payload = {
                "ok": True,
                "profiles": prof_payload,
                "profiles_airband": profiles_airband,
                "profiles_ground": profiles_ground,
                "active_airband_id": active_airband_id,
                "active_ground_id": active_ground_id,
            }
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        if p == "/api/digital/profiles":
            try:
                manager = get_digital_manager()
                payload = {
                    "ok": True,
                    "profiles": manager.listProfiles(),
                    "active": manager.getProfile(),
                }
            except Exception as e:
                payload = {"ok": False, "error": str(e)}
                return self._send(500, json.dumps(payload), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        if p == "/api/digital/preflight":
            try:
                manager = get_digital_manager()
                preflight = manager.preflight() or {}
            except Exception as e:
                preflight = {"tuner_busy": False, "tuner_busy_lines": [], "error": str(e)}
            combined_info = combined_device_summary()
            airband_serial = AIRBAND_RTL_SERIAL or (combined_info.get("airband") or {}).get("serial")
            ground_serial = GROUND_RTL_SERIAL or (combined_info.get("ground") or {}).get("serial")
            digital_serial_configured = bool(DIGITAL_RTL_SERIAL)
            payload = {
                "ok": True,
                "expected_serials": {
                    "airband": airband_serial,
                    "ground": ground_serial,
                    "digital": DIGITAL_RTL_SERIAL or "",
                },
                "digital_serial_configured": digital_serial_configured,
                "tuner_busy": bool(preflight.get("tuner_busy")),
                "tuner_busy_lines": preflight.get("tuner_busy_lines") or [],
                "tuner_busy_count": int(preflight.get("tuner_busy_count") or 0),
                "tuner_busy_last_time_ms": int(preflight.get("tuner_busy_last_time_ms") or 0),
                "rtl_devices": [],
                "rtl_devices_note": "not implemented",
                "device_holders": {"ok": False, "error": "not implemented"},
            }
            if not digital_serial_configured:
                payload["digital_serial_hint"] = DIGITAL_RTL_SERIAL_HINT
                payload["digital_serial_help"] = "Set DIGITAL_RTL_SERIAL in your EnvironmentFile and restart airband-ui."
            if preflight.get("error"):
                payload["error"] = preflight.get("error")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        if p == "/api/digital/talkgroups":
            q = parse_qs(u.query or "")
            profile_id = (q.get("profileId") or [""])[0].strip()
            if not profile_id:
                return self._send(400, json.dumps({"ok": False, "error": "missing profileId"}), "application/json; charset=utf-8")
            ok, payload = read_digital_talkgroups(profile_id)
            if not ok:
                return self._send(400, json.dumps({"ok": False, "error": payload}), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        if p == "/api/hits":
            items = read_hit_list_cached(limit=50)
            def parse_time_ts(value: str) -> float:
                if not value:
                    return 0.0
                try:
                    dt = datetime.strptime(value, "%H:%M:%S")
                    now = datetime.now()
                    dt = dt.replace(year=now.year, month=now.month, day=now.day)
                    return dt.timestamp()
                except Exception:
                    return 0.0
            for item in items:
                item["_ts"] = parse_time_ts(item.get("time"))

            digital_items = []
            try:
                events = get_digital_manager().getRecentEvents(limit=50)
            except Exception:
                events = []
            for event in events:
                label = str(event.get("label") or "").strip()
                if not label:
                    continue
                time_ms = int(event.get("timeMs") or 0)
                ts = time_ms / 1000.0 if time_ms else time.time()
                time_str = time.strftime("%H:%M:%S", time.localtime(ts))
                entry = {
                    "time": time_str,
                    "freq": label,
                    "duration": 0,
                    "label": label,
                    "mode": event.get("mode"),
                    "tgid": event.get("tgid"),
                    "type": "digital",
                    "source": "digital",
                    "_ts": ts,
                }
                digital_items.append(entry)
            digital_items = _coalesce_digital_hits(digital_items)

            merged = items + digital_items
            merged.sort(key=lambda item: item.get("_ts", 0.0))
            merged = merged[-50:]
            merged.reverse()
            for item in merged:
                item.pop("_ts", None)
            payload = {"items": merged}
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        
        if p == "/api/spectrum":
            # One-shot spectrum data
            band = parse_qs(urlparse(self.path).query).get("band", ["airband"])[0]
            return self._send(200, spectrum_to_json(band), "application/json; charset=utf-8")
        
        if p == "/api/stream":
            # Server-Sent Events stream for real-time updates
            return self._handle_sse_stream()
        
        return self._send(404, "Not found", "text/plain; charset=utf-8")

    def _handle_sse_stream(self):
        """Handle SSE stream for real-time data."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        import re
        try:
            while True:
                conf_path = read_active_config_path()
                airband_gain, airband_snr, airband_dbfs, airband_mode = parse_controls(conf_path)
                rtl_unit_active = unit_active(UNITS["rtl"])
                ground_unit_active = unit_active(UNITS["ground"])
                combined_info = combined_device_summary()
                ground_present = combined_info.get("ground") is not None
                rtl_active = rtl_unit_active
                ground_active = rtl_active and ground_present
                ice_ok = icecast_up()
                hit_items = read_hit_list_cached(limit=20)
                last_hit = hit_items[0].get("freq") if hit_items else (read_last_hit_airband() or read_last_hit_ground())
                status_data = {
                    "type": "status",
                    "rtl_active": rtl_active,
                    "ground_active": ground_active,
                    "icecast_active": ice_ok,
                    "ground_unit_active": ground_unit_active,
                    "combined_config_stale": combined_config_stale(),
                    "gain": float(airband_gain),
                    "squelch": float(airband_snr),
                    "squelch_mode": airband_mode,
                    "squelch_snr": float(airband_snr),
                    "squelch_dbfs": float(airband_dbfs),
                    "last_hit": last_hit,
                    "server_time": time.time(),
                }
                self.wfile.write(f"event: status\ndata: {json.dumps(status_data)}\n\n".encode())
                spectrum_data = {
                    "type": "spectrum",
                    "bins": [],
                    "timestamp": time.time(),
                    "note": "stats_filepath not supported in rtl_airband v5.1.1"
                }
                self.wfile.write(f"event: spectrum\ndata: {json.dumps(spectrum_data)}\n\n".encode())
                hits = read_hit_list_cached(limit=10)
                hits_data = {
                    "type": "hits",
                    "items": hits,
                }
                self.wfile.write(f"event: hits\ndata: {json.dumps(hits_data)}\n\n".encode())
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        """Handle POST requests."""

        p = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="ignore")
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            try:
                data = json.loads(raw) if raw.strip() else {}
                form = data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                form = {}
        else:
            form = {k: v[0] for k, v in parse_qs(raw).items()}

        def get_str(key: str, default: str = "") -> str:
            v = form.get(key, default)
            if v is None:
                return default
            return str(v)

        if p == "/api/digital/start":
            ok, err = get_digital_manager().start()
            payload = {"ok": bool(ok)}
            if not ok:
                payload["error"] = err or "start failed"
                return self._send(500, json.dumps(payload), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/digital/stop":
            ok, err = get_digital_manager().stop()
            payload = {"ok": bool(ok)}
            if not ok:
                payload["error"] = err or "stop failed"
                return self._send(500, json.dumps(payload), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/digital/restart":
            ok, err = get_digital_manager().restart()
            payload = {"ok": bool(ok)}
            if not ok:
                payload["error"] = err or "restart failed"
                return self._send(500, json.dumps(payload), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/digital/profile":
            profile_id = get_str("profileId").strip()
            if not profile_id:
                return self._send(400, json.dumps({"ok": False, "error": "missing profileId"}), "application/json; charset=utf-8")
            if not validate_digital_profile_id(profile_id):
                return self._send(400, json.dumps({"ok": False, "error": "invalid profileId"}), "application/json; charset=utf-8")
            ok, err = get_digital_manager().setProfile(profile_id)
            payload = {"ok": bool(ok)}
            if not ok:
                payload["error"] = err or "set profile failed"
                status = 400 if err in ("invalid profileId", "unknown profileId") else 500
                return self._send(status, json.dumps(payload), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/digital/mute":
            raw_muted = form.get("muted")
            if raw_muted is None:
                return self._send(400, json.dumps({"ok": False, "error": "missing muted"}), "application/json; charset=utf-8")
            muted = None
            if isinstance(raw_muted, bool):
                muted = raw_muted
            elif isinstance(raw_muted, (int, float)):
                muted = bool(raw_muted)
            else:
                sval = str(raw_muted).strip().lower()
                if sval in ("1", "true", "yes", "on"):
                    muted = True
                elif sval in ("0", "false", "no", "off"):
                    muted = False
            if muted is None:
                return self._send(400, json.dumps({"ok": False, "error": "invalid muted"}), "application/json; charset=utf-8")
            get_digital_manager().setMuted(muted)
            return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8")

        if p == "/api/digital/profile/create":
            profile_id = get_str("profileId").strip()
            ok, err = create_digital_profile_dir(profile_id)
            if not ok:
                status = 400 if err in ("invalid profileId", "profile already exists") else 500
                return self._send(status, json.dumps({"ok": False, "error": err}), "application/json; charset=utf-8")
            return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8")

        if p == "/api/digital/profile/delete":
            profile_id = get_str("profileId").strip()
            ok, err = delete_digital_profile_dir(profile_id)
            if not ok:
                status = 400 if err in ("invalid profileId", "profile is active", "profile not found", "profile path is a symlink") else 500
                return self._send(status, json.dumps({"ok": False, "error": err}), "application/json; charset=utf-8")
            return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8")

        if p == "/api/digital/profile/inspect":
            profile_id = get_str("profileId").strip()
            ok, payload = inspect_digital_profile(profile_id)
            if not ok:
                status = 400 if payload in ("invalid profileId", "profile not found") else 500
                return self._send(status, json.dumps({"ok": False, "error": payload}), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/digital/talkgroups/listen":
            profile_id = get_str("profileId").strip()
            if not profile_id:
                return self._send(400, json.dumps({"ok": False, "error": "missing profileId"}), "application/json; charset=utf-8")
            items = form.get("items")
            if isinstance(items, str):
                try:
                    items = json.loads(items)
                except json.JSONDecodeError:
                    items = []
            if not isinstance(items, list):
                items = []
            ok, err = write_digital_listen(profile_id, items)
            if not ok:
                return self._send(400, json.dumps({"ok": False, "error": err}), "application/json; charset=utf-8")
            return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8")

        if p == "/api/profile/create":
            profile_id = get_str("id").strip()
            label = get_str("label").strip()
            airband_raw = form.get("airband", True)
            if isinstance(airband_raw, bool):
                airband_flag = airband_raw
            else:
                airband_flag = str(airband_raw).lower() in ("1", "true", "yes", "on")
            clone_from = get_str("clone_from_id").strip()
            if not validate_profile_id(profile_id):
                return self._send(400, json.dumps({"ok": False, "error": "invalid id"}), "application/json; charset=utf-8")
            # Allow minimalist create: if label omitted, default to id.
            if not label:
                label = profile_id
            profiles = load_profiles_registry()
            if find_profile(profiles, profile_id):
                return self._send(400, json.dumps({"ok": False, "error": "id already exists"}), "application/json; charset=utf-8")
            new_path = os.path.join(PROFILES_DIR, f"rtl_airband_{profile_id}.conf")
            safe_path = safe_profile_path(new_path)
            if not safe_path:
                return self._send(400, json.dumps({"ok": False, "error": "invalid path"}), "application/json; charset=utf-8")
            if os.path.exists(safe_path):
                return self._send(400, json.dumps({"ok": False, "error": "profile file exists"}), "application/json; charset=utf-8")
            try:
                src = find_profile(profiles, clone_from) if clone_from else None
                if src:
                    shutil.copyfile(src["path"], safe_path)
                else:
                    # Minimal blank template with freqs block so the textarea editor can save immediately.
                    desired_index = 0 if bool(airband_flag) else 1
                    # Avoid creating an empty freqs list: rtl_airband refuses to start if freqs is empty.
                    default_freq = "118.6000" if bool(airband_flag) else "462.6500"
                    default_mod = "am" if bool(airband_flag) else "nfm"
                    default_bw = "12000" if bool(airband_flag) else "12000"
                    template = f"""airband = {'true' if bool(airband_flag) else 'false'};\n\n""" + \
                        "devices:\n" + \
                        "({\n" + \
                        "  type = \"rtlsdr\";\n" + \
                        f"  index = {desired_index};\n" + \
                        "  mode = \"scan\";\n" + \
                        "  gain = 32.800;   # UI_CONTROLLED\n\n" + \
                        "  channels:\n" + \
                        "  (\n" + \
                        "    {\n" + \
                        f"      freqs = ({default_freq});\n\n" + \
                        f"      modulation = \"{default_mod}\";\n" + \
                        f"      bandwidth = {default_bw};\n" + \
                        "      squelch_threshold = -70;  # UI_CONTROLLED\n" + \
                        "      squelch_delay = 0.8;\n\n" + \
                        "      outputs:\n" + \
                        "      (\n" + \
                        "        {\n" + \
                        "          type = \"icecast\";\n" + \
                        "          send_scan_freq_tags = true;\n" + \
                        "          server = \"127.0.0.1\";\n" + \
                        "          port = 8000;\n" + \
                        "          mountpoint = \"scannerbox.mp3\";\n" + \
                        "          username = \"source\";\n" + \
                        "          password = \"062352\";\n" + \
                        "          name = \"SprontPi Radio\";\n" + \
                        "          genre = \"AIRBAND\";\n" + \
                        "          description = \"Custom\";\n" + \
                        "          bitrate = 16;\n" + \
                        "        }\n" + \
                        "      );\n" + \
                        "    }\n" + \
                        "  );\n" + \
                        "});\n"
                    with open(safe_path, "w", encoding="utf-8") as f:
                        f.write(template)

                write_airband_flag(safe_path, bool(airband_flag))
                enforce_profile_index(safe_path)
                freqs_text = get_str("freqs_text").strip()
                if freqs_text:
                    freqs, labels = parse_freqs_text(freqs_text)
                    write_freqs_labels(safe_path, freqs, labels)
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            profiles.append({
                "id": profile_id,
                "label": label,
                "path": safe_path,
                "airband": bool(airband_flag),
            })
            save_profiles_registry(profiles)
            return self._send(200, json.dumps({"ok": True, "profile": profiles[-1]}), "application/json; charset=utf-8")

        if p == "/api/profile/update_freqs":
            profile_id = get_str("id").strip()
            if not profile_id:
                return self._send(400, json.dumps({"ok": False, "error": "missing id"}), "application/json; charset=utf-8")
            profiles = load_profiles_registry()
            prof = find_profile(profiles, profile_id)
            if not prof:
                return self._send(404, json.dumps({"ok": False, "error": "profile not found"}), "application/json; charset=utf-8")
            path = prof.get("path", "")
            safe_path = safe_profile_path(path) if path else None
            if not safe_path or not os.path.exists(safe_path):
                return self._send(404, json.dumps({"ok": False, "error": "profile file not found"}), "application/json; charset=utf-8")

            # Prefer freqs_text input (textarea format). Otherwise accept arrays.
            freqs_text = get_str("freqs_text").strip()
            labels_present = "labels" in form
            freqs_present = "freqs" in form
            try:
                if freqs_text:
                    freqs, labels = parse_freqs_text(freqs_text)
                else:
                    raw_freqs = form.get("freqs")
                    if isinstance(raw_freqs, list):
                        freqs = [float(x) for x in raw_freqs]
                    elif isinstance(raw_freqs, str) and raw_freqs.strip():
                        freqs = [float(x) for x in raw_freqs.split(",") if x.strip()]
                    else:
                        return self._send(400, json.dumps({"ok": False, "error": "missing freqs"}), "application/json; charset=utf-8")

                    if labels_present:
                        raw_labels = form.get("labels")
                        if not isinstance(raw_labels, list):
                            return self._send(400, json.dumps({"ok": False, "error": "labels must be an array"}), "application/json; charset=utf-8")
                        labels = [str(x) for x in raw_labels]
                        if len(labels) != len(freqs):
                            return self._send(400, json.dumps({"ok": False, "error": "labels must match freqs length"}), "application/json; charset=utf-8")
                    else:
                        labels = None
            except ValueError as e:
                return self._send(400, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")

            try:
                changed = write_freqs_labels(safe_path, freqs, labels)
                enforce_profile_index(safe_path)
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")

            # If editing the active profile, regenerate combined config and restart rtl only if needed.
            active_airband = os.path.realpath(read_active_config_path())
            active_ground = os.path.realpath(GROUND_CONFIG_PATH)
            is_active = os.path.realpath(safe_path) in (active_airband, active_ground)
            if is_active:
                try:
                    combined_changed = write_combined_config()
                    changed = changed or combined_changed
                    if combined_changed:
                        restart_rtl()
                except Exception as e:
                    return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")

            return self._send(200, json.dumps({"ok": True, "changed": bool(changed)}), "application/json; charset=utf-8")

        if p == "/api/profile/update":
            profile_id = get_str("id").strip()
            label = get_str("label").strip()
            if not profile_id or not label:
                return self._send(400, json.dumps({"ok": False, "error": "missing fields"}), "application/json; charset=utf-8")
            profiles = load_profiles_registry()
            prof = find_profile(profiles, profile_id)
            if not prof:
                return self._send(404, json.dumps({"ok": False, "error": "profile not found"}), "application/json; charset=utf-8")
            prof["label"] = label
            save_profiles_registry(profiles)
            return self._send(200, json.dumps({"ok": True, "profile": prof}), "application/json; charset=utf-8")

        if p == "/api/profile/delete":
            profile_id = get_str("id").strip()
            profiles = load_profiles_registry()
            prof = find_profile(profiles, profile_id)
            if not prof:
                return self._send(404, json.dumps({"ok": False, "error": "profile not found"}), "application/json; charset=utf-8")
            airband_conf = read_active_config_path()
            ground_conf = os.path.realpath(GROUND_CONFIG_PATH)
            if os.path.realpath(prof["path"]) in (os.path.realpath(airband_conf), os.path.realpath(ground_conf)):
                return self._send(400, json.dumps({"ok": False, "error": "profile is active"}), "application/json; charset=utf-8")
            safe_path = safe_profile_path(prof["path"])
            if safe_path and os.path.exists(safe_path):
                try:
                    os.remove(safe_path)
                except Exception as e:
                    return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            profiles = [p for p in profiles if p.get("id") != profile_id]
            save_profiles_registry(profiles)
            return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8")

        if p == "/api/profile":
            pid = form.get("profile", "")
            target = form.get("target", "airband")
            result = enqueue_action({"type": "profile", "profile": pid, "target": target})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/apply":
            target = form.get("target", "airband")
            if target not in ("airband", "ground"):
                return self._send(400, json.dumps({"ok": False, "error": "unknown target"}), "application/json; charset=utf-8")
            try:
                gain = float(form.get("gain", "32.8"))
                squelch_mode = (form.get("squelch_mode") or "dbfs").lower()
                squelch_snr = form.get("squelch_snr", form.get("squelch", "10.0"))
                squelch_dbfs = form.get("squelch_dbfs", form.get("squelch", "0"))
                squelch_snr = float(squelch_snr)
                squelch_dbfs = float(squelch_dbfs)
            except ValueError:
                return self._send(400, json.dumps({"ok": False, "error": "bad values"}), "application/json; charset=utf-8")
            result = enqueue_apply(target, gain, squelch_mode, squelch_snr, squelch_dbfs)
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/apply-batch":
            target = form.get("target", "airband")
            if target not in ("airband", "ground"):
                return self._send(400, json.dumps({"ok": False, "error": "unknown target"}), "application/json; charset=utf-8")
            try:
                gain = float(form.get("gain", "32.8"))
                squelch_mode = (form.get("squelch_mode") or "dbfs").lower()
                squelch_snr = form.get("squelch_snr", form.get("squelch", "10.0"))
                squelch_dbfs = form.get("squelch_dbfs", form.get("squelch", "0"))
                cutoff_hz = float(form.get("cutoff_hz", "3500"))
                squelch_snr = float(squelch_snr)
                squelch_dbfs = float(squelch_dbfs)
            except ValueError:
                return self._send(400, json.dumps({"ok": False, "error": "bad values"}), "application/json; charset=utf-8")
            result = enqueue_action({
                "type": "apply_batch",
                "target": target,
                "gain": gain,
                "squelch_mode": squelch_mode,
                "squelch_snr": squelch_snr,
                "squelch_dbfs": squelch_dbfs,
                "cutoff_hz": cutoff_hz,
            })
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/filter":
            target = form.get("target", "airband")
            if target not in ("airband", "ground"):
                return self._send(400, json.dumps({"ok": False, "error": "unknown target"}), "application/json; charset=utf-8")
            try:
                cutoff_hz = float(form.get("cutoff_hz", "3500"))
            except ValueError:
                return self._send(400, json.dumps({"ok": False, "error": "bad values"}), "application/json; charset=utf-8")
            result = enqueue_action({"type": "filter", "target": target, "cutoff_hz": cutoff_hz})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/restart":
            target = form.get("target", "airband")
            result = enqueue_action({"type": "restart", "target": target})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/avoid":
            target = form.get("target", "airband")
            result = enqueue_action({"type": "avoid", "target": target})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/avoid-clear":
            target = form.get("target", "airband")
            result = enqueue_action({"type": "avoid_clear", "target": target})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/vlc":
            action = get_str("action", "start").strip().lower()
            if action == "status":
                return self._send(200, json.dumps({"ok": True, "running": vlc_running()}), "application/json; charset=utf-8")
            if action == "start":
                ok, err = start_vlc()
                running = True if ok else vlc_running()
            elif action == "stop":
                ok, err = stop_vlc()
                running = False if ok else vlc_running()
            else:
                return self._send(400, json.dumps({"ok": False, "error": "unknown action"}), "application/json; charset=utf-8")
            payload = {"ok": ok, "running": running}
            if not ok:
                payload["error"] = err or "command failed"
                return self._send(500, json.dumps(payload), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/tune":
            target = form.get("target", "airband")
            freq = form.get("freq")
            result = enqueue_action({"type": "tune", "target": target, "freq": freq})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/tune-restore":
            target = form.get("target", "airband")
            result = enqueue_action({"type": "tune_restore", "target": target})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/hold":
            target = form.get("target", "airband")
            mode = form.get("action", "start")
            freq = form.get("freq")
            action = {"type": "hold", "target": target, "mode": mode}
            if mode != "stop":
                action["freq"] = freq
            result = enqueue_action(action)
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/diagnostic":
            try:
                path = write_diagnostic_log()
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            return self._send(200, json.dumps({"ok": True, "path": path}), "application/json; charset=utf-8")

        return self._send(404, json.dumps({"ok": False, "error": "not found"}), "application/json; charset=utf-8")

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass
