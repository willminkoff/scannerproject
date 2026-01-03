#!/usr/bin/env python3
import json
import os
import re
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse
from typing import Optional

UI_PORT = 5050

CONFIG_SYMLINK = "/usr/local/etc/rtl_airband.conf"
PROFILES_DIR = "/usr/local/etc/airband-profiles"
LAST_HIT_PATH = "/run/rtl_airband_last_freq.txt"
AVOIDS_DIR = "/home/willminkoff/Desktop/scanner_logs"
AVOIDS_PATH = os.path.join(AVOIDS_DIR, "airband_avoids.json")
AVOIDS_SUMMARY_PATH = os.path.join(AVOIDS_DIR, "airband_avoids.txt")

ICECAST_PORT = 8000
MOUNT_NAME = "GND.mp3"

UNITS = {
    "rtl": "rtl-airband",
    "icecast": "icecast2",
    "keepalive": "icecast-keepalive",
}

PROFILES = [
    ("airband", "AIRBAND", os.path.join(PROFILES_DIR, "rtl_airband_airband.conf")),
    ("tower",  "TOWER (118.600)", os.path.join(PROFILES_DIR, "rtl_airband_tower.conf")),
    ("gmrs",   "GMRS", os.path.join(PROFILES_DIR, "rtl_airband_gmrs.conf")),
    ("wx",     "WX (162.550)", os.path.join(PROFILES_DIR, "rtl_airband_wx.conf")),
]

RE_GAIN = re.compile(r'^(\s*gain\s*=\s*)([0-9.]+)(\s*;\s*#\s*UI_CONTROLLED.*)$')
RE_SQL  = re.compile(r'^(\s*squelch_snr_threshold\s*=\s*)([0-9.]+)(\s*;\s*#\s*UI_CONTROLLED.*)$')
RE_FREQS_BLOCK = re.compile(r'(^\s*freqs\s*=\s*\()(.*?)(\)\s*;)', re.S | re.M)
RE_LABELS_BLOCK = re.compile(r'(^\s*labels\s*=\s*\()(.*?)(\)\s*;)', re.S | re.M)
RE_ACTIVITY = re.compile(r'Activity on ([0-9]+\.[0-9]+)')
GAIN_STEPS = [
    0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7,
    16.6, 19.7, 20.7, 22.9, 25.4, 28.0, 29.7, 32.8, 33.8,
    36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6,
]

