#!/usr/bin/env python3
import datetime
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
GROUND_CONFIG_PATH = "/usr/local/etc/rtl_airband_ground.conf"
LAST_HIT_AIRBAND_PATH = "/run/rtl_airband_last_freq_airband.txt"
LAST_HIT_GROUND_PATH = "/run/rtl_airband_last_freq_ground.txt"
AVOIDS_DIR = "/home/willminkoff/scannerproject/admin/logs"
DIAGNOSTIC_DIR = AVOIDS_DIR
AVOIDS_PATH = os.path.join(AVOIDS_DIR, "airband_avoids.json")
AVOIDS_SUMMARY_PATH = os.path.join(AVOIDS_DIR, "airband_avoids.txt")

ICECAST_PORT = 8000
MOUNT_NAME = "GND.mp3"
ICECAST_STATUS_URL = f"http://127.0.0.1:{ICECAST_PORT}/status-json.xsl"
ICECAST_MOUNT_PATH = f"/{MOUNT_NAME}"
ICECAST_HIT_LOG_PATH = "/run/airband_ui_hitlog.jsonl"
ICECAST_HIT_LOG_LIMIT = 200

UNITS = {
    "rtl": "rtl-airband",
    "ground": "rtl-airband-ground",
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
RE_AIRBAND = re.compile(r'^\s*airband\s*=\s*(true|false)\s*;\s*$', re.I)
RE_INDEX = re.compile(r'^(\s*index\s*=\s*)(\d+)(\s*;.*)$')
RE_FREQS_BLOCK = re.compile(r'(^\s*freqs\s*=\s*\()(.*?)(\)\s*;)', re.S | re.M)
RE_LABELS_BLOCK = re.compile(r'(^\s*labels\s*=\s*\()(.*?)(\)\s*;)', re.S | re.M)
RE_ACTIVITY = re.compile(r'Activity on ([0-9]+\.[0-9]+)')
RE_ACTIVITY_TS = re.compile(
    r'^(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<time>\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|[A-Z]{2,5})?\s+.*Activity on (?P<freq>[0-9]+\.[0-9]+)'
)
HIT_GAP_RESET_SECONDS = 10
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
    button.pill { background: rgba(255,255,255,.03); }
    button.primary { background: rgba(34,197,94,.18); border-color: rgba(34,197,94,.35); }
    .tabs { display:flex; gap:10px; margin-top: 14px; }
    .tab { flex:1; text-align:center; padding:10px 12px; border-radius:999px; border:1px solid var(--line); background: rgba(255,255,255,.04); color: var(--muted); }
    .tab.active { color: var(--text); background: rgba(34,197,94,.16); border-color: rgba(34,197,94,.35); box-shadow: 0 0 0 1px rgba(34,197,94,.2); }
    .swipe-hint { margin-top: 8px; font-size: 12px; color: var(--muted); text-align:center; }
    .pager { margin-top: 12px; overflow:hidden; }
    .pager-inner { display:flex; transition: transform 0.3s ease; }
    .page { min-width:100%; box-sizing:border-box; }
    .page-title { font-size: 15px; color: var(--muted); margin: 4px 0 8px; letter-spacing: .2px; text-transform: uppercase; }
    .foot { margin-top: 12px; color: var(--muted); font-size: 12px; }
    .foot a { color: var(--muted); text-decoration: underline; }
    .foot a:hover { color: var(--text); }
    .warn { color: #fbbf24; font-size: 12px; margin-top: 8px; }
    .avoids { margin-top: 8px; color: var(--muted); font-size: 12px; }
    .hidden { display:none; }
    .nav { display:flex; align-items:center; gap:10px; margin-bottom: 12px; }
    .nav button { padding:8px 12px; }
    .hit-list { display:flex; flex-direction:column; gap:8px; }
    .hit-row { display:grid; grid-template-columns: 90px 1fr 90px; gap:8px; padding:10px 12px; border:1px solid var(--line); border-radius:12px; background: rgba(255,255,255,.02); font-size: 13px; }
    .hit-row.head { font-size: 12px; color: var(--muted); background: transparent; border-style: dashed; }
    .hit-empty { color: var(--muted); font-size: 13px; padding: 8px 2px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div id="view-main">
        <h1>SprontPi Radio Control</h1>

        <div class="row">
          <button class="pill hit-pill" id="btn-hit-airband" type="button">
            <div class="dot good"></div>
            <div>
              <div class="label">Airband Hits</div>
              <div class="val" id="txt-hit-airband">…</div>
            </div>
          </button>
          <button class="pill hit-pill" id="btn-hit-ground" type="button">
            <div class="dot good"></div>
            <div>
              <div class="label">Ground Hits</div>
              <div class="val" id="txt-hit-ground">…</div>
            </div>
          </button>
        </div>

        <div class="row">
          <div class="pill"><div id="dot-rtl" class="dot"></div><div><div class="label">Airband Scanner</div><div class="val" id="txt-rtl">…</div></div></div>
          <div class="pill"><div id="dot-ground" class="dot"></div><div><div class="label">Ground Scanner</div><div class="val" id="txt-ground">…</div></div></div>
          <div class="pill"><div id="dot-ice" class="dot"></div><div><div class="label">Icecast</div><div class="val" id="txt-ice">…</div></div></div>
        </div>

        <div class="btns" style="margin-top:14px;">
          <button class="primary" id="btn-play">Play</button>
          <button id="btn-avoid">Avoid</button>
          <button id="btn-clear-avoids">Clear</button>
        </div>

        <div class="tabs">
          <button type="button" class="tab active" id="tab-airband">Airband</button>
          <button type="button" class="tab" id="tab-ground">Ground</button>
        </div>
        <div class="swipe-hint">Swipe between airband and ground controls.</div>

        <div class="pager" id="pager">
          <div class="pager-inner" id="pager-inner">
            <section class="page">
              <div class="page-title">Airband Scanner Controls</div>
              <div class="profiles" id="profiles-airband"></div>

              <div class="controls">
                <div class="ctrl">
                  <div class="ctrl-head"><b>Gain (dB)</b><span>Applied: <span id="applied-gain-airband">…</span></span></div>
                  <input id="gain-airband" class="range" type="range" min="0" max="28" step="1" />
                  <div class="ctrl-readout"><span>Selected: <span id="selected-gain-airband">…</span> dB</span><span>RTL-SDR steps</span></div>
                </div>

                <div class="ctrl">
                  <div class="ctrl-head"><b>Squelch (SNR)</b><span>Applied: <span id="applied-sql-airband">…</span></span></div>
                  <input id="sql-airband" class="range" type="range" min="0" max="10" step="0.1" />
                  <div class="ctrl-readout"><span>Selected: <span id="selected-sql-airband">…</span></span><span>0.0-10.0 SNR threshold</span></div>
                </div>
              </div>
            </section>

            <section class="page">
              <div class="page-title">Ground Scanner Controls</div>
              <div class="profiles hidden" id="profiles-ground"></div>
              <div class="controls">
                <div class="ctrl">
                  <div class="ctrl-head"><b>Gain (dB)</b><span>Applied: <span id="applied-gain-ground">…</span></span></div>
                  <input id="gain-ground" class="range" type="range" min="0" max="28" step="1" />
                  <div class="ctrl-readout"><span>Selected: <span id="selected-gain-ground">…</span> dB</span><span>RTL-SDR steps</span></div>
                </div>

                <div class="ctrl">
                  <div class="ctrl-head"><b>Squelch (SNR)</b><span>Applied: <span id="applied-sql-ground">…</span></span></div>
                  <input id="sql-ground" class="range" type="range" min="0" max="10" step="0.1" />
                  <div class="ctrl-readout"><span>Selected: <span id="selected-sql-ground">…</span></span><span>0.0-10.0 SNR threshold</span></div>
                </div>
              </div>
              <div class="foot">Both scanners feed the same Icecast stream.</div>
            </section>
          </div>
        </div>

        <div class="warn" id="warn"></div>
        <div class="avoids" id="avoids-summary"></div>
        <div class="foot">Tip: switching profiles or applying changes restarts the scanner; refresh only syncs status + sliders. <a href="#" id="lnk-diagnostic">Generate log</a></div>
      </div>

      <div id="view-hits" class="hidden">
        <div class="nav">
          <button type="button" id="btn-hit-back">Back</button>
          <h1 style="margin:0;">Live Hit List</h1>
        </div>
        <div class="hit-list" id="hit-list">
          <div class="hit-row head"><div>Time</div><div>Frequency</div><div>Duration</div></div>
        </div>
        <div class="foot">Showing the latest 20 hits from the scanner journal.</div>
      </div>
    </div>
  </div>

<script>
const profilesAirbandEl = document.getElementById('profiles-airband');
const profilesGroundEl = document.getElementById('profiles-ground');
const warnEl = document.getElementById('warn');
const avoidsEl = document.getElementById('avoids-summary');
const viewMainEl = document.getElementById('view-main');
const viewHitsEl = document.getElementById('view-hits');
const hitListEl = document.getElementById('hit-list');
const tabAirbandEl = document.getElementById('tab-airband');
const tabGroundEl = document.getElementById('tab-ground');
const pagerEl = document.getElementById('pager');
const pagerInnerEl = document.getElementById('pager-inner');
let actionMsg = '';

const GAIN_STEPS = [
  0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7,
  16.6, 19.7, 20.7, 22.9, 25.4, 28.0, 29.7, 32.8, 33.8,
  36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6,
];

let currentProfileAirband = null;
let currentProfileGround = null;
let hitsView = false;
let activePage = 0;

const controlTargets = {
  airband: {
    gainEl: document.getElementById('gain-airband'),
    sqlEl: document.getElementById('sql-airband'),
    selectedGainEl: document.getElementById('selected-gain-airband'),
    selectedSqlEl: document.getElementById('selected-sql-airband'),
    appliedGainEl: document.getElementById('applied-gain-airband'),
    appliedSqlEl: document.getElementById('applied-sql-airband'),
    dirty: false,
    applyInFlight: false,
  },
  ground: {
    gainEl: document.getElementById('gain-ground'),
    sqlEl: document.getElementById('sql-ground'),
    selectedGainEl: document.getElementById('selected-gain-ground'),
    selectedSqlEl: document.getElementById('selected-sql-ground'),
    appliedGainEl: document.getElementById('applied-gain-ground'),
    appliedSqlEl: document.getElementById('applied-sql-ground'),
    dirty: false,
    applyInFlight: false,
  },
};

Object.values(controlTargets).forEach(target => {
  target.gainEl.max = String(GAIN_STEPS.length - 1);
});

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

function updateSelectedGain(target) {
  const controls = controlTargets[target];
  const idx = Number(controls.gainEl.value || 0);
  controls.selectedGainEl.textContent = GAIN_STEPS[idx].toFixed(1);
}

function updateSelectedSql(target) {
  const controls = controlTargets[target];
  controls.selectedSqlEl.textContent = Number(controls.sqlEl.value).toFixed(1);
}

function formatHitLabel(value) {
  if (value === null || value === undefined) return '';
  const text = String(value).trim();
  if (!text) return '';
  if (/^[0-9]+(\.[0-9]+)?$/.test(text)) {
    const num = Number(text);
    if (Number.isFinite(num)) {
      return num.toFixed(2);
    }
  }
  return text;
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

function buildProfiles(profilesEl, profiles, selected, target) {
  profilesEl.innerHTML = '';
  if (!profiles.length) {
    profilesEl.classList.add('hidden');
    return;
  }
  profilesEl.classList.remove('hidden');
  profiles.forEach(p => {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'profile-card' + (p.id === selected ? ' selected' : '');
    card.setAttribute('aria-pressed', p.id === selected ? 'true' : 'false');
    card.innerHTML = `<div><b>${p.label}</b></div>` + (p.exists ? '' : `<small>Missing: ${p.path}</small>`);
    card.addEventListener('click', async () => {
      if (p.id === selected) return;
      await post('/api/profile', {profile: p.id, target});
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

function setControlsFromStatus(target, gain, squelch, allowSetSliders) {
  const controls = controlTargets[target];
  controls.appliedGainEl.textContent = gain.toFixed(1);
  controls.appliedSqlEl.textContent = squelch.toFixed(1);
  if (allowSetSliders && !controls.dirty) {
    controls.gainEl.value = gainIndexFromValue(gain);
    controls.sqlEl.value = Math.max(0, Math.min(10, squelch)).toFixed(1);
    updateSelectedGain(target);
    updateSelectedSql(target);
  }
}

async function refresh(allowSetSliders=false) {
  const st = await getJSON('/api/status');

  setDot('dot-rtl', st.rtl_active);
  setDot('dot-ground', st.ground_active);
  setDot('dot-ice', st.icecast_active);

  document.getElementById('txt-rtl').textContent = st.rtl_active ? 'Running' : 'Stopped';
  document.getElementById('txt-ground').textContent = st.ground_active ? 'Running' : 'Stopped';
  document.getElementById('txt-ice').textContent = st.icecast_active ? 'Running' : 'Stopped';
  const airbandHit = formatHitLabel(st.last_hit_airband) || '—';
  const groundHit = formatHitLabel(st.last_hit_ground) || '—';
  document.getElementById('txt-hit-airband').textContent = airbandHit;
  document.getElementById('txt-hit-ground').textContent = groundHit;

  setControlsFromStatus('airband', st.airband_gain, st.airband_squelch, allowSetSliders);
  setControlsFromStatus('ground', st.ground_gain, st.ground_squelch, allowSetSliders);

  updateWarn(st.missing_profiles);
  updateAvoids(st.avoids);

  if (currentProfileAirband === null || currentProfileAirband !== st.profile_airband) {
    currentProfileAirband = st.profile_airband;
    buildProfiles(profilesAirbandEl, st.profiles_airband, st.profile_airband, 'airband');
  }
  if (currentProfileGround === null || currentProfileGround !== st.profile_ground) {
    currentProfileGround = st.profile_ground;
    buildProfiles(profilesGroundEl, st.profiles_ground, st.profile_ground, 'ground');
  }

  if (allowSetSliders) {
    updateSelectedGain('airband');
    updateSelectedSql('airband');
    updateSelectedGain('ground');
    updateSelectedSql('ground');
  }
}

function renderHitList(items) {
  hitListEl.innerHTML = '<div class="hit-row head"><div>Time</div><div>Frequency</div><div>Duration</div></div>';
  if (!items || !items.length) {
    const empty = document.createElement('div');
    empty.className = 'hit-empty';
    empty.textContent = 'No hits yet.';
    hitListEl.appendChild(empty);
    return;
  }
  items.forEach(item => {
    const row = document.createElement('div');
    row.className = 'hit-row';
    const freqText = formatHitLabel(item.freq);
    const isNumeric = /^[0-9]+(\.[0-9]+)?$/.test(freqText);
    const displayFreq = isNumeric ? `${freqText} MHz` : (freqText || '—');
    row.innerHTML = `<div>${item.time}</div><div>${displayFreq}</div><div>${item.duration}s</div>`;
    hitListEl.appendChild(row);
  });
}

async function refreshHitList() {
  const data = await getJSON('/api/hits');
  renderHitList(data.items || []);
}

function wireControls(target) {
  const controls = controlTargets[target];
  controls.gainEl.addEventListener('input', () => {
    controls.dirty = true;
    updateSelectedGain(target);
  });
  controls.sqlEl.addEventListener('input', () => {
    controls.dirty = true;
    updateSelectedSql(target);
  });
  controls.gainEl.addEventListener('change', () => applyControls(target));
  controls.sqlEl.addEventListener('change', () => applyControls(target));
}

wireControls('airband');
wireControls('ground');

async function applyControls(target) {
  const controls = controlTargets[target];
  if (controls.applyInFlight) return;
  controls.applyInFlight = true;
  try {
    const gain = GAIN_STEPS[Number(controls.gainEl.value || 0)];
    const squelch = controls.sqlEl.value;
    await post('/api/apply', {gain, squelch, target});
    controls.dirty = false;
    await refresh(true);
  } finally {
    controls.applyInFlight = false;
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

function setPage(index) {
  activePage = index;
  pagerInnerEl.style.transform = `translateX(-${index * 100}%)`;
  tabAirbandEl.classList.toggle('active', index === 0);
  tabGroundEl.classList.toggle('active', index === 1);
}

tabAirbandEl.addEventListener('click', () => setPage(0));
tabGroundEl.addEventListener('click', () => setPage(1));

let touchStartX = null;
pagerEl.addEventListener('touchstart', (event) => {
  if (!event.touches.length) return;
  touchStartX = event.touches[0].clientX;
}, {passive: true});
pagerEl.addEventListener('touchend', (event) => {
  if (touchStartX === null) return;
  const touch = event.changedTouches[0];
  if (!touch) return;
  const delta = touchStartX - touch.clientX;
  if (Math.abs(delta) > 40) {
    if (delta > 0 && activePage < 1) {
      setPage(activePage + 1);
    } else if (delta < 0 && activePage > 0) {
      setPage(activePage - 1);
    }
  }
  touchStartX = null;
}, {passive: true});

document.getElementById('btn-play').addEventListener('click', ()=> {
  const url = `http://${location.hostname}:8000/GND.mp3`;
  window.open(url, '_blank', 'noopener');
});

async function showHitList() {
  hitsView = true;
  viewMainEl.classList.add('hidden');
  viewHitsEl.classList.remove('hidden');
  await refreshHitList();
}

document.getElementById('btn-hit-airband').addEventListener('click', showHitList);
document.getElementById('btn-hit-ground').addEventListener('click', showHitList);

document.getElementById('btn-hit-back').addEventListener('click', ()=> {
  hitsView = false;
  viewHitsEl.classList.add('hidden');
  viewMainEl.classList.remove('hidden');
});

document.getElementById('lnk-diagnostic').addEventListener('click', async (e)=> {
  e.preventDefault();
  actionMsg = 'Generating log...';
  updateWarn([]);
  const res = await post('/api/diagnostic', {});
  if (res.ok) {
    actionMsg = `Log saved: ${res.path}`;
  } else {
    actionMsg = res.error || 'Log failed';
  }
  await refresh(false);
});

setPage(0);
refresh(true);
setInterval(async ()=> {
  await refresh(false);
  if (hitsView) {
    await refreshHitList();
  }
}, 1500);
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

def read_airband_flag(conf_path: str) -> Optional[bool]:
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                match = RE_AIRBAND.match(line)
                if match:
                    return match.group(1).lower() == "true"
    except FileNotFoundError:
        return None
    return None

def split_profiles():
    prof_payload = []
    for pid, label, path in PROFILES:
        exists = os.path.exists(path)
        airband_flag = read_airband_flag(path) if exists else None
        prof_payload.append({
            "id": pid,
            "label": label,
            "path": path,
            "exists": exists,
            "airband": airband_flag,
        })
    profiles_airband = [p for p in prof_payload if p.get("airband") is True]
    profiles_ground = [p for p in prof_payload if p.get("airband") is False]
    return prof_payload, profiles_airband, profiles_ground

def enforce_profile_index(conf_path: str) -> None:
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return

    airband_value = None
    for line in lines:
        match = RE_AIRBAND.match(line)
        if match:
            airband_value = match.group(1).lower() == "true"
            break
    if airband_value is None:
        return

    desired_index = 0 if airband_value else 1
    out = []
    changed = False
    for line in lines:
        match = RE_INDEX.match(line)
        if match:
            new_line = f"{match.group(1)}{desired_index}{match.group(3)}\n"
            if new_line != line:
                changed = True
            out.append(new_line)
            continue
        out.append(line)

    if not changed:
        return

    tmp = conf_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(out)
    os.replace(tmp, conf_path)

def parse_controls(conf_path: str):
    enforce_profile_index(conf_path)
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

def read_last_hit_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [line.strip() for line in f.read().splitlines() if line.strip()]
            if not lines:
                raise ValueError("empty last-hit file")
            value = lines[-1]
            if value and value != "-":
                return value
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return ""

def read_last_hit_airband() -> str:
    if not unit_active(UNITS["rtl"]):
        return ""
    return read_last_hit_file(LAST_HIT_AIRBAND_PATH)

def read_last_hit_ground() -> str:
    if not unit_active(UNITS["ground"]):
        return ""
    return read_last_hit_file(LAST_HIT_GROUND_PATH)

def read_last_hit() -> str:
    value = read_last_hit_airband()
    if value:
        return value

    items = read_hit_list_cached()
    if items:
        return items[0].get("freq", "") or ""

    return read_last_hit_from_journal_cached()

def read_last_hit_from_journal() -> str:
    try:
        result = subprocess.run(
            ["journalctl", "-u", UNITS["rtl"], "-n", "200", "-o", "cat", "--no-pager"],
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

def parse_activity_timestamp(date_part: str, time_part: str, tz_part: Optional[str]) -> datetime.datetime:
    del tz_part
    return datetime.datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S")

def read_hit_list_for_unit(unit: str, limit: int = 20, scan_lines: int = 200) -> list:
    try:
        result = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(scan_lines), "-o", "short-iso", "--no-pager"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        return []

    hits = []
    for line in (result.stdout or "").splitlines():
        match = RE_ACTIVITY_TS.search(line)
        if not match:
            continue
        ts = parse_activity_timestamp(match.group("date"), match.group("time"), None)
        freq = match.group("freq")
        hits.append((ts, freq))

    if not hits:
        return []

    entries = []
    current_freq = None
    start_ts = None
    last_ts = None
    for ts, freq in hits:
        if current_freq is None:
            current_freq = freq
            start_ts = ts
            last_ts = ts
            continue
        gap = (ts - last_ts).total_seconds() if last_ts else None
        if freq != current_freq or (gap is not None and gap > HIT_GAP_RESET_SECONDS):
            duration = int((last_ts - start_ts).total_seconds()) if start_ts else 0
            try:
                freq_text = f"{float(current_freq):.4f}"
            except ValueError:
                freq_text = current_freq
            entries.append({
                "time": last_ts.strftime("%H:%M:%S"),
                "freq": freq_text,
                "duration": duration,
                "ts": last_ts.timestamp(),
            })
            current_freq = freq
            start_ts = ts
        last_ts = ts

    if current_freq is not None and start_ts is not None and last_ts is not None:
        duration = int((last_ts - start_ts).total_seconds())
        try:
            freq_text = f"{float(current_freq):.4f}"
        except ValueError:
            freq_text = current_freq
        entries.append({
            "time": last_ts.strftime("%H:%M:%S"),
            "freq": freq_text,
            "duration": duration,
            "ts": last_ts.timestamp(),
        })

    entries = entries[-limit:]
    entries.reverse()
    return entries

def read_hit_list(limit: int = 20, scan_lines: int = 200) -> list:
    entries = []
    if unit_active(UNITS["rtl"]):
        entries.extend(read_hit_list_for_unit(UNITS["rtl"], limit=limit, scan_lines=scan_lines))
    if unit_active(UNITS["ground"]):
        entries.extend(read_hit_list_for_unit(UNITS["ground"], limit=limit, scan_lines=scan_lines))
    if not entries:
        return []
    entries.sort(key=lambda item: item.get("ts", 0))
    entries = entries[-limit:]
    entries.reverse()
    for item in entries:
        item.pop("ts", None)
    return entries

def read_hit_list_cached() -> list:
    now = time.time()
    cache = getattr(read_hit_list_cached, "_cache", {"value": [], "ts": 0.0})
    if now - cache["ts"] < 2.0:
        return cache["value"]
    value = read_hit_list()
    cache = {"value": value, "ts": now}
    read_hit_list_cached._cache = cache
    return value

def _load_icecast_hit_log():
    cache = getattr(_load_icecast_hit_log, "_cache", None)
    if cache is not None:
        return cache
    cache = {
        "entries": [],
        "current": None,
    }
    try:
        with open(ICECAST_HIT_LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    cache["entries"].append(entry)
    except FileNotFoundError:
        pass
    if len(cache["entries"]) > ICECAST_HIT_LOG_LIMIT:
        cache["entries"] = cache["entries"][-ICECAST_HIT_LOG_LIMIT:]
    _load_icecast_hit_log._cache = cache
    return cache

def _append_icecast_hit_entry(entry: dict) -> None:
    cache = _load_icecast_hit_log()
    cache["entries"].append(entry)
    if len(cache["entries"]) > ICECAST_HIT_LOG_LIMIT:
        cache["entries"] = cache["entries"][-ICECAST_HIT_LOG_LIMIT:]
    try:
        with open(ICECAST_HIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        pass

def update_icecast_hit_log(title: str) -> None:
    cache = _load_icecast_hit_log()
    now = time.time()
    normalized = (title or "").strip()
    current = cache.get("current")

    if not normalized:
        if current is not None:
            duration = max(0, int(now - current["start_ts"]))
            entry = {
                "time": time.strftime("%H:%M:%S", time.localtime(now)),
                "freq": current["title"],
                "duration": duration,
            }
            _append_icecast_hit_entry(entry)
            cache["current"] = None
        return

    if current is None:
        cache["current"] = {"title": normalized, "start_ts": now}
        return

    if normalized == current["title"]:
        return

    duration = max(0, int(now - current["start_ts"]))
    entry = {
        "time": time.strftime("%H:%M:%S", time.localtime(now)),
        "freq": current["title"],
        "duration": duration,
    }
    _append_icecast_hit_entry(entry)
    cache["current"] = {"title": normalized, "start_ts": now}

def read_icecast_hit_list(limit: int = 20) -> list:
    cache = _load_icecast_hit_log()
    items = list(cache.get("entries", []))
    current = cache.get("current")
    if current is not None:
        now = time.time()
        items.append({
            "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "freq": current["title"],
            "duration": max(0, int(now - current["start_ts"])),
        })
    if not items:
        return []
    items = items[-limit:]
    items.reverse()
    return items

def parse_last_hit_freq() -> Optional[float]:
    value = read_last_hit_from_icecast() or read_last_hit()
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

def run_cmd_capture(cmd):
    try:
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        return -1, "", str(e)

def fetch_local_icecast_status():
    try:
        with urllib.request.urlopen(ICECAST_STATUS_URL, timeout=5) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return f"ERROR: {e}"

def _normalize_icecast_title(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{value}".strip()
    if isinstance(value, str):
        return value.strip()
    return ""

def extract_icecast_title(status_text: str) -> str:
    try:
        data = json.loads(status_text)
    except json.JSONDecodeError:
        return ""
    sources = data.get("icestats", {}).get("source")
    if not sources:
        return ""
    if not isinstance(sources, list):
        sources = [sources]
    for source in sources:
        listenurl = (source.get("listenurl") or "")
        mount = (source.get("mount") or "")
        if listenurl.endswith(ICECAST_MOUNT_PATH) or mount == ICECAST_MOUNT_PATH:
            for key in ("title", "streamtitle", "yp_currently_playing"):
                value = _normalize_icecast_title(source.get(key))
                if value:
                    return value
    return ""

def read_last_hit_from_icecast() -> str:
    try:
        with urllib.request.urlopen(ICECAST_STATUS_URL, timeout=1.5) as resp:
            status_text = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""
    return extract_icecast_title(status_text)

def write_diagnostic_log():
    os.makedirs(DIAGNOSTIC_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = os.path.join(DIAGNOSTIC_DIR, f"diagnostic-{ts}.txt")

    lines = []
    lines.append(f"SprontPi Diagnostic Log (UTC {ts})")
    lines.append("")
    lines.append("### icecast status-json.xsl (localhost)")
    lines.append(fetch_local_icecast_status())
    lines.append("")

    commands = [
        ["date", "-u"],
        ["uname", "-a"],
        ["uptime"],
        ["systemctl", "status", "icecast2", "--no-pager"],
        ["systemctl", "status", "icecast-keepalive", "--no-pager"],
        ["systemctl", "status", "rtl-airband", "--no-pager"],
        ["systemctl", "status", "airband-ui", "--no-pager"],
        ["journalctl", "-u", "icecast2", "-n", "200", "--no-pager"],
        ["journalctl", "-u", "icecast-keepalive", "-n", "200", "--no-pager"],
        ["journalctl", "-u", "rtl-airband", "-n", "200", "--no-pager"],
        ["journalctl", "-u", "airband-ui", "-n", "200", "--no-pager"],
        ["tail", "-n", "200", "/var/log/icecast2/error.log"],
        ["tail", "-n", "200", "/var/log/icecast2/access.log"],
        ["grep", "-n", "fallback-mount", "/etc/icecast2/icecast.xml"],
    ]

    for cmd in commands:
        lines.append(f"### command: {' '.join(cmd)}")
        code, out, err = run_cmd_capture(cmd)
        lines.append(f"(exit={code})")
        if out:
            lines.append("stdout:")
            lines.append(out.rstrip())
        if err:
            lines.append("stderr:")
            lines.append(err.rstrip())
        if not out and not err:
            lines.append("no output")
        lines.append("")

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    os.replace(tmp, path)
    return path

def restart_rtl():
    subprocess.Popen(
        ["systemctl", "restart", "--no-block", UNITS["rtl"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

def restart_ground():
    subprocess.Popen(
        ["systemctl", "restart", "--no-block", UNITS["ground"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

def stop_rtl():
    subprocess.run(
        ["systemctl", "stop", UNITS["rtl"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

def stop_ground():
    subprocess.run(
        ["systemctl", "stop", UNITS["ground"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

def start_rtl():
    subprocess.Popen(
        ["systemctl", "start", "--no-block", UNITS["rtl"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

def start_ground():
    subprocess.Popen(
        ["systemctl", "start", "--no-block", UNITS["ground"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

def set_profile(profile_id: str, current_conf_path: str, profiles, target_symlink: str):
    for pid, _, path in profiles:
        if pid == profile_id:
            if os.path.realpath(path) == os.path.realpath(current_conf_path):
                return True, False
            enforce_profile_index(path)
            subprocess.run(["ln", "-sf", path, target_symlink], check=False)
            return True, True
    return False, False

def guess_current_profile(conf_realpath: str, profiles):
    for pid, _, path in profiles:
        if os.path.realpath(path) == conf_realpath:
            return pid
    return profiles[0][0] if profiles else ""

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
            airband_gain, airband_squelch = parse_controls(conf_path)
            ground_gain, ground_squelch = parse_controls(GROUND_CONFIG_PATH)
            rtl_ok = unit_active(UNITS["rtl"])
            ground_ok = unit_active(UNITS["ground"])
            ice_ok = icecast_up()

            prof_payload, profiles_airband, profiles_ground = split_profiles()
            missing = [p["path"] for p in prof_payload if not p.get("exists")]
            profile_airband = guess_current_profile(conf_path, [(p["id"], p["label"], p["path"]) for p in profiles_airband])
            profile_ground = guess_current_profile(os.path.realpath(GROUND_CONFIG_PATH), [(p["id"], p["label"], p["path"]) for p in profiles_ground])
            icecast_hit = read_last_hit_from_icecast() if ice_ok else ""
            update_icecast_hit_log(icecast_hit)
            last_hit_airband = read_last_hit_airband()
            last_hit_ground = read_last_hit_ground()

            payload = {
                "rtl_active": rtl_ok,
                "ground_active": ground_ok,
                "icecast_active": ice_ok,
                "keepalive_active": unit_active(UNITS["keepalive"]),
                "profile_airband": profile_airband,
                "profile_ground": profile_ground,
                "profiles_airband": profiles_airband,
                "profiles_ground": profiles_ground,
                "missing_profiles": missing,
                "gain": float(airband_gain),
                "squelch": float(airband_squelch),
                "airband_gain": float(airband_gain),
                "airband_squelch": float(airband_squelch),
                "ground_gain": float(ground_gain),
                "ground_squelch": float(ground_squelch),
                "last_hit": icecast_hit or read_last_hit(),
                "last_hit_airband": last_hit_airband,
                "last_hit_ground": last_hit_ground,
                "avoids": summarize_avoids(conf_path),
            }
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        if p == "/api/hits":
            items = read_icecast_hit_list()
            if not items:
                items = read_hit_list_cached()
            payload = {"items": items}
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        return self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        p = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="ignore")
        form = {k: v[0] for k, v in parse_qs(raw).items()}

        if p == "/api/profile":
            pid = form.get("profile", "")
            target = form.get("target", "airband")
            _, profiles_airband, profiles_ground = split_profiles()
            if target == "ground":
                conf_path = os.path.realpath(GROUND_CONFIG_PATH)
                profiles = [(p["id"], p["label"], p["path"]) for p in profiles_ground]
                unit_stop = stop_ground
                unit_start = start_ground
                unit_restart = restart_ground
                target_symlink = GROUND_CONFIG_PATH
            else:
                conf_path = read_active_config_path()
                profiles = [(p["id"], p["label"], p["path"]) for p in profiles_airband]
                unit_stop = stop_rtl
                unit_start = start_rtl
                unit_restart = restart_rtl
                target_symlink = CONFIG_SYMLINK

            if not profiles:
                return self._send(400, json.dumps({"ok": False, "error": "no profiles available"}), "application/json; charset=utf-8")

            current_profile = guess_current_profile(conf_path, profiles)
            did_stop = False
            if pid and pid != current_profile:
                unit_stop()
                did_stop = True
            ok, changed = set_profile(pid, conf_path, profiles, target_symlink)
            if ok:
                if changed:
                    unit_restart()
                elif did_stop:
                    unit_start()
                return self._send(200, json.dumps({"ok": True, "changed": changed}), "application/json; charset=utf-8")
            if did_stop:
                unit_start()
            return self._send(400, json.dumps({"ok": False, "error": "unknown profile"}), "application/json; charset=utf-8")

        if p == "/api/apply":
            target = form.get("target", "airband")
            if target == "ground":
                conf_path = GROUND_CONFIG_PATH
                stop_ground()
            elif target == "airband":
                conf_path = read_active_config_path()
                stop_rtl()
            else:
                return self._send(400, json.dumps({"ok": False, "error": "unknown target"}), "application/json; charset=utf-8")
            try:
                gain = float(form.get("gain", "32.8"))
                squelch = float(form.get("squelch", "10.0"))
            except ValueError:
                if target == "ground":
                    start_ground()
                else:
                    start_rtl()
                return self._send(400, json.dumps({"ok": False, "error": "bad values"}), "application/json; charset=utf-8")

            try:
                changed = write_controls(conf_path, gain, squelch)
            except Exception as e:
                if target == "ground":
                    start_ground()
                else:
                    start_rtl()
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")

            if changed:
                if target == "ground":
                    restart_ground()
                else:
                    restart_rtl()
            else:
                if target == "ground":
                    start_ground()
                else:
                    start_rtl()
            return self._send(200, json.dumps({"ok": True, "changed": changed}), "application/json; charset=utf-8")

        if p == "/api/avoid":
            conf_path = read_active_config_path()
            stop_rtl()
            try:
                freq, err = avoid_current_hit(conf_path)
            except Exception as e:
                start_rtl()
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            if err:
                start_rtl()
                return self._send(400, json.dumps({"ok": False, "error": err}), "application/json; charset=utf-8")
            return self._send(200, json.dumps({"ok": True, "freq": f"{freq:.4f}"}), "application/json; charset=utf-8")

        if p == "/api/avoid-clear":
            conf_path = read_active_config_path()
            stop_rtl()
            try:
                _, err = clear_avoids(conf_path)
            except Exception as e:
                start_rtl()
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            if err:
                start_rtl()
                return self._send(400, json.dumps({"ok": False, "error": err}), "application/json; charset=utf-8")
            return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8")

        if p == "/api/diagnostic":
            try:
                path = write_diagnostic_log()
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            return self._send(200, json.dumps({"ok": True, "path": path}), "application/json; charset=utf-8")

        return self._send(404, json.dumps({"ok": False, "error": "not found"}), "application/json; charset=utf-8")

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

def main():
    server = ThreadedHTTPServer(("0.0.0.0", UI_PORT), Handler)
    print(f"UI listening on 0.0.0.0:{UI_PORT}")
    server.serve_forever()

if __name__ == "__main__":
    main()
