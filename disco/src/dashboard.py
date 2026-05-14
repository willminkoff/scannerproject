"""Disco dashboard — detections (with mod class) + live spectrum + waterfall. Port 8092."""
import asyncio
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from typing import Optional

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
import uvicorn

# Phase 5 listen integration. listen.py exposes module-level listen()/stop()/
# list_active() that manage rtl-airband symlinks + restart. We just wrap them
# as endpoints; all the rtl-airband + sudoers plumbing is over there.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import listen as listen_mod
    _LISTEN_AVAILABLE = True
except Exception as _le:
    listen_mod = None
    _LISTEN_AVAILABLE = False
    _LISTEN_IMPORT_ERROR = _le

# /usr/local/bin/disco-svc-ctl is allowed via NOPASSWD sudoers for the ubuntu
# user — it stops/starts the RSPduo-owning sweep + classifier services so the
# user can hand the radios back to SB3 without ssh'ing.
SVC_CTL = "/usr/local/bin/disco-svc-ctl"

CONFIG_PATH = os.environ.get("DISCO_CONFIG", "/home/ubuntu/scannerproject/disco/configs/sweep.yaml")
STATE_DIR = os.environ.get("DISCO_STATE_DIR", "/run/scannerproject/disco")
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)
DB_PATH = CFG["db"]["path"]
TUNER_ORDER = sorted(CFG["tuners"].keys())

app = FastAPI(title="Disco")


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=5.0)
    c.row_factory = sqlite3.Row
    return c