HTML = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>SprontPi Radio Control</title>
  <style>
    :root { --bg:#0b1020; --card:#121a33; --text:#e8ebff; --muted:#9aa3c7; --good:#22c55e; --bad:#ef4444; --line:#22305e; }
    body { margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; background:var(--bg); color:var(--text); }
    .wrap { max-width: 780px; margin: 0 auto; padding: 18px; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px; box-shadow: 0 10px 30px rgba(0,0,0,.25); }
    h1 { font-size: 18px; margin:0 0 12px; letter-spacing:.2px; }
    .row { display:flex; gap:12px; flex-wrap:wrap; align-items:center; }
    .pill { display:flex; gap:10px; align-items:center; padding:10px 12px; border-radius:999px; border:1px solid var(--line); background: rgba(255,255,255,.03); }
    .pill-text { display:flex; flex-direction:column; }
    .pill-center .pill-text { align-items:center; text-align:center; flex:1; }
    .dot { width:10px; height:10px; border-radius:50%; background: var(--bad); box-shadow: 0 0 0 4px rgba(239,68,68,.12); }
    .dot.good { background: var(--good); box-shadow: 0 0 0 4px rgba(34,197,94,.12); }
    .label { font-size: 13px; color: var(--muted); }
    .val { font-size: 13px; }
    .profiles { margin-top: 12px; display:grid; gap:10px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .profile-card { text-align:left; padding:10px 12px; border:1px solid var(--line); border-radius:12px; cursor:pointer; background: rgba(255,255,255,.02); color: var(--text); }
    .profile-card.selected { border-color: rgba(34,197,94,.55); box-shadow: 0 0 0 1px rgba(34,197,94,.25), 0 12px 18px rgba(0,0,0,.2); }
    .profile-card small { color: var(--muted); display:block; margin-top: 4px; }
    .controls { margin-top: 14px; display:grid; gap:14px; }
    .ctrl { border:1px solid var(--line); border-radius:12px; padding:12px; background: rgba(255,255,255,.02); }
    .ctrl-head { display:flex; justify-content:space-between; align-items:baseline; }
    .ctrl-head b { font-size: 14px; }
    .ctrl-head span { color: var(--muted); font-size: 12px; }
    .ctrl-readout { margin-top: 8px; font-size: 13px; color: var(--muted); display:flex; justify-content:space-between; }
    input[type="range"] { width: 100%; }
    .range {
      -webkit-appearance: none;
      appearance: none;
      width: 100%;
      height: 14px;
      border-radius: 999px;
      background: linear-gradient(90deg, rgba(34,197,94,.55), rgba(34,197,94,.15));
      outline: none;
    }
    .range::-webkit-slider-thumb {
      -webkit-appearance: none;
      width: 32px;
      height: 32px;
      border-radius: 50%;
      background: #e8ebff;
      border: 2px solid #2a3a6f;
      box-shadow: 0 4px 12px rgba(0,0,0,.35);
    }
    .range::-moz-range-thumb {
      width: 32px;
      height: 32px;
      border-radius: 50%;
      background: #e8ebff;
      border: 2px solid #2a3a6f;
      box-shadow: 0 4px 12px rgba(0,0,0,.35);
    }
    @media (max-width: 520px) {
      .ctrl { padding: 14px; }
      .ctrl-head b { font-size: 16px; }
      .ctrl-head span { font-size: 13px; }
      .ctrl-readout { font-size: 14px; }
      .range { height: 18px; }
      .range::-webkit-slider-thumb { width: 38px; height: 38px; }
      .range::-moz-range-thumb { width: 38px; height: 38px; }
      button { padding: 12px 14px; font-size: 15px; }
      h1 { font-size: 20px; }
      .profiles { grid-template-columns: 1fr; }
    }
    .btns { display:flex; gap:10px; flex-wrap:wrap; margin-top: 12px; }
    button { border:1px solid var(--line); background: rgba(255,255,255,.06); color:var(--text); padding:10px 12px; border-radius:12px; cursor:pointer; }
    button.primary { background: rgba(34,197,94,.18); border-color: rgba(34,197,94,.35); }
    .foot { margin-top: 12px; color: var(--muted); font-size: 12px; }
    .warn { color: #fbbf24; font-size: 12px; margin-top: 8px; }
    .avoids { margin-top: 8px; color: var(--muted); font-size: 12px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>SprontPi Radio Control</h1>

      <div class="row">
        <div class="pill"><div id="dot-rtl" class="dot"></div><div><div class="label">Scanner</div><div class="val" id="txt-rtl">…</div></div></div>
        <div class="pill"><div id="dot-ice" class="dot"></div><div><div class="label">Icecast</div><div class="val" id="txt-ice">…</div></div></div>
        <div class="pill pill-center"><div class="dot good"></div><div class="pill-text"><div class="label">Last hit</div><div class="val" id="txt-hit">…</div></div></div>
      </div>

      <div class="btns" style="margin-top:14px;">
        <button class="primary" id="btn-play">▶ Play</button>
        <button id="btn-refresh" title="Refresh status and sync sliders without restarting">↻ Refresh</button>
        <button id="btn-avoid">Avoid Current Hit</button>
        <button id="btn-clear-avoids">Clear Avoids</button>
      </div>

      <div class="profiles" id="profiles"></div>

      <div class="controls">
        <div class="ctrl">
          <div class="ctrl-head"><b>Gain (dB)</b><span>Applied: <span id="applied-gain">…</span></span></div>
          <input id="gain" class="range" type="range" min="0" max="28" step="1" />
          <div class="ctrl-readout"><span>Selected: <span id="selected-gain">…</span> dB</span><span>RTL-SDR steps</span></div>
        </div>

        <div class="ctrl">
          <div class="ctrl-head"><b>Squelch (SNR)</b><span>Applied: <span id="applied-sql">…</span></span></div>
          <input id="sql" class="range" type="range" min="0" max="10" step="0.1" />
          <div class="ctrl-readout"><span>Selected: <span id="selected-sql">…</span></span><span>0.0-10.0 SNR threshold</span></div>
        </div>
      </div>

      <div class="warn" id="warn"></div>
      <div class="avoids" id="avoids-summary"></div>
      <div class="foot">Tip: switching profiles or applying changes restarts the scanner; refresh only syncs status + sliders.</div>
    </div>
  </div>

<script>
const profilesEl = document.getElementById('profiles');
const warnEl = document.getElementById('warn');
const avoidsEl = document.getElementById('avoids-summary');
const gainEl = document.getElementById('gain');
const sqlEl = document.getElementById('sql');
const selectedGainEl = document.getElementById('selected-gain');
const selectedSqlEl = document.getElementById('selected-sql');
let actionMsg = '';

const GAIN_STEPS = [
  0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7,
  16.6, 19.7, 20.7, 22.9, 25.4, 28.0, 29.7, 32.8, 33.8,
  36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6,
];

let currentProfile = null;
let sliderDirty = false;
let applyInFlight = false;

gainEl.max = String(GAIN_STEPS.length - 1);

function setDot(id, ok) {
  const el = document.getElementById(id);
  if (ok) el.classList.add('good'); else el.classList.remove('good');
}

function gainIndexFromValue(value) {
  let best = 0;
  let bestDiff = Infinity;
  GAIN_STEPS.forEach((g, idx) => {
    const diff = Math.abs(g - value);
    if (diff < bestDiff) {
      bestDiff = diff;
      best = idx;
    }
  });
  return best;
}

function updateSelectedGain() {
  const idx = Number(gainEl.value || 0);
  selectedGainEl.textContent = GAIN_STEPS[idx].toFixed(1);
}

function updateSelectedSql() {
  selectedSqlEl.textContent = Number(sqlEl.value).toFixed(1);
}

function updateWarn(missingProfiles) {
  const parts = [];
  if (missingProfiles.length) {
    parts.push('Missing profile file(s): ' + missingProfiles.join(' • '));
  }
  if (actionMsg) {
    parts.push(actionMsg);
  }
  warnEl.textContent = parts.join(' • ');
}

function updateAvoids(avoids) {
  if (!avoidsEl) return;
  if (!avoids) {
    avoidsEl.textContent = '';
    return;
  }
  const count = avoids.count || 0;
  const sample = (avoids.sample || []).filter(Boolean);
  let text = count ? `Avoids: ${count} for this profile` : 'Avoids: none';
  if (sample.length) {
    text += ` (${sample.join(', ')})`;
  }
  avoidsEl.textContent = text;
}

function buildProfiles(profiles, selected) {
  profilesEl.innerHTML = '';
  profiles.forEach(p => {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'profile-card' + (p.id === selected ? ' selected' : '');
    card.setAttribute('aria-pressed', p.id === selected ? 'true' : 'false');
    card.innerHTML = `<div><b>${p.label}</b></div>` + (p.exists ? '' : `<small>Missing: ${p.path}</small>`);
    card.addEventListener('click', async () => {
      if (p.id === selected) return;
      await post('/api/profile', {profile: p.id});
      await refresh(true);
    });
    profilesEl.appendChild(card);
  });
}

async function getJSON(url) {
  const r = await fetch(url, {cache:'no-store'});
  return await r.json();
}
async function post(url, obj) {
  const body = new URLSearchParams(obj).toString();
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body});
  return await r.json();
}

async function refresh(allowSetSliders=false) {
  const st = await getJSON('/api/status');

  setDot('dot-rtl', st.rtl_active);
  setDot('dot-ice', st.icecast_active);

  document.getElementById('txt-rtl').textContent = st.rtl_active ? 'Running' : 'Stopped';
  document.getElementById('txt-ice').textContent = st.icecast_active ? 'Running' : 'Stopped';
  document.getElementById('txt-hit').textContent = st.last_hit || 'No hits yet';

  document.getElementById('applied-gain').textContent = st.gain.toFixed(1);
  document.getElementById('applied-sql').textContent = st.squelch.toFixed(1);

  updateWarn(st.missing_profiles);
  updateAvoids(st.avoids);

  if (currentProfile === null || currentProfile !== st.profile) {
    currentProfile = st.profile;
    buildProfiles(st.profiles, st.profile);
  }

  if (allowSetSliders && !sliderDirty) {
    gainEl.value = gainIndexFromValue(st.gain);
    sqlEl.value = Math.max(0, Math.min(10, st.squelch)).toFixed(1);
    updateSelectedGain();
    updateSelectedSql();
  }
}

gainEl.addEventListener('input', ()=> {
  sliderDirty = true;
  updateSelectedGain();
});
sqlEl.addEventListener('input', ()=> {
  sliderDirty = true;
  updateSelectedSql();
});
gainEl.addEventListener('change', ()=> applyControls());
sqlEl.addEventListener('change', ()=> applyControls());

document.getElementById('btn-refresh').addEventListener('click', async ()=> {
  sliderDirty = false;
  await refresh(true);
});

async function applyControls() {
  if (applyInFlight) return;
  applyInFlight = true;
  try {
    const gain = GAIN_STEPS[Number(gainEl.value || 0)];
    const squelch = sqlEl.value;
    await post('/api/apply', {gain, squelch});
    sliderDirty = false;
    await refresh(true);
  } finally {
    applyInFlight = false;
  }
}

document.getElementById('btn-avoid').addEventListener('click', async ()=> {
  const res = await post('/api/avoid', {});
  actionMsg = res.ok ? `Avoided ${res.freq}` : (res.error || 'Avoid failed');
  await refresh(true);
});

document.getElementById('btn-clear-avoids').addEventListener('click', async ()=> {
  const res = await post('/api/avoid-clear', {});
  actionMsg = res.ok ? 'Cleared avoids' : (res.error || 'Clear avoids failed');
  await refresh(true);
});

document.getElementById('btn-play').addEventListener('click', ()=> {
  const url = `http://${location.hostname}:8000/GND.mp3`;
  window.open(url, '_blank', 'noopener');
});

refresh(true);
setInterval(()=>refresh(false), 1500);
</script>
</body>
</html>
"""

def unit_active(unit: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0

def read_active_config_path() -> str:
    try:
        return os.path.realpath(CONFIG_SYMLINK)
    except Exception:
        return CONFIG_SYMLINK

def parse_controls(conf_path: str):
    gain = 32.8
    squelch = 10.0
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = RE_GAIN.match(line)
                if m:
                    gain = float(m.group(2))
                m = RE_SQL.match(line)
                if m:
                    squelch = float(m.group(2))
    except FileNotFoundError:
        pass
    return gain, squelch

def read_last_hit_from_journal_cached() -> str:
    # Cache to avoid spawning journalctl on every poll.
    now = time.time()
    cache = getattr(read_last_hit_from_journal_cached, "_cache", {"value": "", "ts": 0.0})
    if now - cache["ts"] < 2.0:
        return cache["value"]
    value = read_last_hit_from_journal()
    cache = {"value": value, "ts": now}
    read_last_hit_from_journal_cached._cache = cache
    return value

def read_last_hit() -> str:
    try:
        with open(LAST_HIT_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = [line.strip() for line in f.read().splitlines() if line.strip()]
            if not lines:
                return read_last_hit_from_journal_cached()
            value = lines[-1]
            if value and value != "-":
                return value
    except FileNotFoundError:
        pass

    return read_last_hit_from_journal_cached()

def read_last_hit_from_journal() -> str:
    try:
        result = subprocess.run(
            ["journalctl", "-u", UNITS["rtl"], "-n", "5", "-o", "cat", "--no-pager"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        return ""
    matches = RE_ACTIVITY.findall(result.stdout or "")
    if not matches:
        return ""
    return matches[-1]

def parse_last_hit_freq() -> Optional[float]:
    value = read_last_hit()
    if not value:
        return None
    m = re.search(r'[0-9]+(?:\.[0-9]+)?', value)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None

def load_avoids() -> dict:
    try:
        with open(AVOIDS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("profiles", {})
                return data
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        pass
    return {"profiles": {}}

def summarize_avoids(conf_path: str) -> dict:
    data = load_avoids()
    prof = data.get("profiles", {}).get(conf_path, {})
    avoids = prof.get("avoids", []) or []
    avoids_sorted = sorted(avoids)
    sample = [f"{freq:.4f}" for freq in avoids_sorted[:4]]
    return {"count": len(avoids), "sample": sample}

def save_avoids(data: dict) -> None:
    os.makedirs(AVOIDS_DIR, exist_ok=True)
    tmp = AVOIDS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, AVOIDS_PATH)
    write_avoids_summary(data)

def write_avoids_summary(data: dict) -> None:
    lines = []
    ts = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
    lines.append(f"SprontPi Avoids Summary (UTC {ts})")
    lines.append("")

    profiles = data.get("profiles", {})
    if not profiles:
        lines.append("No avoids recorded.")
    else:
        path_to_label = {path: label for _, label, path in PROFILES}
        for conf_path in sorted(profiles.keys()):
            prof = profiles.get(conf_path, {})
            avoids = sorted(prof.get("avoids", []) or [])
            label = path_to_label.get(conf_path, os.path.basename(conf_path))
            lines.append(f"Profile: {label}")
            lines.append(f"Config: {conf_path}")
            if avoids:
                lines.append(f"Avoids ({len(avoids)}): " + ", ".join(f"{f:.4f}" for f in avoids))
            else:
                lines.append("Avoids: none")
            lines.append("")

    tmp = AVOIDS_SUMMARY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    os.replace(tmp, AVOIDS_SUMMARY_PATH)

def parse_freqs_labels(text: str):
    m = RE_FREQS_BLOCK.search(text)
    if not m:
        raise ValueError("freqs block not found")
    freqs = [float(x) for x in re.findall(r'[0-9]+(?:\.[0-9]+)?', m.group(2))]
    labels = None
    m = RE_LABELS_BLOCK.search(text)
    if m:
        labels = re.findall(r'"([^"]+)"', m.group(2))
    return freqs, labels

def replace_freqs_labels(text: str, freqs, labels):
    freqs_text = ", ".join(f"{f:.4f}" for f in freqs)
    text = RE_FREQS_BLOCK.sub(lambda m: f"{m.group(1)}{freqs_text}{m.group(3)}", text, count=1)
    if labels is not None:
        labels_text = ", ".join(f"\"{l}\"" for l in labels)
        text = RE_LABELS_BLOCK.sub(lambda m: f"{m.group(1)}{labels_text}{m.group(3)}", text, count=1)
    return text

def same_freq(a: float, b: float) -> bool:
    return abs(a - b) < 0.0005

def filter_freqs_labels(freqs, labels, avoids):
    if labels is not None and len(labels) != len(freqs):
        labels = [f"{f:.4f}" for f in freqs]
    kept_freqs = []
    kept_labels = [] if labels is not None else None
    for idx, freq in enumerate(freqs):
        if any(same_freq(freq, avoid) for avoid in avoids):
            continue
        kept_freqs.append(freq)
        if kept_labels is not None:
            kept_labels.append(labels[idx])
    return kept_freqs, kept_labels

def write_freqs_labels(conf_path: str, freqs, labels):
    with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    text = replace_freqs_labels(text, freqs, labels)
    tmp = conf_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, conf_path)

def avoid_current_hit(conf_path: str):
    freq = parse_last_hit_freq()
    if freq is None:
        return None, "No recent hit to avoid"

    data = load_avoids()
    profiles = data.setdefault("profiles", {})
    prof = profiles.get(conf_path)

    if not prof:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            freqs, labels = parse_freqs_labels(f.read())
        prof = {
            "original_freqs": freqs,
            "original_labels": labels,
            "avoids": [],
        }
        profiles[conf_path] = prof

    avoids = prof.get("avoids", [])
    if not any(same_freq(freq, avoid) for avoid in avoids):
        avoids.append(freq)
        prof["avoids"] = avoids

    base_freqs = prof.get("original_freqs") or []
    base_labels = prof.get("original_labels")
    if not base_freqs:
        return None, "No freqs found to avoid"

    new_freqs, new_labels = filter_freqs_labels(base_freqs, base_labels, avoids)
    if not new_freqs:
        return None, "Avoid would remove all frequencies"

    write_freqs_labels(conf_path, new_freqs, new_labels)
    save_avoids(data)
    restart_rtl()
    return freq, None

def clear_avoids(conf_path: str):
    data = load_avoids()
    profiles = data.get("profiles", {})
    prof = profiles.get(conf_path)
    if not prof:
        return 0, None

    freqs = prof.get("original_freqs") or []
    labels = prof.get("original_labels")
    if not freqs:
        return 0, "No stored freqs to restore"

    write_freqs_labels(conf_path, freqs, labels)
    profiles.pop(conf_path, None)
    save_avoids(data)
    restart_rtl()
    return len(freqs), None

def write_controls(conf_path: str, gain: float, squelch: float) -> bool:
    # clamp and snap to tuner steps
    gain_value = float(gain)
    gain = min(GAIN_STEPS, key=lambda g: abs(g - gain_value))
    squelch = max(0.0, min(10.0, float(squelch)))

    with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    out = []
    changed = False
    for line in lines:
        m = RE_GAIN.match(line)
        if m:
            new_line = f"{m.group(1)}{gain:.3f}{m.group(3)}\n"
            if new_line != line:
                changed = True
            out.append(new_line)
            continue
        m = RE_SQL.match(line)
        if m:
            new_line = f"{m.group(1)}{squelch:.3f}{m.group(3)}\n"
            if new_line != line:
                changed = True
            out.append(new_line)
            continue
        out.append(line)

    if not changed:
        return False

    tmp = conf_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(out)
    os.replace(tmp, conf_path)
    return True

def restart_rtl():
    subprocess.Popen(
        ["systemctl", "restart", "--no-block", UNITS["rtl"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

def set_profile(profile_id: str, current_conf_path: str):
    for pid, _, path in PROFILES:
        if pid == profile_id:
            if os.path.realpath(path) == os.path.realpath(current_conf_path):
                return True, False
            subprocess.run(["ln", "-sf", path, CONFIG_SYMLINK], check=False)
            return True, True
    return False, False

def guess_current_profile(conf_realpath: str):
    for pid, _, path in PROFILES:
        if os.path.realpath(path) == conf_realpath:
            return pid
    return "airband"

def icecast_up():
    return unit_active(UNITS["icecast"])

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/":
            return self._send(200, HTML)
        if p == "/api/status":
            conf_path = read_active_config_path()
            gain, squelch = parse_controls(conf_path)
            rtl_ok = unit_active(UNITS["rtl"])
            ice_ok = icecast_up()

            missing = []
            prof_payload = []
            for pid, label, path in PROFILES:
                exists = os.path.exists(path)
                if not exists:
                    missing.append(path)
                prof_payload.append({"id": pid, "label": label, "path": path, "exists": exists})

            profile = guess_current_profile(conf_path)

            payload = {
                "rtl_active": rtl_ok,
                "icecast_active": ice_ok,
                "keepalive_active": unit_active(UNITS["keepalive"]),
                "profile": profile,
                "profiles": prof_payload,
                "missing_profiles": missing,
                "gain": float(gain),
                "squelch": float(squelch),
                "last_hit": read_last_hit(),
                "avoids": summarize_avoids(conf_path),
            }
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        return self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        p = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="ignore")
        form = {k: v[0] for k, v in parse_qs(raw).items()}

        if p == "/api/profile":
            pid = form.get("profile", "")
            conf_path = read_active_config_path()
            ok, changed = set_profile(pid, conf_path)
            if ok:
                if changed:
                    restart_rtl()
                return self._send(200, json.dumps({"ok": True, "changed": changed}), "application/json; charset=utf-8")
            return self._send(400, json.dumps({"ok": False, "error": "unknown profile"}), "application/json; charset=utf-8")

        if p == "/api/apply":
            conf_path = read_active_config_path()
            try:
                gain = float(form.get("gain", "32.8"))
                squelch = float(form.get("squelch", "10.0"))
            except ValueError:
                return self._send(400, json.dumps({"ok": False, "error": "bad values"}), "application/json; charset=utf-8")

            try:
                changed = write_controls(conf_path, gain, squelch)
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")

            if changed:
                restart_rtl()
            return self._send(200, json.dumps({"ok": True, "changed": changed}), "application/json; charset=utf-8")

        if p == "/api/avoid":
            conf_path = read_active_config_path()
            try:
                freq, err = avoid_current_hit(conf_path)
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            if err:
                return self._send(400, json.dumps({"ok": False, "error": err}), "application/json; charset=utf-8")
            return self._send(200, json.dumps({"ok": True, "freq": f"{freq:.4f}"}), "application/json; charset=utf-8")

        if p == "/api/avoid-clear":
            conf_path = read_active_config_path()
            try:
                _, err = clear_avoids(conf_path)
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            if err:
                return self._send(400, json.dumps({"ok": False, "error": err}), "application/json; charset=utf-8")
            return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8")

        return self._send(404, json.dumps({"ok": False, "error": "not found"}), "application/json; charset=utf-8")

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

def main():
    server = ThreadedHTTPServer(("0.0.0.0", UI_PORT), Handler)
    print(f"UI listening on 0.0.0.0:{UI_PORT}")
    server.serve_forever()

if __name__ == "__main__":
    main()
