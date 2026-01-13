"""HTTP request handlers."""
import json
import os
import sys
import time
import queue
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

try:
    from .config import CONFIG_SYMLINK, GROUND_CONFIG_PATH, UI_PORT, UNITS
    from .profile_config import (
        read_active_config_path, parse_controls, split_profiles,
        guess_current_profile, summarize_avoids, parse_filter
    )
    from .scanner import (
        read_last_hit_airband, read_last_hit_ground, read_icecast_hit_list,
        read_hit_list_cached
    )
    from .icecast import icecast_up, read_last_hit_from_icecast
    from .systemd import unit_active, unit_exists
    from .server_workers import enqueue_action, enqueue_apply
    from .diagnostic import write_diagnostic_log
    from .spectrum import get_spectrum_bins, spectrum_to_json, start_spectrum, parse_stats_file
except ImportError:
    from ui.config import CONFIG_SYMLINK, GROUND_CONFIG_PATH, UI_PORT, UNITS
    from ui.profile_config import (
        read_active_config_path, parse_controls, split_profiles,
        guess_current_profile, summarize_avoids, parse_filter
    )
    from ui.scanner import (
        read_last_hit_airband, read_last_hit_ground, read_icecast_hit_list,
        read_hit_list_cached
    )
    from ui.icecast import icecast_up, read_last_hit_from_icecast
    from ui.systemd import unit_active, unit_exists
    from ui.server_workers import enqueue_action, enqueue_apply
    from ui.diagnostic import write_diagnostic_log
    from ui.spectrum import get_spectrum_bins, spectrum_to_json, start_spectrum, parse_stats_file


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
        p = urlparse(self.path).path
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
        
        if p == "/api/status":
            conf_path = read_active_config_path()
            airband_gain, airband_squelch = parse_controls(conf_path)
            ground_gain, ground_squelch = parse_controls(GROUND_CONFIG_PATH)
            airband_filter = parse_filter("airband")
            ground_filter = parse_filter("ground")
            rtl_ok = unit_active(UNITS["rtl"])
            # Robustly count top-level device blocks using regex
            def count_device_blocks(conf_path):
                try:
                    with open(conf_path, "r", encoding="utf-8") as f:
                        conf = f.read()
                    # Find the devices section
                    start = conf.find('devices:')
                    if start == -1:
                        return 1
                    start = conf.find('(', start)
                    end = conf.find(');', start)
                    if start == -1 or end == -1:
                        return 1
                    devices_blob = conf[start+1:end]
                    # Minimal parser: count top-level curly-brace blocks
                    count = 0
                    depth = 0
                    in_string = False
                    for c in devices_blob:
                        if c == '"':
                            in_string = not in_string
                        elif not in_string:
                            if c == '{':
                                if depth == 0:
                                    count += 1
                                depth += 1
                            elif c == '}':
                                depth = max(0, depth - 1)
                    return count
                except Exception:
                    return 1

            num_devices = count_device_blocks(conf_path)
            ground_exists = num_devices > 1
            rtl_exists = num_devices >= 1
            # If main rtl-airband service is running and device exists, airband is active
            rtl_ok = rtl_ok and rtl_exists
            # If main rtl-airband service is running and more than one device, ground is active
            ground_ok = rtl_ok and ground_exists
            ice_ok = icecast_up()

            prof_payload, profiles_airband, profiles_ground = split_profiles()
            missing = [p["path"] for p in prof_payload if not p.get("exists")]
            profile_airband = guess_current_profile(conf_path, [(p["id"], p["label"], p["path"]) for p in profiles_airband])
            profile_ground = guess_current_profile(os.path.realpath(GROUND_CONFIG_PATH), [(p["id"], p["label"], p["path"]) for p in profiles_ground])
            last_hit_airband = read_last_hit_airband()
            last_hit_ground = read_last_hit_ground()
            icecast_hit = read_last_hit_from_icecast() if ice_ok else ""

            airband_active_stats = False
            ground_active_stats = False
            try:
                stats = parse_stats_file()
                for freq_str, data in (stats or {}).items():
                    delta = data.get("active_delta", 0)
                    if delta and delta > 0:
                        try:
                            num = float(freq_str)
                            if 118.0 <= num <= 136.0:
                                airband_active_stats = True
                            else:
                                ground_active_stats = True
                        except (TypeError, ValueError):
                            continue
            except Exception:
                pass

            payload = {
                "rtl_active": rtl_ok,
                "ground_active": ground_ok,                "ground_exists": ground_exists,
                "icecast_active": ice_ok,
                "keepalive_active": unit_active(UNITS["keepalive"]),
                "server_time": time.time(),
                "profile_airband": profile_airband,
                "profile_ground": profile_ground,
                "profiles_airband": profiles_airband,
                "profiles_ground": profiles_ground,
                "missing_profiles": missing,
                "gain": float(airband_gain),
                "squelch": float(airband_squelch),
                "airband_gain": float(airband_gain),
                "airband_squelch": float(airband_squelch),
                "airband_filter": float(airband_filter),
                "ground_gain": float(ground_gain),
                "ground_squelch": float(ground_squelch),
                "ground_filter": float(ground_filter),
                "last_hit": icecast_hit or read_last_hit_from_icecast() or "",
                "last_hit_airband": last_hit_airband,
                "last_hit_ground": last_hit_ground,
                "airband_active_stats": airband_active_stats,
                "ground_active_stats": ground_active_stats,
                "avoids_airband": summarize_avoids(conf_path, "airband"),
                "avoids_ground": summarize_avoids(os.path.realpath(GROUND_CONFIG_PATH), "ground"),
            }
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        if p == "/api/hits":
            items = read_icecast_hit_list(limit=50)
            # Append journalctl-based hits if needed for broader coverage
            if len(items) < 20:
                journal_items = read_hit_list_cached()
                # Merge and deduplicate based on time and frequency
                existing_times = {(item.get("time"), item.get("freq")) for item in items}
                for item in journal_items:
                    if (item.get("time"), item.get("freq")) not in existing_times:
                        items.append(item)
                        existing_times.add((item.get("time"), item.get("freq")))
            payload = {"items": items[:50]}
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
                airband_gain, airband_squelch = parse_controls(conf_path)
                rtl_ok = unit_active(UNITS["rtl"])
                # Unified device block counting
                def count_device_blocks(conf_path):
                    try:
                        with open(conf_path, "r", encoding="utf-8") as f:
                            conf = f.read()
                        m = re.search(r'devices:\s*\((.*)\)\s*;', conf, re.S)
                        if not m:
                            return 1
                        devices_blob = m.group(1)
                        depth = 0
                        count = 0
                        for c in devices_blob:
                            if c == '{':
                                if depth == 0:
                                    count += 1
                                depth += 1
                            elif c == '}':
                                depth = max(0, depth - 1)
                        return count
                    except Exception:
                        return 1
                num_devices = count_device_blocks(conf_path)
                ground_exists = num_devices > 1
                rtl_exists = num_devices >= 1
                rtl_active = rtl_ok and rtl_exists
                ground_active = rtl_ok and ground_exists
                ice_ok = icecast_up()
                last_hit_airband = read_last_hit_airband()
                last_hit_ground = read_last_hit_ground()
                last_hit = read_last_hit_from_icecast() if ice_ok else last_hit_airband
                airband_active_stats = False
                ground_active_stats = False
                try:
                    stats = parse_stats_file()
                    for freq_str, data in (stats or {}).items():
                        delta = data.get("active_delta", 0)
                        if delta and delta > 0:
                            try:
                                num = float(freq_str)
                                if 118.0 <= num <= 136.0:
                                    airband_active_stats = True
                                else:
                                    ground_active_stats = True
                            except (TypeError, ValueError):
                                continue
                except Exception:
                    pass
                status_data = {
                    "type": "status",
                    "rtl_active": rtl_active,
                    "ground_active": ground_active,
                    "icecast_active": ice_ok,
                    "gain": float(airband_gain),
                    "squelch": float(airband_squelch),
                    "last_hit": last_hit,
                    "last_hit_airband": last_hit_airband,
                    "last_hit_ground": last_hit_ground,
                    "airband_active_stats": airband_active_stats,
                    "ground_active_stats": ground_active_stats,
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
                hits = read_icecast_hit_list(limit=10)
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
        form = {k: v[0] for k, v in parse_qs(raw).items()}

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
                squelch = float(form.get("squelch", "10.0"))
            except ValueError:
                from systemd import start_rtl
                start_rtl()
                return self._send(400, json.dumps({"ok": False, "error": "bad values"}), "application/json; charset=utf-8")
            result = enqueue_apply(target, gain, squelch)
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