def _load_state(tuner_id: str) -> Optional[dict]:
    path = os.path.join(STATE_DIR, f"spectrum_{tuner_id}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None


@app.get("/api/config")
def api_config():
    return {
        "tuners": {tid: {
            "band_start_hz": cfg["band_start_hz"],
            "band_end_hz": cfg["band_end_hz"],
        } for tid, cfg in CFG["tuners"].items()},
        "tuner_order": TUNER_ORDER,
    }


def _svc_ctl(action: str) -> dict:
    """Invoke the disco-svc-ctl wrapper via sudo. Returns parsed status."""
    # mode-off / mode-on can take ~12-15s end-to-end (drain + service start),
    # so widen the timeout for those.
    timeout = 45 if action in ("mode-off", "mode-on") else 30
    try:
        proc = subprocess.run(
            ["sudo", "-n", SVC_CTL, action],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "action": action}
    out = proc.stdout.strip()
    err = proc.stderr.strip()
    units = {}
    # status, mode-status, and the mode-off/mode-on actions all emit
    # one-line-per-unit state on stdout.
    if action in ("status", "mode-status", "mode-off", "mode-on"):
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                units[parts[0]] = parts[1]
    return {
        "ok": proc.returncode == 0,
        "action": action,
        "returncode": proc.returncode,
        "stdout": out,
        "stderr": err,
        "units": units,
    }


@app.get("/api/services/status")
def api_services_status():
    return _svc_ctl("status")


@app.post("/api/services/start")
def api_services_start():
    return _svc_ctl("start")


@app.post("/api/services/stop")
def api_services_stop():
    return _svc_ctl("stop")


# Handoff actions — toggle only the radio-owning sweep@ instances and the
# SB3 digital stack. Classifier and interpret stay running across the
# toggle (warm-cache preservation), and sweep@ instances are masked while
# SB3 owns the radios so a classifier restart can't pull them back.
@app.post("/api/services/mode-off")
def api_services_mode_off():
    return _svc_ctl("mode-off")


@app.post("/api/services/mode-on")
def api_services_mode_on():
    return _svc_ctl("mode-on")


@app.get("/api/services/mode-status")
def api_services_mode_status():
    return _svc_ctl("mode-status")


# --- Phase 5 listen endpoints -----------------------------------------------

class ListenBody(BaseModel):
    freq_hz: float
    bandwidth_hz: float | None = None
    modulation_class: str = ""
    protocol_tag: str | None = None
    user_id: str | None = None


class StopBody(BaseModel):
    freq_hz: float
    user_id: str | None = None


class MuteBody(BaseModel):
    freq_hz: float
    muted: bool
    user_id: str | None = None


@app.post("/api/decode/listen")
def api_decode_listen(body: ListenBody):
    if not _LISTEN_AVAILABLE:
        return {"status": "error", "detail": f"listen module unavailable: {_LISTEN_IMPORT_ERROR}"}
    listen_mod.init_schema(DB_PATH)
    req = listen_mod.ListenRequest(
        freq_hz=body.freq_hz,
        bandwidth_hz=body.bandwidth_hz,
        modulation_class=body.modulation_class,
        protocol_tag=body.protocol_tag,
    )
    res = listen_mod.listen(req, db_path=DB_PATH, user_id=body.user_id)
    return {
        "status": res.status, "detail": res.detail,
        "stream_url": res.stream_url, "target": res.target,
        "modulation": res.modulation,
    }


@app.post("/api/decode/stop")
def api_decode_stop(body: StopBody):
    if not _LISTEN_AVAILABLE:
        return {"status": "error", "detail": f"listen module unavailable: {_LISTEN_IMPORT_ERROR}"}
    listen_mod.init_schema(DB_PATH)
    res = listen_mod.stop(body.freq_hz, db_path=DB_PATH, user_id=body.user_id)
    return {
        "status": res.status, "detail": res.detail,
        "stream_url": res.stream_url, "target": res.target,
    }


@app.get("/api/decode/active")
def api_decode_active():
    if not _LISTEN_AVAILABLE:
        return {"items": [], "stream_url": ""}
    return listen_mod.list_active()


@app.post("/api/decode/mute")
def api_decode_mute(body: MuteBody):
    if not _LISTEN_AVAILABLE:
        return {"status": "error", "detail": f"listen module unavailable: {_LISTEN_IMPORT_ERROR}"}
    listen_mod.init_schema(DB_PATH)
    res = listen_mod.mute(body.freq_hz, body.muted, db_path=DB_PATH, user_id=body.user_id)
    return {
        "status": res.status, "detail": res.detail,
        "stream_url": res.stream_url, "target": res.target,
    }


# --- Phase 4 polish: persistent favorites -----------------------------------

def _ensure_favorites_table():
    """Create the favorites table if absent. Cheap; called per-request."""
    c = _conn()
    try:
        c.execute(
            "CREATE TABLE IF NOT EXISTS disco_favorites ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  freq_hz REAL NOT NULL UNIQUE,"
            "  label TEXT,"
            "  modulation_class TEXT,"
            "  protocol_tag TEXT,"
            "  uls_callsign TEXT,"
            "  uls_entity_name TEXT,"
            "  added_ts REAL NOT NULL,"
            "  notes TEXT"
            ")"
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_fav_freq ON disco_favorites(freq_hz)")
        c.commit()
    finally:
        c.close()


class FavoriteAddBody(BaseModel):
    freq_hz: float
    label: str | None = None
    modulation_class: str | None = None
    protocol_tag: str | None = None
    uls_callsign: str | None = None
    uls_entity_name: str | None = None


class FavoriteRemoveBody(BaseModel):
    freq_hz: float


@app.get("/api/favorites")
def api_favorites_list(active_window_s: float = 600.0):
    """Return favorites with last-seen activity in the recent window."""
    _ensure_favorites_table()
    cutoff = time.time() - active_window_s
    c = _conn()
    try:
        favs = c.execute(
            "SELECT freq_hz, label, modulation_class, protocol_tag, "
            "uls_callsign, uls_entity_name, added_ts, notes "
            "FROM disco_favorites ORDER BY freq_hz"
        ).fetchall()
        out = []
        for f in favs:
            d = dict(f)
            # Bin-aware last-seen (25 kHz bin matches the strongest-signals SQL)
            row = c.execute(
                "SELECT MAX(ts) as last_seen, COUNT(*) as hits, MAX(snr_db) as max_snr "
                "FROM detections "
                "WHERE ts >= ? AND ABS(freq_hz - ?) < 25000",
                (cutoff, d["freq_hz"]),
            ).fetchone()
            d["last_seen"] = row["last_seen"]
            d["recent_hits"] = row["hits"] or 0
            d["recent_max_snr"] = row["max_snr"]
            out.append(d)
        return {"items": out, "active_window_s": active_window_s}
    finally:
        c.close()


@app.post("/api/favorites/add")
def api_favorites_add(body: FavoriteAddBody):
    _ensure_favorites_table()
    c = _conn()
    try:
        # 25 kHz bin dedup so re-clicking near the same freq doesn't create a duplicate
        existing = c.execute(
            "SELECT id FROM disco_favorites WHERE ABS(freq_hz - ?) < 25000 LIMIT 1",
            (body.freq_hz,),
        ).fetchone()
        if existing:
            return {"status": "already", "id": existing["id"]}
        cur = c.execute(
            "INSERT INTO disco_favorites (freq_hz, label, modulation_class, protocol_tag,"
            " uls_callsign, uls_entity_name, added_ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (body.freq_hz, body.label, body.modulation_class, body.protocol_tag,
             body.uls_callsign, body.uls_entity_name, time.time()),
        )
        c.commit()
        return {"status": "added", "id": cur.lastrowid}
    finally:
        c.close()


@app.post("/api/favorites/remove")
def api_favorites_remove(body: FavoriteRemoveBody):
    _ensure_favorites_table()
    c = _conn()
    try:
        cur = c.execute(
            "DELETE FROM disco_favorites WHERE ABS(freq_hz - ?) < 25000",
            (body.freq_hz,),
        )
        c.commit()
        return {"status": "removed", "n": cur.rowcount}
    finally:
        c.close()


# --- Phase 4 polish: hidden rows --------------------------------------------
# Mirrors the favorites pattern. Stations the user doesn't want cluttering the
# table (e.g. all 30 commercial FM broadcast stations once CDBS labels them)
# get a freq stored in disco_hidden; refreshTables filters them out client-side.

def _ensure_hidden_table():
    c = _conn()
    try:
        c.execute(
            "CREATE TABLE IF NOT EXISTS disco_hidden ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  freq_hz REAL NOT NULL UNIQUE,"
            "  label TEXT,"
            "  added_ts REAL NOT NULL,"
            "  notes TEXT"
            ")"
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_hidden_freq ON disco_hidden(freq_hz)")
        c.commit()
    finally:
        c.close()


class HideAddBody(BaseModel):
    freq_hz: float
    label: str | None = None


class HideRemoveBody(BaseModel):
    freq_hz: float


@app.get("/api/hidden")
def api_hidden_list():
    _ensure_hidden_table()
    c = _conn()
    try:
        rows = c.execute(
            "SELECT freq_hz, label, added_ts FROM disco_hidden ORDER BY freq_hz"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
    finally:
        c.close()


@app.post("/api/hidden/add")
def api_hidden_add(body: HideAddBody):
    _ensure_hidden_table()
    c = _conn()
    try:
        existing = c.execute(
            "SELECT id FROM disco_hidden WHERE ABS(freq_hz - ?) < 25000 LIMIT 1",
            (body.freq_hz,),
        ).fetchone()
        if existing:
            return {"status": "already", "id": existing["id"]}
        cur = c.execute(
            "INSERT INTO disco_hidden (freq_hz, label, added_ts) VALUES (?, ?, ?)",
            (body.freq_hz, body.label, time.time()),
        )
        c.commit()
        return {"status": "added", "id": cur.lastrowid}
    finally:
        c.close()


@app.post("/api/hidden/remove")
def api_hidden_remove(body: HideRemoveBody):
    _ensure_hidden_table()
    c = _conn()
    try:
        cur = c.execute(
            "DELETE FROM disco_hidden WHERE ABS(freq_hz - ?) < 25000",
            (body.freq_hz,),
        )
        c.commit()
        return {"status": "removed", "n": cur.rowcount}
    finally:
        c.close()


@app.post("/api/hidden/clear")
def api_hidden_clear():
    _ensure_hidden_table()
    c = _conn()
    try:
        cur = c.execute("DELETE FROM disco_hidden")
        c.commit()
        return {"status": "cleared", "n": cur.rowcount}
    finally:
        c.close()


@app.get("/api/detections")
def api_detections(since_seconds: float = 60.0, limit: int = 1000):
    cutoff = time.time() - since_seconds
    c = _conn()
    rows = c.execute(
        "SELECT ts, tuner_id, freq_hz, bandwidth_hz, power_dbfs, snr_db, "
        "modulation_class, modulation_confidence, protocol_tag, "
        "uls_callsign, uls_entity_name, uls_emission_designator, "
        "uls_station_class, uls_distance_km, uls_source "
        "FROM detections WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


@app.get("/api/strongest")
def api_strongest(since_seconds: float = 60.0, per_tuner: int = 15, bin_khz: float = 25.0):
    cutoff = time.time() - since_seconds
    bin_hz = bin_khz * 1000.0
    c = _conn()
    out = {}
    total = 0
    for tid in TUNER_ORDER:
        rows = c.execute(
            "SELECT MIN(freq_hz) as freq_hz, MAX(power_dbfs) as max_power, "
            "MAX(snr_db) as max_snr, COUNT(*) as hits, MAX(ts) as last_seen, "
            "( SELECT modulation_class FROM detections d2 "
            "  WHERE d2.tuner_id = detections.tuner_id "
            "    AND CAST(d2.freq_hz / ? AS INTEGER) = CAST(detections.freq_hz / ? AS INTEGER) "
            "    AND d2.modulation_class IS NOT NULL "
            "    AND d2.ts >= ? "
            "  ORDER BY d2.modulation_confidence DESC LIMIT 1 ) as modulation_class, "
            "( SELECT modulation_confidence FROM detections d2 "
            "  WHERE d2.tuner_id = detections.tuner_id "
            "    AND CAST(d2.freq_hz / ? AS INTEGER) = CAST(detections.freq_hz / ? AS INTEGER) "
            "    AND d2.modulation_class IS NOT NULL "
            "    AND d2.ts >= ? "
            "  ORDER BY d2.modulation_confidence DESC LIMIT 1 ) as modulation_confidence, "
            "( SELECT protocol_tag FROM detections d2 "
            "  WHERE d2.tuner_id = detections.tuner_id "
            "    AND CAST(d2.freq_hz / ? AS INTEGER) = CAST(detections.freq_hz / ? AS INTEGER) "
            "    AND d2.modulation_class IS NOT NULL "
            "    AND d2.ts >= ? "
            "  ORDER BY d2.modulation_confidence DESC LIMIT 1 ) as protocol_tag, "
            "( SELECT interpretation FROM detections d2 "
            "  WHERE d2.tuner_id = detections.tuner_id "
            "    AND CAST(d2.freq_hz / ? AS INTEGER) = CAST(detections.freq_hz / ? AS INTEGER) "
            "    AND d2.interpretation IS NOT NULL "
            "    AND d2.ts >= ? "
            "  ORDER BY d2.interpreted_ts DESC LIMIT 1 ) as interpretation, "
            # Phase 3: ULS enrichment per bin. We want the licensee for the
            # strongest hit in the bin; ranking by snr_db DESC reuses the same
            # row the modulation columns above already used for tag/class.
            "( SELECT uls_callsign FROM detections d2 "
            "  WHERE d2.tuner_id = detections.tuner_id "
            "    AND CAST(d2.freq_hz / ? AS INTEGER) = CAST(detections.freq_hz / ? AS INTEGER) "
            "    AND d2.uls_callsign IS NOT NULL AND d2.ts >= ? "
            "  ORDER BY d2.snr_db DESC LIMIT 1 ) as uls_callsign, "
            "( SELECT uls_entity_name FROM detections d2 "
            "  WHERE d2.tuner_id = detections.tuner_id "
            "    AND CAST(d2.freq_hz / ? AS INTEGER) = CAST(detections.freq_hz / ? AS INTEGER) "
            "    AND d2.uls_callsign IS NOT NULL AND d2.ts >= ? "
            "  ORDER BY d2.snr_db DESC LIMIT 1 ) as uls_entity_name, "
            "( SELECT uls_emission_designator FROM detections d2 "
            "  WHERE d2.tuner_id = detections.tuner_id "
            "    AND CAST(d2.freq_hz / ? AS INTEGER) = CAST(detections.freq_hz / ? AS INTEGER) "
            "    AND d2.uls_callsign IS NOT NULL AND d2.ts >= ? "
            "  ORDER BY d2.snr_db DESC LIMIT 1 ) as uls_emission_designator, "
            "( SELECT uls_station_class FROM detections d2 "
            "  WHERE d2.tuner_id = detections.tuner_id "
            "    AND CAST(d2.freq_hz / ? AS INTEGER) = CAST(detections.freq_hz / ? AS INTEGER) "
            "    AND d2.uls_callsign IS NOT NULL AND d2.ts >= ? "
            "  ORDER BY d2.snr_db DESC LIMIT 1 ) as uls_station_class, "
            "( SELECT uls_distance_km FROM detections d2 "
            "  WHERE d2.tuner_id = detections.tuner_id "
            "    AND CAST(d2.freq_hz / ? AS INTEGER) = CAST(detections.freq_hz / ? AS INTEGER) "
            "    AND d2.uls_callsign IS NOT NULL AND d2.ts >= ? "
            "  ORDER BY d2.snr_db DESC LIMIT 1 ) as uls_distance_km "
            "FROM detections WHERE ts >= ? AND tuner_id = ? "
            "GROUP BY CAST(freq_hz / ? AS INTEGER) "
            "ORDER BY max_snr DESC LIMIT ?",
            (bin_hz, bin_hz, cutoff,
             bin_hz, bin_hz, cutoff,
             bin_hz, bin_hz, cutoff,
             bin_hz, bin_hz, cutoff,
             bin_hz, bin_hz, cutoff,
             bin_hz, bin_hz, cutoff,
             bin_hz, bin_hz, cutoff,
             bin_hz, bin_hz, cutoff,
             bin_hz, bin_hz, cutoff,
             cutoff, tid, bin_hz, per_tuner)
        ).fetchall()
        out[tid] = [dict(r) for r in rows]
        total += len(rows)
    c.close()
    return {"buckets": out, "total": total, "since_seconds": since_seconds}


@app.get("/api/summary")
def api_summary(since_seconds: float = 60.0):
    cutoff = time.time() - since_seconds
    c = _conn()
    out = {tid: {"count": 0, "max_snr": None, "last_seen": None, "classified": 0} for tid in TUNER_ORDER}
    for r in c.execute(
        "SELECT tuner_id, COUNT(*) as n, MAX(snr_db) as max_snr, MAX(ts) as last_seen, "
        "SUM(CASE WHEN modulation_class IS NOT NULL THEN 1 ELSE 0 END) as classified "
        "FROM detections WHERE ts >= ? GROUP BY tuner_id",
        (cutoff,),
    ).fetchall():
        out[r["tuner_id"]] = {
            "count": r["n"],
            "max_snr": r["max_snr"],
            "last_seen": r["last_seen"],
            "classified": r["classified"] or 0,
        }
    c.close()
    return out


@app.get("/api/spectrum_snapshot/{tuner_id}")
def api_spectrum_snapshot(tuner_id: str):
    if tuner_id not in CFG["tuners"]:
        return {"error": "unknown tuner"}
    s = _load_state(tuner_id)
    return s if s else {"error": "no state yet"}


@app.get("/api/spectrum/{tuner_id}")
async def stream_spectrum(tuner_id: str, mode: str = "composite", request: Request = None):
    if tuner_id not in CFG["tuners"]:
        return {"error": "unknown tuner"}

    async def gen():
        last_ts = 0.0
        while True:
            if request is not None and await request.is_disconnected():
                break
            s = _load_state(tuner_id)
            if s and s.get("ts", 0) > last_ts:
                last_ts = s["ts"]
                if mode == "live_if":
                    payload = {
                        "tuner_id": tuner_id, "mode": "live_if", "ts": s["ts"],
                        "center_hz": s["live_if"]["center_hz"],
                        "sample_rate_hz": s["live_if"]["sample_rate_hz"],
                        "bins_dbfs": s["live_if"]["bins_dbfs"],
                        "current_sweep_pos": s.get("current_sweep_pos"),
                        "total_steps": s.get("total_steps"),
                        "noise_floor": s.get("noise_floor_dbfs_recent"),
                    }
                else:
                    payload = {
                        "tuner_id": tuner_id, "mode": "composite", "ts": s["ts"],
                        "band_min_hz": s["composite"]["band_min_hz"],
                        "band_max_hz": s["composite"]["band_max_hz"],
                        "bins_dbfs": s["composite"]["bins_dbfs"],
                        "bin_age_s": s["composite"].get("bin_age_s"),
                        "current_center_hz": s.get("current_center_hz"),
                        "current_sweep_pos": s.get("current_sweep_pos"),
                        "total_steps": s.get("total_steps"),
                        "cycle_count": s.get("cycle_count"),
                        "noise_floor": s.get("noise_floor_dbfs_recent"),
                    }
                yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream")


HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Disco</title><style>
:root{
  --fs-h1:30px;
  --fs-status:15px;
  --fs-card-h:19px;
  --fs-btn:14px;
  --fs-band:14px;
  --fs-summary:14px;
  --fs-table:14px;
  --fs-th:13px;
  --fs-empty:14px;
  --color-info:#7fc7ff;
  --color-warn:#e6c97a;
  --color-good:#a8e6a8;
}
body{font-family:-apple-system,sans-serif;margin:0;padding:14px;background:#0c0c10;color:#ddd;font-size:var(--fs-table)}
h1{margin:0 0 4px 0;font-size:var(--fs-h1)}
.status{color:#888;font-size:var(--fs-status);margin-bottom:12px}
tr[title]{cursor:help}
tr[title]:hover{background:#1f1f28}
.tuners{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
.tuner{background:#16161c;border:1px solid #2a2a35;border-radius:8px;padding:12px}
.tuner h2{margin:0 0 4px 0;font-size:var(--fs-card-h);color:#e8e8ec;display:flex;justify-content:space-between;align-items:center}
.tuner .ctrl{display:flex;gap:6px;margin-left:auto}
.tuner button{background:#222;color:#bbb;border:1px solid #333;padding:4px 10px;font-size:var(--fs-btn);border-radius:4px;cursor:pointer;font-family:inherit}
.tuner button.active{background:#3a5a8a;color:#fff;border-color:#5a7aaa}
.band{color:#888;font-size:var(--fs-band);margin-bottom:8px;font-family:ui-monospace,monospace}
.summary{font-size:var(--fs-summary);color:#aaa;margin-bottom:8px;font-family:ui-monospace,monospace}
canvas{display:block;width:100%;background:#000;border-radius:3px;margin-bottom:4px}
canvas.spectrum{height:120px;border:1px solid #1f1f25}
canvas.waterfall{height:180px;border:1px solid #1f1f25}
table{width:100%;border-collapse:collapse;font-size:var(--fs-table);font-family:ui-monospace,monospace;margin-top:8px}
th,td{padding:4px 8px;text-align:left;border-bottom:1px solid #25252c;white-space:nowrap}
th{color:#888;font-weight:normal;font-size:var(--fs-th);text-transform:uppercase;letter-spacing:.5px}
.empty{color:#666;font-style:italic;font-size:var(--fs-empty)}
.hot{color:#ffb84d}.warm{color:#7fc7ff}
.mod-high{color:#a8e6a8;font-weight:600}.mod-mid{color:#cccc77}.mod-low{color:#666}
.uls{color:#cdd0d6;max-width:240px;overflow:hidden;text-overflow:ellipsis;cursor:help}
.uls-cs{color:#7a8696;font-size:0.85em;margin-left:6px}
.details-btn{background:#2a2a35;color:#cdd0d6;border:1px solid #3a3a45;border-radius:3px;padding:1px 7px;font-size:11px;cursor:pointer;font-family:inherit;margin-left:6px;line-height:1.3}
.details-btn:hover{background:#3a3a45;color:#fff}
#detail-popup{position:absolute;display:none;z-index:9500;background:#16161c;border:1px solid #3a3a45;color:#dde0e6;padding:10px 14px;border-radius:6px;max-width:420px;font-size:13px;line-height:1.45;box-shadow:0 6px 20px rgba(0,0,0,0.6);white-space:pre-wrap;font-family:-apple-system,sans-serif}
.svc-bar{display:flex;align-items:center;gap:10px;margin:6px 0 10px 0;font-size:14px;font-family:ui-monospace,monospace}
.svc-status{padding:3px 9px;border-radius:4px;border:1px solid #2a2a35;background:#16161c;color:#aaa}
.svc-status.running{color:#a8e6a8;border-color:#3a5a3a}
.svc-status.stopped{color:#e6a8a8;border-color:#5a3a3a}
.svc-status.partial{color:#e6c97a;border-color:#5a4a2a}
.svc-btn{background:#222;color:#bbb;border:1px solid #333;padding:5px 14px;font-size:13px;border-radius:4px;cursor:pointer;font-family:inherit}
.svc-btn:hover{background:#2a2a35;color:#fff}
.svc-btn.start{color:#a8e6a8}
.svc-btn.stop{color:#e6a8a8}
.svc-btn.mode-off{color:#e6c97a}
.svc-btn.mode-on{color:#a8c8e6}
.svc-btn:disabled{opacity:0.45;cursor:not-allowed}
.svc-detail{color:#666;font-size:12px}
.svc-status.handoff{color:#e6c97a;border-color:#5a4a2a}
.svc-status.disco{color:#a8c8e6;border-color:#3a4a5a}
.listen-bar{display:flex;align-items:center;gap:10px;margin:6px 0 10px 0;font-size:13px;font-family:ui-monospace,monospace;flex-wrap:wrap}
.listen-bar-label{color:#888;letter-spacing:.5px;font-size:12px;text-transform:uppercase}
.listen-empty{color:#666;font-style:italic}
.listen-list{display:flex;flex-wrap:wrap;gap:6px}
.listen-pill{display:inline-flex;align-items:center;gap:6px;background:#16161c;border:1px solid #3a5a3a;color:#a8e6a8;padding:2px 4px 2px 10px;border-radius:14px;font-size:12px}
.listen-pill.is-muted{border-color:#5a5a3a;color:#aaa}
.listen-pill.is-muted .listen-freq{text-decoration:line-through;opacity:.7}
.listen-pill button{background:transparent;color:#e6a8a8;border:0;cursor:pointer;font-size:14px;padding:0 4px;line-height:1}
.listen-pill button:hover{color:#fff}
.listen-pill .pill-mute{color:#e6c97a}
.listen-pill .pill-mute:hover{color:#fff}
.listen-stream a{color:#7fc7ff;text-decoration:none}
.listen-stream a:hover{text-decoration:underline}
#disco-audio-player{height:30px;max-width:280px;display:none;vertical-align:middle}
#disco-audio-player.is-active{display:inline-block}
.listen-btn{background:#16161c;color:#a8e6a8;border:1px solid #3a5a3a;border-radius:3px;padding:1px 7px;font-size:11px;cursor:pointer;font-family:inherit;margin-left:6px;line-height:1.3}
.listen-btn.is-active{color:#e6a8a8;border-color:#5a3a3a}
.listen-btn:hover{background:#222}
.filter-bar{display:flex;align-items:center;gap:14px;margin:6px 0 10px 0;font-size:13px;font-family:ui-monospace,monospace;flex-wrap:wrap}
.filter-bar label{color:#888;font-size:11px;letter-spacing:.5px;text-transform:uppercase;display:flex;align-items:center;gap:6px}
.filter-bar select,.filter-bar input{background:#16161c;color:#cdd0d6;border:1px solid #2a2a35;padding:3px 6px;font-family:inherit;font-size:13px;border-radius:3px}
.filter-bar input[type=number]{width:60px}
.filter-bar input[type=text]{width:180px}
.filter-bar .clear{background:#2a2a35;color:#cdd0d6;border:1px solid #3a3a45;padding:3px 10px;font-size:12px;cursor:pointer;border-radius:3px;font-family:inherit}
.filter-bar .clear:hover{background:#3a3a45;color:#fff}
.fav-bar{display:flex;align-items:center;gap:10px;margin:6px 0 10px 0;font-size:13px;font-family:ui-monospace,monospace;flex-wrap:wrap}
.fav-bar-label{color:#888;letter-spacing:.5px;font-size:12px;text-transform:uppercase}
.fav-empty{color:#666;font-style:italic}
.fav-list{display:flex;flex-wrap:wrap;gap:6px}
.fav-pill{display:inline-flex;align-items:center;gap:6px;background:#16161c;border:1px solid #4a4a2a;color:#e6c97a;padding:2px 4px 2px 10px;border-radius:14px;font-size:12px}
.fav-pill.active{border-color:#3a5a3a;color:#a8e6a8}
.fav-pill .fav-meta{color:#7a8696;font-size:11px}
.fav-pill button{background:transparent;color:#7a8696;border:0;cursor:pointer;font-size:14px;padding:0 6px;line-height:1}
.fav-pill button:hover{color:#fff}
.star-btn{background:transparent;color:#666;border:0;cursor:pointer;font-size:14px;padding:0 4px;line-height:1;font-family:inherit;vertical-align:middle}
.star-btn:hover{color:#e6c97a}
.star-btn.is-fav{color:#e6c97a}
/* Hide button — match the star button's muted/transparent aesthetic. The
   default "hide this row" state is a grey eye with a CSS slash drawn across
   it; when a row is currently hidden but the user has toggled "show", the
   slash drops and the eye goes a touch brighter to mean "currently visible". */
.hide-btn{background:transparent;border:0;cursor:pointer;font-size:13px;padding:0 4px;line-height:1;font-family:inherit;vertical-align:middle;position:relative;filter:grayscale(1) brightness(0.85);opacity:0.55}
.hide-btn:hover{opacity:1;filter:grayscale(1) brightness(1.15)}
.hide-btn::after{content:"";position:absolute;left:3px;right:3px;top:50%;border-top:1.5px solid #999;transform:rotate(-22deg);pointer-events:none}
.hide-btn.is-hidden{opacity:0.85;filter:grayscale(0.4) brightness(1)}
.hide-btn.is-hidden::after{display:none}
.hidden-control{color:#7a8696;font-size:12px;display:flex;align-items:center;gap:6px}
.hidden-control button{background:#16161c;color:#cdd0d6;border:1px solid #2a2a35;padding:2px 8px;font-size:11px;cursor:pointer;border-radius:3px;font-family:inherit}
.hidden-control button:hover{background:#2a2a35;color:#fff}
.hidden-control.has-items{color:#e6c97a}
.row-hidden{display:none !important}
/* ============================================================
 * Tier 4 — sticky compact header + gear-revealed controls drawer
 * ============================================================ */
.app-header{position:sticky;top:0;z-index:200;background:rgba(12,12,16,0.96);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);padding:10px 14px 8px;margin:-14px -14px 12px;border-bottom:1px solid #1a1a22;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.app-header .brand{margin:0;font-size:var(--fs-h1);font-weight:800;letter-spacing:-0.5px;color:#f0f0f4;flex-shrink:0;line-height:1.1}
.app-header .brand-sub{color:#7a8696;font-size:12px;margin-left:8px;font-weight:400;letter-spacing:0;vertical-align:middle}
.header-listen{flex:1;min-width:200px;display:flex;align-items:center;gap:10px;font-size:13px;font-family:ui-monospace,monospace;flex-wrap:wrap;margin:0}
.header-listen .listen-bar-label{color:#888;letter-spacing:.5px;font-size:11px;text-transform:uppercase}
.gear-btn{background:#1a1a22;color:#cdd0d6;border:1px solid #2a2a35;border-radius:6px;padding:6px 12px;font-size:18px;cursor:pointer;font-family:inherit;line-height:1;display:none;margin-left:auto}
.gear-btn:hover{background:#2a2a35;color:#fff}
.gear-btn.is-open{background:#3a5a8a;color:#fff;border-color:#5a7aaa}
#controls-drawer{display:block}
.collapsible-toggle{display:none;background:#16161c;color:#cdd0d6;border:1px solid #2a2a35;border-radius:6px;padding:9px 12px;font-size:13px;font-family:ui-monospace,monospace;cursor:pointer;width:100%;text-align:left;align-items:center;justify-content:space-between;margin:6px 0;letter-spacing:.5px;text-transform:uppercase}
.collapsible-toggle:hover{background:#1f1f28;color:#fff}
.collapsible-toggle .chev{display:inline-block;transition:transform 0.18s ease;margin-right:6px;color:#7a8696}
.collapsible-toggle.is-open .chev{transform:rotate(180deg);color:#cdd0d6}
.collapsible-toggle .count{color:#7a8696;font-size:11px;margin-left:6px;text-transform:none;letter-spacing:0}
.collapsible-toggle.has-active .count{color:var(--color-warn)}

/* ============================================================
 * Tier 4 — compact band badges (BCST/AVI/HAM/...) shown on phone
 * ============================================================ */
.mode-compact{display:none}
.mode-badge{display:inline-block;font-weight:700;letter-spacing:.5px;padding:1px 6px;border-radius:3px;font-size:11px;margin-right:6px;background:#1a1a22;border:1px solid #2a2a35;vertical-align:baseline}
.mode-badge.band-allowed{color:var(--color-info);border-color:#3a4a5a;background:rgba(127,199,255,0.08)}
.mode-badge.band-rejected{color:var(--color-warn);border-color:#5a4a2a;background:rgba(230,201,122,0.08)}

/* ============================================================
 * Tier 4 — pulsing border on rows currently piped to audio
 * ============================================================ */
@keyframes listen-pulse{
  0%,100%{box-shadow:inset 3px 0 0 rgba(168,230,168,0.85)}
  50%{box-shadow:inset 3px 0 0 rgba(168,230,168,0.25)}
}
tr.is-listening td:first-child{position:relative}
tr.is-listening{background:rgba(58,90,58,0.10);animation:listen-pulse 1.5s ease-in-out infinite}
tr.is-listening:hover{background:rgba(58,90,58,0.18)}

/* ============================================================
 * Tier 4 — tuner card visual punch (always-on)
 * ============================================================ */
.tuner h2 span:first-child{font-weight:700;letter-spacing:.2px}
.tuner .summary{font-weight:500;color:#bbb}
.tuner .band{font-weight:600;color:#9aa0aa}

/* ============================================================
 * Tier 4 — tablet (≤820px): single column, smaller canvases
 * ============================================================ */
@media (max-width: 820px){
  body{padding:10px}
  .app-header{margin:-10px -10px 10px;padding:10px 12px 8px}
  .tuners{grid-template-columns:1fr;gap:16px}
  canvas.spectrum{height:100px}
  canvas.waterfall{height:140px}
  .filter-bar input[type=text]{width:140px}
}

/* ============================================================
 * Tier 4 — phone (≤480px): drawer, card-view rows, badges, etc.
 * ============================================================ */
@media (max-width: 480px){
  body{padding:0;font-size:15px}
  :root{
    --fs-h1:26px;
    --fs-card-h:18px;
    --fs-table:14px;
    --fs-status:12px;
    --fs-band:13px;
    --fs-summary:13px;
  }
  .app-header{padding:10px 12px 8px;margin:0 0 10px;border-radius:0;flex-wrap:nowrap;gap:8px}
  .app-header .brand{font-size:24px}
  .app-header .brand-sub{display:none}
  .header-listen{flex:1;min-width:0;font-size:12px;gap:6px}
  #disco-audio-player{max-width:160px;height:26px}
  .gear-btn{display:block;flex-shrink:0;padding:6px 10px;font-size:16px}

  /* Drawer collapsed by default — gear toggles is-open */
  #controls-drawer{display:none;padding:0 12px 4px;border-bottom:1px solid #1a1a22;margin-bottom:10px}
  #controls-drawer.is-open{display:block}
  .svc-bar{margin:6px 0;flex-wrap:wrap;font-size:13px;gap:8px}
  .svc-btn{padding:7px 14px;font-size:13px}
  .svc-detail{font-size:11px;flex-basis:100%;line-height:1.3}

  /* Collapsible Favorites + Filters on phone */
  .collapsible-toggle{display:flex}
  .fav-bar,.filter-bar{display:none;margin:0 0 8px 0;padding:8px 2px 4px;flex-direction:column;align-items:stretch;gap:8px;border-top:1px solid #1a1a22}
  .fav-bar.is-open,.filter-bar.is-open{display:flex}
  .filter-bar label{font-size:11px;color:#888;flex-direction:column;align-items:stretch;gap:4px}
  .filter-bar select,.filter-bar input,.filter-bar input[type=number],.filter-bar input[type=text]{width:100%;box-sizing:border-box;font-size:14px;padding:6px 8px}
  .filter-bar .clear{padding:6px 10px;font-size:13px}
  .hidden-control{flex-wrap:wrap;border-top:1px solid #1a1a22;padding-top:8px;font-size:12px}
  .fav-bar .fav-list{flex-direction:column;align-items:stretch;gap:6px}
  .fav-bar .fav-pill{justify-content:space-between}

  /* Tuner cards padded inline + bigger card header */
  .status{padding:0 12px}
  .tuners{padding:0 12px;gap:14px}
  .tuner{padding:10px;border-radius:10px}
  .tuner h2{font-size:18px;flex-direction:column;align-items:flex-start;gap:8px;margin-bottom:8px}
  .tuner h2 .ctrl{margin-left:0}
  .tuner h2 span:first-child{font-weight:800;letter-spacing:0;color:#fff}
  canvas.spectrum{height:80px}
  canvas.waterfall{height:110px}

  /* Compact MODE badge replaces full band-class text on phone */
  .mode-full{display:none}
  .mode-compact{display:inline-block}

  /* Convert detection table rows to standalone cards */
  table[data-strongest]{display:block;margin-top:6px}
  table[data-strongest] thead{display:none}
  table[data-strongest] tbody{display:block}
  table[data-strongest] tr{display:grid;grid-template-columns:1fr auto;gap:4px 12px;padding:10px 12px;margin:0 0 8px 0;background:#16161c;border:1px solid #25252c;border-bottom:1px solid #25252c;border-radius:8px;white-space:normal}
  table[data-strongest] tr:hover{background:#1a1a22}
  table[data-strongest] tr.is-listening{border-color:#3a5a3a}
  table[data-strongest] td{display:block;padding:0;border:0;white-space:normal;font-size:13px;line-height:1.4}
  /* Row 1: freq (big, headline) — full width */
  table[data-strongest] td:nth-child(1){grid-column:1/-1;font-size:18px;font-weight:700;color:#e8e8ec;font-family:ui-monospace,monospace;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
  /* SNR / pwr / hits — inline mini stats */
  table[data-strongest] td:nth-child(2),
  table[data-strongest] td:nth-child(3),
  table[data-strongest] td:nth-child(4){display:inline-block;margin-right:14px;font-size:13px}
  table[data-strongest] td:nth-child(2)::before{content:"SNR ";color:#666;font-size:11px;text-transform:uppercase;margin-right:2px}
  table[data-strongest] td:nth-child(3)::before{content:"PWR ";color:#666;font-size:11px;text-transform:uppercase;margin-right:2px}
  table[data-strongest] td:nth-child(4)::before{content:"HITS ";color:#666;font-size:11px;text-transform:uppercase;margin-right:2px}
  /* Wrap the SNR/PWR/HITS trio into one logical row */
  table[data-strongest] td:nth-child(2){grid-column:1/-1;padding-top:4px;border-top:1px solid #25252c;margin-top:2px}
  table[data-strongest] td:nth-child(3),
  table[data-strongest] td:nth-child(4){grid-column:1/-1;padding-top:0;margin-top:-22px;padding-left:90px}
  table[data-strongest] td:nth-child(4){padding-left:180px}
  /* Mode + conf — full width line */
  table[data-strongest] td:nth-child(5){grid-column:1/-1;padding-top:6px;border-top:1px solid #25252c;margin-top:4px;font-size:14px}
  table[data-strongest] td:nth-child(6){display:none}
  /* Licensee row */
  table[data-strongest] td:nth-child(7){grid-column:1/-1;max-width:none;color:#cdd0d6;font-size:12px}
  table[data-strongest] td:nth-child(7):empty,
  table[data-strongest] td.uls:not(:empty){overflow:visible;text-overflow:initial}
  /* Age — right column, tiny */
  table[data-strongest] td:nth-child(8){grid-column:2;grid-row:1;justify-self:end;color:#666;font-size:11px;font-weight:400;align-self:start;padding-top:6px}
  /* Empty-state row spans entire card */
  table[data-strongest] tr td.empty{grid-column:1/-1;text-align:center;padding:14px}
  /* Detail popup full-width on phone */
  #detail-popup{max-width:calc(100vw - 24px);left:12px !important;right:12px}
}

/* ============================================================
 * Tier 4 — desktop (>1024px): drawer always open, gear hidden
 * ============================================================ */
@media (min-width: 1025px){
  .app-header{padding:14px 18px 12px}
  .gear-btn{display:none}
  #controls-drawer{display:block !important}
  .fav-bar,.filter-bar{display:flex !important}
  .collapsible-toggle{display:none !important}
}
</style></head><body>
<header class="app-header">
  <h1 class="brand">Disco<span class="brand-sub">Phase 2</span></h1>
  <div class="listen-bar header-listen" id="listen-bar">
    <span class="listen-bar-label">Listening</span>
    <span class="listen-empty" id="listen-empty">— tap 🎧 on a row to wire audio</span>
    <span class="listen-list" id="listen-list"></span>
    <audio id="disco-audio-player" preload="none" controls></audio>
    <span class="listen-stream" id="listen-stream"></span>
  </div>
  <button class="gear-btn" id="gear-btn" type="button" aria-label="Show controls" title="Show controls">⚙</button>
</header>
<div id="controls-drawer">
<div class="svc-bar">
  <span class="svc-status" id="svc-status">checking…</span>
  <button class="svc-btn start" id="svc-start" type="button" disabled>Start</button>
  <button class="svc-btn stop" id="svc-stop" type="button" disabled>Stop</button>
  <span class="svc-detail" id="svc-detail">full Disco control — Stop also takes the classifier down (warm cache lost)</span>
</div>
<div class="svc-bar mode-bar">
  <span class="svc-status" id="mode-status">checking…</span>
  <button class="svc-btn mode-off" id="mode-off-btn" type="button" disabled>Hand off to SB3</button>
  <button class="svc-btn mode-on" id="mode-on-btn" type="button" disabled>Reclaim radios</button>
  <span class="svc-detail" id="mode-detail">at-home handoff — classifier stays warm across the toggle</span>
</div>
<button class="collapsible-toggle" id="fav-toggle" type="button" aria-controls="fav-bar" aria-expanded="false">
  <span><span class="chev">▼</span>Favorites<span class="count" id="fav-toggle-count">(0)</span></span>
</button>
<div class="fav-bar" id="fav-bar">
  <span class="fav-bar-label">Favorites</span>
  <span class="fav-empty" id="fav-empty">— click ☆ on any row to track it over time</span>
  <span class="fav-list" id="fav-list"></span>
</div>
<button class="collapsible-toggle" id="filter-toggle" type="button" aria-controls="filter-bar" aria-expanded="false">
  <span><span class="chev">▼</span>Filters<span class="count" id="filter-toggle-count">(0 active)</span></span>
</button>
<div class="filter-bar" id="filter-bar">
  <label>mode
    <select id="filter-mode">
      <option value="">all</option>
      <option value="FM_BROADCAST">FM_BROADCAST</option>
      <option value="FM_NARROW">FM_NARROW</option>
      <option value="AM_VOICE">AM_VOICE</option>
      <option value="DMR">DMR</option>
      <option value="GMSK">GMSK</option>
      <option value="QPSK">QPSK</option>
      <option value="OQPSK">OQPSK</option>
      <option value="QAM">QAM</option>
      <option value="OOK">OOK</option>
      <option value="NOISE">NOISE</option>
      <option value="unclassified">unclassified</option>
    </select>
  </label>
  <label>min SNR <input type="number" id="filter-snr" min="0" max="80" step="1" value="0"></label>
  <label>licensee <input type="text" id="filter-licensee" placeholder="contains…"></label>
  <label>window
    <select id="filter-window">
      <option value="60">60s</option>
      <option value="120" selected>120s</option>
      <option value="300">5m</option>
      <option value="900">15m</option>
      <option value="1800">30m</option>
    </select>
  </label>
  <button class="clear" id="filter-clear" type="button">clear</button>
  <span class="hidden-control" id="hidden-control">
    <span id="hidden-count">0 hidden</span>
    <button id="hidden-toggle" type="button" title="Temporarily show hidden rows">show</button>
    <button id="hidden-clear" type="button" title="Unhide everything">unhide all</button>
  </span>
  <span class="svc-detail" id="filter-summary"></span>
</div>
</div>
<div class="status" id="status">loading…</div>
<div class="tuners" id="tuners"></div>
<div id="detail-popup" role="dialog" aria-hidden="true"></div>
<script>
const WATERFALL_ROWS = 160;
const SPECTRUM_DB_MIN = -100, SPECTRUM_DB_MAX = -30;
let CONFIG = null;
const tuners = {};
// Phase 5 listen integration: rtl-airband only handles analog FM/AM at the
// moment, so the per-row "Listen" button only appears for these classes.
const LISTEN_SUPPORTED = new Set(["FM_BROADCAST", "FM_NARROW", "AM_VOICE"]);
const ACTIVE_LISTEN_FREQS = new Set();  // freqs (Hz, rounded) currently wired

function dbToColor(db){
  let t = (db - SPECTRUM_DB_MIN) / (SPECTRUM_DB_MAX - SPECTRUM_DB_MIN);
  if(t<0) t=0; if(t>1) t=1;
  let r,g,b;
  if(t<0.25){const u=t/0.25; r=0; g=Math.round(u*64); b=Math.round(64+u*191);}
  else if(t<0.5){const u=(t-0.25)/0.25; r=0; g=Math.round(64+u*191); b=Math.round(255*(1-u));}
  else if(t<0.75){const u=(t-0.5)/0.25; r=Math.round(u*255); g=255; b=0;}
  else{const u=(t-0.75)/0.25; r=255; g=Math.round(255*(1-u)); b=0;}
  return [r,g,b];
}
function snrClass(snr){ if(snr>=25) return "hot"; if(snr>=18) return "warm"; return ""; }
function modConfClass(c){ if(c==null) return "mod-low"; if(c>=0.75) return "mod-high"; if(c>=0.5) return "mod-mid"; return "mod-low"; }

// Tier 4: derive a 3-letter band badge from protocol_tag for the phone view.
// `protocol_tag` shape from band_plan.tag_for() is "<BAND_NAME> — <class>" for
// in-band hits and "<BAND_NAME> — unidentified" for band-rejected ones; out-of-
// band signals fall through to bare modulation_class. Specific bands (LMR_800,
// PS_700_NARROW, PS_800_NARROW, NOAA_WX, RADIO_ASTRONOMY, DME_TACAN, METAIDS)
// match before the prefix groups so they don't get folded into [LMR] / [WX].
const BAND_ABBREV_EXACT = {
  "LMR_800":"LMR8", "PS_700_NARROW":"PS7", "PS_800_NARROW":"PS8",
  "NOAA_WX":"WX", "RADIO_ASTRONOMY":"RA", "DME_TACAN":"DME", "METAIDS":"MET",
};
const BAND_ABBREV_PREFIX = [
  ["BCAST_","BCST"], ["AVIATION_","AVI"], ["GOV_","GOV"], ["MIL_","MIL"],
  ["CELL_","CEL"], ["UHF_LMR_","LMR"], ["VHF_LMR_","LMR"], ["LMR_","LMR"],
  ["PS_","LMR"], ["TV_","TV"], ["AMATEUR_","HAM"], ["ISM_","ISM"],
];
function bandAbbrev(protocolTag, modulationClass){
  if (!protocolTag) {
    // No band-plan tag — fall back to the first 4 chars of the modulation class.
    const m = (modulationClass || "").toUpperCase().replace(/[^A-Z0-9]/g,"");
    return {label: m.slice(0,4) || "?", rejected: false, bandName: ""};
  }
  // Split on em-dash; left side is BAND_NAME, right side is class or "unidentified".
  // Python sees this triple-quoted HTML as a regular string, so \\s here escapes
  // the regex \s into the rendered JS — without the double backslash Python emits
  // SyntaxWarning ("invalid escape sequence") on every import.
  const parts = String(protocolTag).split(/\\s*[—\\-]\\s*/);
  const bandName = (parts[0] || "").trim().toUpperCase();
  const rejected = parts.length > 1 && /unidentified/i.test(parts[1] || "");
  if (BAND_ABBREV_EXACT[bandName]) return {label: BAND_ABBREV_EXACT[bandName], rejected, bandName};
  for (const [pfx, abbr] of BAND_ABBREV_PREFIX) {
    if (bandName.startsWith(pfx)) return {label: abbr, rejected, bandName};
  }
  return {label: bandName.slice(0,4) || "?", rejected, bandName};
}

async function loadConfig(){
  const r = await fetch("/api/config");
  CONFIG = await r.json();
}
function setupTunerCard(tid, cfg){
  const card = document.createElement("div");
  card.className = "tuner";
  card.innerHTML = `
    <h2>
      <span>${tid}</span>
      <span class="ctrl">
        <button class="mode-btn active" data-mode="composite">Composite</button>
        <button class="mode-btn" data-mode="live_if">Live IF</button>
      </span>
    </h2>
    <div class="band">${(cfg.band_start_hz/1e6).toFixed(0)} – ${(cfg.band_end_hz/1e6).toFixed(0)} MHz</div>
    <div class="summary" data-summary>—</div>
    <canvas class="spectrum" data-spectrum width="800" height="100"></canvas>
    <canvas class="waterfall" data-waterfall width="800" height="160"></canvas>
    <table data-strongest><thead><tr><th>freq</th><th>SNR</th><th>pwr</th><th>hits</th><th>mode</th><th>conf</th><th>licensed&nbsp;to</th><th>age</th></tr></thead><tbody></tbody></table>
  `;
  document.getElementById("tuners").appendChild(card);
  const t = {
    id: tid, cfg: cfg, mode: "composite",
    spectrumCanvas: card.querySelector("[data-spectrum]"),
    waterfallCanvas: card.querySelector("[data-waterfall]"),
    summary: card.querySelector("[data-summary]"),
    strongestTbody: card.querySelector("[data-strongest] tbody"),
    waterfall: [], source: null, lastSpectrumTs: 0,
  };
  tuners[tid] = t;
  card.querySelectorAll(".mode-btn").forEach(b => {
    b.addEventListener("click", () => {
      card.querySelectorAll(".mode-btn").forEach(bb => bb.classList.toggle("active", bb===b));
      switchMode(t, b.dataset.mode);
    });
  });
  openSSE(t);
}
function openSSE(t){
  if(t.source) t.source.close();
  t.waterfall = [];
  const url = `/api/spectrum/${t.id}?mode=${t.mode}`;
  const src = new EventSource(url);
  t.source = src;
  src.onmessage = (ev) => {
    try { const data = JSON.parse(ev.data); onSpectrumFrame(t, data); } catch (e) { }
  };
}
function switchMode(t, mode){ t.mode = mode; t.waterfall = []; openSSE(t); }
function onSpectrumFrame(t, data){
  drawSpectrum(t, data);
  t.waterfall.push(data.bins_dbfs.slice());
  while (t.waterfall.length > WATERFALL_ROWS) t.waterfall.shift();
  drawWaterfall(t);
  t.lastSpectrumTs = data.ts;
}
function drawSpectrum(t, data){
  const c = t.spectrumCanvas;
  const dpr = window.devicePixelRatio || 1;
  if (c.width !== c.clientWidth*dpr) { c.width = c.clientWidth*dpr; c.height = c.clientHeight*dpr; }
  const W = c.width, H = c.height;
  const ctx = c.getContext("2d");
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle = "#000"; ctx.fillRect(0,0,W,H);
  const bins = data.bins_dbfs;
  const N = bins.length;
  ctx.strokeStyle = "#7fc7ff"; ctx.lineWidth = 1.0 * dpr;
  ctx.beginPath();
  for(let i=0;i<N;i++){
    const x = i/(N-1) * W;
    const db = bins[i];
    let y = (1 - (db - SPECTRUM_DB_MIN)/(SPECTRUM_DB_MAX - SPECTRUM_DB_MIN)) * H;
    if(y<0) y=0; if(y>H) y=H;
    if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  }
  ctx.stroke();
  if(data.noise_floor != null){
    const nfy = (1 - (data.noise_floor - SPECTRUM_DB_MIN)/(SPECTRUM_DB_MAX - SPECTRUM_DB_MIN)) * H;
    ctx.strokeStyle = "rgba(255,200,80,0.35)";
    ctx.setLineDash([4*dpr,4*dpr]);
    ctx.beginPath(); ctx.moveTo(0,nfy); ctx.lineTo(W,nfy); ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.fillStyle = "#888"; ctx.font = `${17*dpr}px ui-monospace, monospace`;
  let label;
  if(data.mode === "composite"){
    label = `${(data.band_min_hz/1e6).toFixed(0)} – ${(data.band_max_hz/1e6).toFixed(0)} MHz | sweep ${data.current_sweep_pos+1}/${data.total_steps}`;
  } else {
    const f0 = data.center_hz - data.sample_rate_hz/2, f1 = data.center_hz + data.sample_rate_hz/2;
    label = `IF @ ${(data.center_hz/1e6).toFixed(3)} MHz | ${(f0/1e6).toFixed(2)}–${(f1/1e6).toFixed(2)}`;
  }
  ctx.fillText(label, 10*dpr, 22*dpr);
  if(data.mode === "composite" && data.current_center_hz){
    const fp = (data.current_center_hz - data.band_min_hz) / (data.band_max_hz - data.band_min_hz);
    const x = fp * W;
    ctx.strokeStyle = "rgba(255,80,80,0.5)";
    ctx.lineWidth = 1*dpr;
    ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x,H); ctx.stroke();
  }
}
function drawWaterfall(t){
  const c = t.waterfallCanvas;
  const dpr = window.devicePixelRatio || 1;
  if (c.width !== c.clientWidth*dpr) { c.width = c.clientWidth*dpr; c.height = c.clientHeight*dpr; }
  const W = c.width, H = c.height;
  const ctx = c.getContext("2d");
  if(t.waterfall.length === 0){ ctx.fillStyle="#000"; ctx.fillRect(0,0,W,H); return; }
  const N_BINS = t.waterfall[0].length;
  const N_ROWS = t.waterfall.length;
  const img = ctx.createImageData(W, H);
  const data = img.data;
  for(let yPix=0; yPix<H; yPix++){
    const row_idx = N_ROWS - 1 - Math.floor(yPix * N_ROWS / H);
    if(row_idx < 0 || row_idx >= N_ROWS) continue;
    const row = t.waterfall[row_idx];
    for(let xPix=0; xPix<W; xPix++){
      const bin_idx = Math.floor(xPix * N_BINS / W);
      const db = row[bin_idx];
      const [r,g,b] = dbToColor(db);
      const off = (yPix*W + xPix)*4;
      data[off]=r; data[off+1]=g; data[off+2]=b; data[off+3]=255;
    }
  }
  ctx.putImageData(img, 0, 0);
}
// --- Phase 4 polish: filter + favorites state -------------------------------
const FILTER_STATE = (() => {
  // Hydrate from localStorage so filters persist across refreshes.
  let s = {};
  try { s = JSON.parse(localStorage.getItem("disco-filter") || "{}"); } catch (e) {}
  return {
    mode: s.mode || "",
    snr: typeof s.snr === "number" ? s.snr : 0,
    licensee: s.licensee || "",
    window_s: s.window_s || 120,
  };
})();
function persistFilter(){
  try { localStorage.setItem("disco-filter", JSON.stringify(FILTER_STATE)); } catch (e) {}
}
const FAV_FREQS = new Set();   // freqs (rounded Hz) currently favorited
const HIDDEN_FREQS = new Set();  // freqs (rounded Hz) the user has hidden
let SHOW_HIDDEN = false;          // temporary "reveal" toggle (not persisted)

function rowMatchesFilter(r){
  // Hidden rows drop out unless the user has flipped the "show hidden" toggle.
  if (!SHOW_HIDDEN) {
    const fid = Math.round(r.freq_hz);
    if (HIDDEN_FREQS.has(fid)) return false;
  }
  if (FILTER_STATE.mode) {
    const want = FILTER_STATE.mode;
    const got = r.modulation_class || (r.protocol_tag === "unclassified" ? "unclassified" : "");
    if (want === "unclassified") {
      if (got && got !== "unclassified") return false;
    } else if (got !== want) {
      return false;
    }
  }
  if (FILTER_STATE.snr > 0 && (r.max_snr || 0) < FILTER_STATE.snr) return false;
  if (FILTER_STATE.licensee) {
    const needle = FILTER_STATE.licensee.toLowerCase();
    const ent = (r.uls_entity_name || "").toLowerCase();
    const cs = (r.uls_callsign || "").toLowerCase();
    if (!ent.includes(needle) && !cs.includes(needle)) return false;
  }
  return true;
}

async function refreshTables(){
  const win = FILTER_STATE.window_s;
  const [strong, summ] = await Promise.all([
    fetch(`/api/strongest?since_seconds=${win}&per_tuner=8&bin_khz=25`).then(r=>r.json()),
    fetch(`/api/summary?since_seconds=${win}`).then(r=>r.json()),
  ]);
  for(const tid of CONFIG.tuner_order){
    const t = tuners[tid]; if(!t) continue;
    const s = summ[tid] || {count:0,max_snr:null,last_seen:null,classified:0};
    const winLabel = win >= 60 ? (win >= 60 ? `${win}s` : `${win}s`) : `${win}s`;
    let sumStr = `${win}s: ${s.count} det`;
    if(s.classified) sumStr += `, ${s.classified} classified`;
    if(s.max_snr!=null) sumStr += `, peak ${s.max_snr.toFixed(1)} dB`;
    if(s.last_seen) sumStr += `, last ${(Math.round(Date.now()/1000-s.last_seen))}s`;
    t.summary.textContent = sumStr;
    const buckets = (strong.buckets && strong.buckets[tid]) || [];
    const filtered = buckets.filter(rowMatchesFilter);
    const tbody = t.strongestTbody;
    tbody.innerHTML = "";
    if(buckets.length===0){
      tbody.innerHTML = `<tr><td colspan="8" class="empty">no detections in last ${win}s</td></tr>`;
    } else if (filtered.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" class="empty">${buckets.length} detections hidden by filters</td></tr>`;
    } else {
      for(const r of filtered){
        const age = Math.round(Date.now()/1000 - r.last_seen);
        const cls = snrClass(r.max_snr);
        const modCls = modConfClass(r.modulation_confidence);
        // Tier 4: full mode tag (desktop) + compact band-badge (phone). Both
        // spans always render; CSS media query swaps visibility — no JS resize
        // listener needed and the table-refresh cadence stays untouched.
        const fullText = r.protocol_tag || r.modulation_class || "—";
        const ab = bandAbbrev(r.protocol_tag, r.modulation_class);
        const compactClassName = r.modulation_class
          || (r.protocol_tag === "unclassified" ? "unclassified" : "—");
        const badgeCls = ab.rejected ? "mode-badge band-rejected" : "mode-badge band-allowed";
        let modLabel = `<span class="mode-full">${fullText}</span>`
          + `<span class="mode-compact"><span class="${badgeCls}" title="${ab.bandName || ""}">[${ab.label}]</span>${compactClassName}</span>`;
        if (r.interpretation) modLabel = modLabel + ` <button class="details-btn" type="button">details</button>`;
        const cleanClass = (r.modulation_class || "").toUpperCase();
        const freqId = Math.round(r.freq_hz);
        const isListening = ACTIVE_LISTEN_FREQS.has(freqId);
        if (LISTEN_SUPPORTED.has(cleanClass)) {
          const lbl = isListening ? "Stop" : "🎧 Listen";
          const cls = isListening ? "listen-btn is-active" : "listen-btn";
          modLabel = modLabel + ` <button class="${cls}" type="button">${lbl}</button>`;
        }
        const modConf = r.modulation_confidence != null ? r.modulation_confidence.toFixed(2) : "—";
        const tr = document.createElement("tr");
        // Tier 4: pulse the source row while audio is wired to it.
        if (isListening) tr.classList.add("is-listening");
        // Compose a single "licensed to" cell from the ULS columns. We show
        // entity_name truncated + callsign in a smaller dim font, with a
        // tooltip carrying emission designator + distance so the user can
        // hover for technical detail without bloating the table.
        let ulsCell = "—";
        let ulsTitle = "";
        if (r.uls_entity_name || r.uls_callsign) {
          const ent = (r.uls_entity_name || "").trim();
          const cs = (r.uls_callsign || "").trim();
          const entShort = ent.length > 22 ? ent.slice(0, 21) + "…" : ent;
          ulsCell = entShort
            ? `${entShort}${cs ? ` <span class="uls-cs">${cs}</span>` : ""}`
            : cs;
          const bits = [];
          if (ent && ent !== entShort) bits.push(ent);
          if (cs) bits.push("callsign " + cs);
          if (r.uls_emission_designator) bits.push("emission " + r.uls_emission_designator);
          if (r.uls_station_class) bits.push("class " + r.uls_station_class);
          if (r.uls_distance_km != null) bits.push(r.uls_distance_km.toFixed(1) + " km");
          ulsTitle = bits.join(" • ");
        }
        // Row tooltip carries ULS technical detail. Interpretation goes into
        // the click-popup below — different information densities (hover =
        // quick aside, click = full prose).
        tr.title = ulsTitle;
        const isFav = FAV_FREQS.has(freqId);
        const isHidden = HIDDEN_FREQS.has(freqId);
        const starHtml = `<button class="star-btn ${isFav ? "is-fav" : ""}" type="button" title="${isFav ? "Remove from favorites" : "Add to favorites"}">${isFav ? "★" : "☆"}</button>`;
        const hideHtml = `<button class="hide-btn ${isHidden ? "is-hidden" : ""}" type="button" title="${isHidden ? "Unhide this row" : "Hide this row"}">👁</button>`;
        tr.innerHTML = `<td>${starHtml}${hideHtml}${(r.freq_hz/1e6).toFixed(4)}</td>`+
          `<td class="${cls}">${r.max_snr.toFixed(1)}</td>`+
          `<td>${r.max_power.toFixed(1)}</td>`+
          `<td>${r.hits}</td>`+
          `<td class="${modCls}">${modLabel}</td>`+
          `<td>${modConf}</td>`+
          `<td class="uls">${ulsCell}</td>`+
          `<td>${age}s</td>`;
        // Stash interpretation on the freshly-rendered button so the popup
        // can read it back without HTML-escaping prose into an attribute.
        // tbody re-renders every 2s, so we re-attach per row each refresh.
        if (r.interpretation) {
          const btn = tr.querySelector(".details-btn");
          if (btn) btn._detail = r.interpretation;
        }
        // Stash decode-call args on the listen button (same reason — avoid
        // attribute-encoding floats and modulation strings into onclick).
        const listenBtn = tr.querySelector(".listen-btn");
        if (listenBtn) {
          listenBtn._freq_hz = r.freq_hz;
          listenBtn._bandwidth_hz = r.bandwidth_hz;
          listenBtn._modulation_class = cleanClass;
          listenBtn._protocol_tag = r.protocol_tag || "";
          listenBtn._is_active = ACTIVE_LISTEN_FREQS.has(freqId);
        }
        // Stash row context on the star button so favorites/add can carry the
        // current modulation + ULS info into the new row.
        const starBtn = tr.querySelector(".star-btn");
        if (starBtn) {
          starBtn._freq_hz = r.freq_hz;
          starBtn._modulation_class = cleanClass;
          starBtn._protocol_tag = r.protocol_tag || "";
          starBtn._uls_callsign = r.uls_callsign || "";
          starBtn._uls_entity_name = r.uls_entity_name || "";
          starBtn._is_fav = isFav;
        }
        const hideBtn = tr.querySelector(".hide-btn");
        if (hideBtn) {
          hideBtn._freq_hz = r.freq_hz;
          hideBtn._label = (r.uls_entity_name || r.uls_callsign || cleanClass || "").slice(0, 60);
          hideBtn._is_hidden = isHidden;
        }
        tbody.appendChild(tr);
      }
    }
  }
  document.getElementById("status").textContent = `updated ${new Date().toLocaleTimeString()}`;
}
// --- service control (Start / Stop) -----------------------------------------
// Stops the 4 sweep services + classifier + interpreter when the user wants
// to hand the RSPduos back to SB3. Dashboard itself is intentionally NOT in
// the controlled set — if it stopped, the user couldn't restart from here.
async function refreshSvcStatus(){
  const elStatus = document.getElementById("svc-status");
  const elStart = document.getElementById("svc-start");
  const elStop = document.getElementById("svc-stop");
  if (!elStatus || !elStart || !elStop) return;
  let resp;
  try {
    resp = await fetch("/api/services/status").then(r => r.json());
  } catch (e) {
    elStatus.textContent = "status unreachable";
    elStatus.className = "svc-status stopped";
    return resp;
  }
  const units = resp.units || {};
  const states = Object.values(units);
  const total = states.length;
  const active = states.filter(s => s === "active").length;
  let label, klass;
  if (total === 0) { label = "no units"; klass = "svc-status"; }
  else if (active === total) { label = `running (${active}/${total})`; klass = "svc-status running"; }
  else if (active === 0) { label = "stopped"; klass = "svc-status stopped"; }
  else { label = `partial (${active}/${total})`; klass = "svc-status partial"; }
  elStatus.textContent = label;
  elStatus.className = klass;
  elStatus.title = Object.entries(units).map(([u,s]) => `${u} ${s}`).join("\\n");
  elStart.disabled = (active === total);
  elStop.disabled = (active === 0);
  return resp;
}
async function svcAction(action){
  const elStart = document.getElementById("svc-start");
  const elStop = document.getElementById("svc-stop");
  elStart.disabled = true;
  elStop.disabled = true;
  const elStatus = document.getElementById("svc-status");
  elStatus.textContent = action === "stop" ? "stopping…" : "starting…";
  elStatus.className = "svc-status partial";
  try {
    await fetch(`/api/services/${action}`, { method: "POST" });
  } catch (e) { /* fall through to refresh, which will show the real state */ }
  // give systemd a beat to settle
  setTimeout(refreshSvcStatus, 1500);
}

// Mode bar: at-home Disco<->SB3 handoff. Calls disco-svc-ctl mode-off /
// mode-on / mode-status. Classifier is intentionally NOT toggled here —
// the wrapper preserves it across the handoff so the trained model and
// caches stay warm.
const SWEEP_UNIT_NAMES = [
  "disco-sweep@A-T1.service",
  "disco-sweep@A-T2.service",
  "disco-sweep@B-T1.service",
  "disco-sweep@B-T2.service",
];
const SB3_PRIMARY_UNIT = "scanner-digital-op25.service";
// Stuck-recovery: how long the mode bar can sit in "transitioning" before we
// re-enable both buttons so the user can retry from the UI instead of being
// forced to ssh in. The legitimate transition window is ~13–18s; 25s is a
// generous "this is wedged" threshold.
const STUCK_AFTER_MS = 25000;
const MODE_DETAIL_DEFAULT = "at-home handoff — classifier stays warm across the toggle";
const MODE_DETAIL_STUCK = "stuck — both buttons re-enabled; click to retry, or use CLI";
let _transitioningSinceMs = null;

function deriveMode(units){
  const sweepStates = SWEEP_UNIT_NAMES.map(u => units[u] || "unknown");
  const op25 = units[SB3_PRIMARY_UNIT] || "unknown";
  const allSweepActive = sweepStates.every(s => s === "active");
  const allSweepInactive = sweepStates.every(s => s === "inactive");
  if (allSweepActive && op25 === "inactive") return "disco";
  if (allSweepInactive && op25 === "active") return "handoff";
  return "transitioning";
}

async function refreshModeStatus(){
  const elStatus = document.getElementById("mode-status");
  const elOff = document.getElementById("mode-off-btn");
  const elOn = document.getElementById("mode-on-btn");
  const elDetail = document.getElementById("mode-detail");
  if (!elStatus || !elOff || !elOn) return;
  let resp;
  try {
    resp = await fetch("/api/services/mode-status").then(r => r.json());
  } catch (e) {
    elStatus.textContent = "mode unreachable";
    elStatus.className = "svc-status stopped";
    return;
  }
  const units = resp.units || {};
  const mode = deriveMode(units);
  let label, klass;
  if (mode === "disco") { label = "mode: disco"; klass = "svc-status disco"; }
  else if (mode === "handoff") { label = "mode: handoff (SB3)"; klass = "svc-status handoff"; }
  else { label = "mode: transitioning"; klass = "svc-status partial"; }
  elStatus.textContent = label;
  elStatus.className = klass;
  elStatus.title = Object.entries(units).map(([u,s]) => `${u} ${s}`).join("\\n");

  // Stuck-recovery: while mode === "transitioning", keep both buttons
  // disabled until STUCK_AFTER_MS elapses since the first such observation,
  // then re-enable both so the user has a UI-side retry path. Reset the
  // timer the instant we leave "transitioning".
  if (mode === "transitioning") {
    if (_transitioningSinceMs === null) _transitioningSinceMs = Date.now();
    const stuck = (Date.now() - _transitioningSinceMs) >= STUCK_AFTER_MS;
    elOff.disabled = !stuck;
    elOn.disabled = !stuck;
    if (elDetail) elDetail.textContent = stuck ? MODE_DETAIL_STUCK : MODE_DETAIL_DEFAULT;
  } else {
    _transitioningSinceMs = null;
    elOff.disabled = (mode !== "disco");
    elOn.disabled = (mode !== "handoff");
    if (elDetail) elDetail.textContent = MODE_DETAIL_DEFAULT;
  }
}

async function modeAction(action){
  // action is "mode-off" or "mode-on"
  // Reset the stuck timer so a fresh click gets a fresh ~25s window before
  // the recovery affordance re-fires — otherwise a click made while stuck
  // would briefly disable the buttons and re-enable them on the next poll
  // with no visible feedback.
  _transitioningSinceMs = null;
  const elOff = document.getElementById("mode-off-btn");
  const elOn = document.getElementById("mode-on-btn");
  elOff.disabled = true;
  elOn.disabled = true;
  const elStatus = document.getElementById("mode-status");
  elStatus.textContent = action === "mode-off" ? "handing off to SB3…" : "reclaiming radios…";
  elStatus.className = "svc-status partial";
  try {
    await fetch(`/api/services/${action}`, { method: "POST" });
  } catch (e) { /* fall through; refresh will show the real state */ }
  // mode-off / mode-on take ~10-15s end-to-end (drain wait + service start),
  // so wait longer than svcAction's 1.5s. Refresh once after the toggle
  // should be done, then again 5s later in case the first refresh caught
  // mid-transition.
  setTimeout(refreshModeStatus, 13000);
  setTimeout(refreshModeStatus, 18000);
}
// --- Details popup -----------------------------------------------------------
// Click a "details" button → popup positions next to it with the full Phase 2.5
// interpretation prose. Click anywhere else closes it. Tables refresh every 2s
// so we delegate via a single document listener instead of per-button binding.
function showDetailPopup(target){
  const popup = document.getElementById("detail-popup");
  if (!popup) return;
  popup.textContent = target._detail || "";
  popup.style.display = "block";
  popup.setAttribute("aria-hidden", "false");
  const rect = target.getBoundingClientRect();
  const top = rect.bottom + window.scrollY + 6;
  let left = rect.left + window.scrollX;
  popup.style.top = top + "px";
  popup.style.left = left + "px";
  const popRect = popup.getBoundingClientRect();
  if (popRect.right > window.innerWidth - 8) {
    left = Math.max(8, window.innerWidth - popRect.width - 8) + window.scrollX;
    popup.style.left = left + "px";
  }
}
function hideDetailPopup(){
  const popup = document.getElementById("detail-popup");
  if (!popup) return;
  popup.style.display = "none";
  popup.setAttribute("aria-hidden", "true");
}
document.addEventListener("click", (e) => {
  const popup = document.getElementById("detail-popup");
  if (e.target.classList && e.target.classList.contains("details-btn")) {
    showDetailPopup(e.target);
    e.stopPropagation();
    return;
  }
  if (popup && popup.style.display === "block" && !popup.contains(e.target)) {
    hideDetailPopup();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") hideDetailPopup();
});

// --- Phase 5 listen wiring --------------------------------------------------
async function decodeListen(btn){
  try {
    const resp = await fetch("/api/decode/listen", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        freq_hz: btn._freq_hz,
        bandwidth_hz: btn._bandwidth_hz,
        modulation_class: btn._modulation_class,
        protocol_tag: btn._protocol_tag,
        user_id: "will",
      }),
    });
    const d = await resp.json();
    if (d.status !== "wired") {
      alert(`Could not wire ${(btn._freq_hz/1e6).toFixed(4)} MHz: ${d.status}\\n${d.detail}`);
    }
  } catch (e) {
    alert("decode/listen request failed: " + e);
  }
  refreshActiveListens();
}
async function decodeStop(freq_hz){
  try {
    await fetch("/api/decode/stop", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({freq_hz: freq_hz, user_id: "will"}),
    });
  } catch (e) {
    alert("decode/stop request failed: " + e);
  }
  refreshActiveListens();
}
async function decodeMute(freq_hz, muted){
  try {
    await fetch("/api/decode/mute", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({freq_hz: freq_hz, muted: !!muted, user_id: "will"}),
    });
  } catch (e) {
    alert("decode/mute request failed: " + e);
  }
  refreshActiveListens();
}
async function refreshActiveListens(){
  let resp;
  try { resp = await fetch("/api/decode/active").then(r => r.json()); }
  catch (e) { return; }
  const items = resp.items || [];
  ACTIVE_LISTEN_FREQS.clear();
  for (const it of items) ACTIVE_LISTEN_FREQS.add(Math.round(it.freq_hz));
  const elEmpty = document.getElementById("listen-empty");
  const elList = document.getElementById("listen-list");
  const elStream = document.getElementById("listen-stream");
  const elAudio = document.getElementById("disco-audio-player");
  if (!elEmpty || !elList || !elStream) return;
  if (items.length === 0) {
    elEmpty.style.display = "";
    elList.innerHTML = "";
    elStream.innerHTML = "";
    if (elAudio) {
      elAudio.classList.remove("is-active");
      try { elAudio.pause(); } catch (e) {}
      elAudio.removeAttribute("src");
      elAudio.load();
    }
    return;
  }
  elEmpty.style.display = "none";
  elList.innerHTML = items.map(it => {
    const mhz = (it.freq_hz/1e6).toFixed(4);
    const tag = it.modulation || it.label || "";
    const muted = !!it.muted;
    const pillClass = muted ? "listen-pill is-muted" : "listen-pill";
    const muteIcon = muted ? "🔇" : "🔊";
    const muteTitle = muted ? "Unmute" : "Mute";
    return `<span class="${pillClass}"><span class="listen-freq">${mhz} MHz</span> <span style="opacity:.7">${tag}</span>`
      + `<button class="pill-mute" data-mute-freq="${it.freq_hz}" data-muted="${muted ? 1 : 0}" title="${muteTitle}">${muteIcon}</button>`
      + `<button data-stop-freq="${it.freq_hz}" title="Stop">×</button></span>`;
  }).join("");
  // Wire the embedded audio element to the disco mount the moment we have at
  // least one active listen. We don't auto-play (browser autoplay policies);
  // user clicks play once.
  if (elAudio && resp.stream_url) {
    const wantSrc = resp.stream_url;
    if (elAudio.getAttribute("src") !== wantSrc) {
      elAudio.setAttribute("src", wantSrc);
      // Only call .load() when the src actually changed to avoid restarting
      // a stream the user is currently listening to.
      try { elAudio.load(); } catch (e) {}
    }
    elAudio.classList.add("is-active");
  }
  elStream.innerHTML = resp.stream_url
    ? `→ <a href="${resp.stream_url}" target="_blank" rel="noopener">${resp.stream_url}</a>`
    : "";
}
document.addEventListener("click", (e) => {
  if (e.target.classList && e.target.classList.contains("listen-btn")) {
    if (e.target._is_active) {
      decodeStop(e.target._freq_hz);
    } else {
      decodeListen(e.target);
    }
    e.stopPropagation();
    return;
  }
  if (e.target.classList && e.target.classList.contains("star-btn")) {
    if (e.target._is_fav) favoriteRemove(e.target._freq_hz);
    else favoriteAdd(e.target);
    e.stopPropagation();
    return;
  }
  if (e.target.classList && e.target.classList.contains("hide-btn")) {
    if (e.target._is_hidden) hideRemove(e.target._freq_hz);
    else hideAdd(e.target);
    e.stopPropagation();
    return;
  }
  // listen pill mute toggle
  const muteFreq = e.target && e.target.getAttribute && e.target.getAttribute("data-mute-freq");
  if (muteFreq) {
    const wasMuted = e.target.getAttribute("data-muted") === "1";
    decodeMute(parseFloat(muteFreq), !wasMuted);
    e.stopPropagation();
    return;
  }
  // pill stop button (either listen pills or favorite pills)
  const stopFreq = e.target && e.target.getAttribute && e.target.getAttribute("data-stop-freq");
  if (stopFreq) {
    decodeStop(parseFloat(stopFreq));
    e.stopPropagation();
    return;
  }
  const unfavFreq = e.target && e.target.getAttribute && e.target.getAttribute("data-unfav-freq");
  if (unfavFreq) {
    favoriteRemove(parseFloat(unfavFreq));
    e.stopPropagation();
  }
});

// --- Phase 4 polish: favorites ----------------------------------------------
async function favoriteAdd(btn){
  try {
    await fetch("/api/favorites/add", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        freq_hz: btn._freq_hz,
        modulation_class: btn._modulation_class,
        protocol_tag: btn._protocol_tag,
        uls_callsign: btn._uls_callsign,
        uls_entity_name: btn._uls_entity_name,
      }),
    });
  } catch (e) { console.warn("favorite add failed", e); }
  refreshFavorites();
  refreshTables();
}
async function favoriteRemove(freq_hz){
  try {
    await fetch("/api/favorites/remove", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({freq_hz: freq_hz}),
    });
  } catch (e) { console.warn("favorite remove failed", e); }
  refreshFavorites();
  refreshTables();
}
// --- Phase 4 polish: hide rows ---------------------------------------------
async function hideAdd(btn){
  try {
    await fetch("/api/hidden/add", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({freq_hz: btn._freq_hz, label: btn._label || null}),
    });
  } catch (e) { console.warn("hide add failed", e); }
  refreshHidden();
  refreshTables();
}
async function hideRemove(freq_hz){
  try {
    await fetch("/api/hidden/remove", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({freq_hz: freq_hz}),
    });
  } catch (e) { console.warn("hide remove failed", e); }
  refreshHidden();
  refreshTables();
}
async function hideClearAll(){
  try {
    await fetch("/api/hidden/clear", {method: "POST"});
  } catch (e) { console.warn("hide clear failed", e); }
  refreshHidden();
  refreshTables();
}
async function refreshHidden(){
  let resp;
  try { resp = await fetch("/api/hidden").then(r=>r.json()); }
  catch (e) { return; }
  const items = resp.items || [];
  HIDDEN_FREQS.clear();
  for (const it of items) HIDDEN_FREQS.add(Math.round(it.freq_hz));
  const elCount = document.getElementById("hidden-count");
  const elCtrl = document.getElementById("hidden-control");
  const elToggle = document.getElementById("hidden-toggle");
  if (elCount) elCount.textContent = `${items.length} hidden`;
  if (elCtrl) elCtrl.classList.toggle("has-items", items.length > 0);
  if (elToggle) elToggle.textContent = SHOW_HIDDEN ? "re-hide" : "show";
}
function toggleShowHidden(){
  SHOW_HIDDEN = !SHOW_HIDDEN;
  const elToggle = document.getElementById("hidden-toggle");
  if (elToggle) elToggle.textContent = SHOW_HIDDEN ? "re-hide" : "show";
  refreshTables();
}

async function refreshFavorites(){
  let resp;
  try { resp = await fetch("/api/favorites").then(r=>r.json()); }
  catch (e) { return; }
  const items = resp.items || [];
  FAV_FREQS.clear();
  for (const it of items) FAV_FREQS.add(Math.round(it.freq_hz));
  const elEmpty = document.getElementById("fav-empty");
  const elList = document.getElementById("fav-list");
  if (!elEmpty || !elList) return;
  if (items.length === 0) {
    elEmpty.style.display = "";
    elList.innerHTML = "";
    updateFavToggleCount(0);
    return;
  }
  elEmpty.style.display = "none";
  elList.innerHTML = items.map(it => {
    const mhz = (it.freq_hz/1e6).toFixed(4);
    const isActive = (it.recent_hits || 0) > 0;
    const meta = isActive
      ? `${it.recent_hits} hit${it.recent_hits === 1 ? "" : "s"}`
      : "quiet";
    const owner = it.uls_entity_name || it.uls_callsign || it.modulation_class || "";
    const ownerSpan = owner ? `<span class="fav-meta">${owner}</span>` : "";
    return `<span class="fav-pill ${isActive ? "active" : ""}">${mhz} MHz ${ownerSpan} <span class="fav-meta">(${meta})</span><button data-unfav-freq="${it.freq_hz}" title="Remove">×</button></span>`;
  }).join("");
  updateFavToggleCount(items.length);
}

// --- Phase 4 polish: filter wiring ------------------------------------------
function applyFilterUiToState(){
  FILTER_STATE.mode = document.getElementById("filter-mode").value || "";
  FILTER_STATE.snr = parseFloat(document.getElementById("filter-snr").value || "0") || 0;
  FILTER_STATE.licensee = document.getElementById("filter-licensee").value || "";
  FILTER_STATE.window_s = parseInt(document.getElementById("filter-window").value || "120", 10) || 120;
  persistFilter();
  updateFilterToggleCount();
  // Re-render immediately so the user sees the effect without waiting for the 2s tick.
  refreshTables();
}
function hydrateFilterUi(){
  document.getElementById("filter-mode").value = FILTER_STATE.mode;
  document.getElementById("filter-snr").value = FILTER_STATE.snr;
  document.getElementById("filter-licensee").value = FILTER_STATE.licensee;
  document.getElementById("filter-window").value = String(FILTER_STATE.window_s);
}
function clearFilters(){
  FILTER_STATE.mode = "";
  FILTER_STATE.snr = 0;
  FILTER_STATE.licensee = "";
  FILTER_STATE.window_s = 120;
  hydrateFilterUi();
  persistFilter();
  updateFilterToggleCount();
  refreshTables();
}

// --- Tier 4: gear-drawer + collapsible filter/favorites sections -------------
// On phone, the controls drawer is collapsed by default. Tapping the ⚙ gear in
// the sticky header toggles the .is-open class — CSS media query handles the
// actual visibility. On desktop the gear is hidden and the drawer is always
// open via the >1024px media query (rules force display:block).
function toggleDrawer(){
  const drawer = document.getElementById("controls-drawer");
  const gear = document.getElementById("gear-btn");
  if (!drawer || !gear) return;
  const open = !drawer.classList.contains("is-open");
  drawer.classList.toggle("is-open", open);
  gear.classList.toggle("is-open", open);
}
// Generic collapsible — wires a .collapsible-toggle button to a target panel.
function bindCollapsible(toggleId, panelId, defaultOpen){
  const toggle = document.getElementById(toggleId);
  const panel = document.getElementById(panelId);
  if (!toggle || !panel) return;
  // Mirror state to the button so the chevron flips correctly.
  const setOpen = (open) => {
    panel.classList.toggle("is-open", open);
    toggle.classList.toggle("is-open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  };
  setOpen(!!defaultOpen);
  toggle.addEventListener("click", () => {
    setOpen(!panel.classList.contains("is-open"));
  });
}
// Filter toggle "count" badge — surfaces how many filters are active so the
// collapsed header still tells the user the table is being narrowed.
function activeFilterCount(){
  let n = 0;
  if (FILTER_STATE.mode) n++;
  if (FILTER_STATE.snr > 0) n++;
  if (FILTER_STATE.licensee) n++;
  if (FILTER_STATE.window_s !== 120) n++;
  return n;
}
function updateFilterToggleCount(){
  const el = document.getElementById("filter-toggle-count");
  const tog = document.getElementById("filter-toggle");
  if (!el || !tog) return;
  const n = activeFilterCount();
  el.textContent = n === 0 ? "(none active)" : `(${n} active)`;
  tog.classList.toggle("has-active", n > 0);
}
function updateFavToggleCount(n){
  const el = document.getElementById("fav-toggle-count");
  const tog = document.getElementById("fav-toggle");
  if (!el || !tog) return;
  el.textContent = `(${n || 0})`;
  tog.classList.toggle("has-active", (n || 0) > 0);
}

async function init(){
  await loadConfig();
  for(const tid of CONFIG.tuner_order){
    setupTunerCard(tid, CONFIG.tuners[tid]);
  }
  hydrateFilterUi();
  document.getElementById("filter-mode").addEventListener("change", applyFilterUiToState);
  document.getElementById("filter-snr").addEventListener("change", applyFilterUiToState);
  document.getElementById("filter-licensee").addEventListener("input", applyFilterUiToState);
  document.getElementById("filter-window").addEventListener("change", applyFilterUiToState);
  document.getElementById("filter-clear").addEventListener("click", clearFilters);
  document.getElementById("hidden-toggle").addEventListener("click", toggleShowHidden);
  document.getElementById("hidden-clear").addEventListener("click", hideClearAll);
  // Tier 4: gear drawer + filter/favorites collapsibles. Filters open by default
  // when any are active so the user can see what's narrowing the view.
  document.getElementById("gear-btn").addEventListener("click", toggleDrawer);
  bindCollapsible("fav-toggle", "fav-bar", false);
  bindCollapsible("filter-toggle", "filter-bar", activeFilterCount() > 0);
  updateFilterToggleCount();
  refreshTables();
  setInterval(refreshTables, 2000);
  document.getElementById("svc-start").addEventListener("click", () => svcAction("start"));
  document.getElementById("svc-stop").addEventListener("click", () => svcAction("stop"));
  refreshSvcStatus();
  setInterval(refreshSvcStatus, 5000);
  document.getElementById("mode-off-btn").addEventListener("click", () => modeAction("mode-off"));
  document.getElementById("mode-on-btn").addEventListener("click", () => modeAction("mode-on"));
  refreshModeStatus();
  setInterval(refreshModeStatus, 5000);
  refreshActiveListens();
  setInterval(refreshActiveListens, 4000);
  refreshFavorites();
  setInterval(refreshFavorites, 5000);
  refreshHidden();
  setInterval(refreshHidden, 7000);
}
init();
</script></body></html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(HTML)


def main():
    uvicorn.run(app, host=CFG["dashboard"]["host"], port=CFG["dashboard"]["port"], log_level="warning")


if __name__ == "__main__":
    main()
