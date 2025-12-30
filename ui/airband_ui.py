#!/usr/bin/env python3
import json
import os
import re
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

UI_PORT = 5050

CONFIG_SYMLINK = "/usr/local/etc/rtl_airband.conf"
PROFILES_DIR = "/usr/local/etc/airband-profiles"

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
    ("nissan", "NISSAN STADIUM", os.path.join(PROFILES_DIR, "rtl_airband_nissan_stadium.conf")),
    ("wx",     "WX (162.550)", os.path.join(PROFILES_DIR, "rtl_airband_wx.conf")),
]

RE_GAIN = re.compile(r'^(\s*gain\s*=\s*)([0-9.]+)(\s*;\s*#\s*UI_CONTROLLED.*)$')
RE_SQL  = re.compile(r'^(\s*squelch_snr_threshold\s*=\s*)([0-9.]+)(\s*;\s*#\s*UI_CONTROLLED.*)$')

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
    .dot { width:10px; height:10px; border-radius:50%; background: var(--bad); box-shadow: 0 0 0 4px rgba(239,68,68,.12); }
    .dot.good { background: var(--good); box-shadow: 0 0 0 4px rgba(34,197,94,.12); }
    .label { font-size: 13px; color: var(--muted); }
    .val { font-size: 13px; }
    .profiles { margin-top: 12px; display:grid; gap:10px; }
    .profiles label { display:flex; align-items:center; gap:10px; padding:10px 12px; border:1px solid var(--line); border-radius:12px; cursor:pointer; background: rgba(255,255,255,.02); }
    .profiles small { color: var(--muted); display:block; margin-left: 28px; }
    .controls { margin-top: 14px; display:grid; gap:14px; }
    .ctrl { border:1px solid var(--line); border-radius:12px; padding:12px; background: rgba(255,255,255,.02); }
    .ctrl-head { display:flex; justify-content:space-between; align-items:baseline; }
    .ctrl-head b { font-size: 14px; }
    .ctrl-head span { color: var(--muted); font-size: 12px; }
    input[type="range"] { width: 100%; }
    .btns { display:flex; gap:10px; flex-wrap:wrap; margin-top: 12px; }
    button { border:1px solid var(--line); background: rgba(255,255,255,.06); color:var(--text); padding:10px 12px; border-radius:12px; cursor:pointer; }
    button.primary { background: rgba(34,197,94,.18); border-color: rgba(34,197,94,.35); }
    .foot { margin-top: 12px; color: var(--muted); font-size: 12px; }
    .warn { color: #fbbf24; font-size: 12px; margin-top: 8px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>SprontPi Radio Control</h1>

      <div class="row">
        <div class="pill"><div id="dot-rtl" class="dot"></div><div><div class="label">Scanner</div><div class="val" id="txt-rtl">…</div></div></div>
        <div class="pill"><div id="dot-ice" class="dot"></div><div><div class="label">Icecast</div><div class="val" id="txt-ice">…</div></div></div>
      </div>

      <div class="btns" style="margin-top:14px;">
        <button class="primary" id="btn-play">▶ Play</button>
        <button id="btn-refresh">↻ Refresh</button>
      </div>

      <div class="profiles" id="profiles"></div>

      <div class="controls">
        <div class="ctrl">
          <div class="ctrl-head"><b>Gain (dB)</b><span>Applied: <span id="applied-gain">…</span></span></div>
          <input id="gain" type="range" min="0" max="49.6" step="0.1" />
        </div>

        <div class="ctrl">
          <div class="ctrl-head"><b>Squelch (SNR threshold)</b><span>Applied: <span id="applied-sql">…</span></span></div>
          <input id="sql" type="range" min="0" max="30" step="0.1" />
        </div>
      </div>

      <div class="btns">
        <button class="primary" id="btn-apply">Apply</button>
      </div>

      <div class="warn" id="warn"></div>
      <div class="foot">Tip: switching profiles or applying changes restarts the scanner; playback stays alive via keepalive fallback.</div>
    </div>
  </div>

<script>
const profilesEl = document.getElementById('profiles');
const warnEl = document.getElementById('warn');

let currentProfile = null;
let sliderDirty = false;

function setDot(id, ok) {
  const el = document.getElementById(id);
  if (ok) el.classList.add('good'); else el.classList.remove('good');
}

function buildProfiles(profiles, selected) {
  profilesEl.innerHTML = '';
  profiles.forEach(p => {
    const wrap = document.createElement('label');
    const r = document.createElement('input');
    r.type = 'radio';
    r.name = 'profile';
    r.value = p.id;
    r.checked = (p.id === selected);
    r.addEventListener('change', async () => {
      await post('/api/profile', {profile: p.id});
      await refresh(true);
    });
    const text = document.createElement('div');
    text.innerHTML = `<div><b>${p.label}</b></div>` + (p.exists ? '' : `<small>Missing: ${p.path}</small>`);
    wrap.appendChild(r);
    wrap.appendChild(text);
    profilesEl.appendChild(wrap);
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

  document.getElementById('applied-gain').textContent = st.gain.toFixed(1);
  document.getElementById('applied-sql').textContent = st.squelch.toFixed(1);

  warnEl.textContent = st.missing_profiles.length ? ('Missing profile file(s): ' + st.missing_profiles.join(' • ')) : '';

  if (currentProfile === null || currentProfile !== st.profile) {
    currentProfile = st.profile;
    buildProfiles(st.profiles, st.profile);
  }

  if (allowSetSliders && !sliderDirty) {
    document.getElementById('gain').value = st.gain;
    document.getElementById('sql').value = st.squelch;
  }
}

document.getElementById('gain').addEventListener('input', ()=> sliderDirty = true);
document.getElementById('sql').addEventListener('input', ()=> sliderDirty = true);

document.getElementById('btn-refresh').addEventListener('click', async ()=> {
  sliderDirty = false;
  await refresh(true);
});

document.getElementById('btn-apply').addEventListener('click', async ()=> {
  const gain = document.getElementById('gain').value;
  const squelch = document.getElementById('sql').value;
  await post('/api/apply', {gain, squelch});
  sliderDirty = false;
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

def write_controls(conf_path: str, gain: float, squelch: float):
    # clamp
    gain = max(0.0, min(49.6, float(gain)))
    squelch = max(0.0, min(30.0, float(squelch)))

    with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    out = []
    for line in lines:
        m = RE_GAIN.match(line)
        if m:
            out.append(f"{m.group(1)}{gain:.3f}{m.group(3)}\n")
            continue
        m = RE_SQL.match(line)
        if m:
            out.append(f"{m.group(1)}{squelch:.3f}{m.group(3)}\n")
            continue
        out.append(line)

    tmp = conf_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(out)
    os.replace(tmp, conf_path)

def restart_rtl():
    subprocess.run(["systemctl", "restart", UNITS["rtl"]], check=False)

def set_profile(profile_id: str):
    for pid, _, path in PROFILES:
        if pid == profile_id:
            subprocess.run(["ln", "-sf", path, CONFIG_SYMLINK], check=False)
            return True
    return False

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
            ok = set_profile(pid)
            if ok:
                restart_rtl()
                return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8")
            return self._send(400, json.dumps({"ok": False, "error": "unknown profile"}), "application/json; charset=utf-8")

        if p == "/api/apply":
            conf_path = read_active_config_path()
            try:
                gain = float(form.get("gain", "32.8"))
                squelch = float(form.get("squelch", "10.0"))
            except ValueError:
                return self._send(400, json.dumps({"ok": False, "error": "bad values"}), "application/json; charset=utf-8")

            try:
                write_controls(conf_path, gain, squelch)
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")

            restart_rtl()
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
