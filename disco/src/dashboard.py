"""Disco dashboard — detections (with mod class) + live spectrum + waterfall. Port 8092."""
import asyncio
import json
import os
import sqlite3
import time
from typing import Optional

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
import uvicorn

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


@app.get("/api/detections")
def api_detections(since_seconds: float = 60.0, limit: int = 1000):
    cutoff = time.time() - since_seconds
    c = _conn()
    rows = c.execute(
        "SELECT ts, tuner_id, freq_hz, bandwidth_hz, power_dbfs, snr_db, "
        "modulation_class, modulation_confidence, protocol_tag "
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
            "  ORDER BY d2.modulation_confidence DESC LIMIT 1 ) as protocol_tag, ( SELECT interpretation FROM detections d2   WHERE d2.tuner_id = detections.tuner_id     AND CAST(d2.freq_hz / ? AS INTEGER) = CAST(detections.freq_hz / ? AS INTEGER)     AND d2.interpretation IS NOT NULL     AND d2.ts >= ?   ORDER BY d2.interpreted_ts DESC LIMIT 1 ) as interpretation "
            "FROM detections WHERE ts >= ? AND tuner_id = ? "
            "GROUP BY CAST(freq_hz / ? AS INTEGER) "
            "ORDER BY max_snr DESC LIMIT ?",
            (bin_hz, bin_hz, cutoff,
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
  --fs-h1:26px;
  --fs-status:15px;
  --fs-card-h:18px;
  --fs-btn:14px;
  --fs-band:14px;
  --fs-summary:14px;
  --fs-table:14px;
  --fs-th:13px;
  --fs-empty:14px;
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
</style></head><body>
<h1>Disco — Phase 2</h1>
<div class="status" id="status">loading…</div>
<div class="tuners" id="tuners"></div>
<script>
const WATERFALL_ROWS = 160;
const SPECTRUM_DB_MIN = -100, SPECTRUM_DB_MAX = -30;
let CONFIG = null;
const tuners = {};

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
    <table data-strongest><thead><tr><th>freq</th><th>SNR</th><th>pwr</th><th>hits</th><th>mode</th><th>conf</th><th>age</th></tr></thead><tbody></tbody></table>
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
async function refreshTables(){
  const [strong, summ] = await Promise.all([
    fetch("/api/strongest?since_seconds=120&per_tuner=8&bin_khz=25").then(r=>r.json()),
    fetch("/api/summary?since_seconds=120").then(r=>r.json()),
  ]);
  for(const tid of CONFIG.tuner_order){
    const t = tuners[tid]; if(!t) continue;
    const s = summ[tid] || {count:0,max_snr:null,last_seen:null,classified:0};
    let sumStr = `120s: ${s.count} det`;
    if(s.classified) sumStr += `, ${s.classified} classified`;
    if(s.max_snr!=null) sumStr += `, peak ${s.max_snr.toFixed(1)} dB`;
    if(s.last_seen) sumStr += `, last ${(Math.round(Date.now()/1000-s.last_seen))}s`;
    t.summary.textContent = sumStr;
    const buckets = (strong.buckets && strong.buckets[tid]) || [];
    const tbody = t.strongestTbody;
    tbody.innerHTML = "";
    if(buckets.length===0){
      tbody.innerHTML = `<tr><td colspan="7" class="empty">no detections in last 120s</td></tr>`;
    } else {
      for(const r of buckets){
        const age = Math.round(Date.now()/1000 - r.last_seen);
        const cls = snrClass(r.max_snr);
        const modCls = modConfClass(r.modulation_confidence);
        let modLabel = r.protocol_tag || r.modulation_class || "—";
        if (r.interpretation) modLabel = modLabel + " 💬";
        const modConf = r.modulation_confidence != null ? r.modulation_confidence.toFixed(2) : "—";
        const tr = document.createElement("tr");
        if (r.interpretation) tr.title = r.interpretation;
        tr.innerHTML = `<td>${(r.freq_hz/1e6).toFixed(4)}</td>`+
          `<td class="${cls}">${r.max_snr.toFixed(1)}</td>`+
          `<td>${r.max_power.toFixed(1)}</td>`+
          `<td>${r.hits}</td>`+
          `<td class="${modCls}">${modLabel}</td>`+
          `<td>${modConf}</td>`+
          `<td>${age}s</td>`;
        tbody.appendChild(tr);
      }
    }
  }
  document.getElementById("status").textContent = `updated ${new Date().toLocaleTimeString()}`;
}
async function init(){
  await loadConfig();
  for(const tid of CONFIG.tuner_order){
    setupTunerCard(tid, CONFIG.tuners[tid]);
  }
  refreshTables();
  setInterval(refreshTables, 2000);
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
