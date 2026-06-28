#!/usr/bin/env python3
"""scannerctl — thin mobile-first web UI over the macOS SDR backend.

The macOS replacement for airband-ui's *user-facing* role: the glanceable panel + quick
controls Will reaches for on his phone. Deep config stays in the native SDRangel/SDRTrunk
apps; conversational control via Claude (Path B) complements it.

Three layers:
  FLEET   — single-consumer reality: SDRangel (analog) and SDRTrunk (digital) share one
            SDRplay apiService and must not run at once. Switching is done through `sdrctl`
            (the arbiter), which enforces single-consumer + the restart throttle.
  ANALOG  — SDRangel REST (:8091): live device/freq read; scan + squelch writes (best-effort).
  DIGITAL — SDRTrunk decode read from event_logs (live call feed + CC health); coarse control
            is "reload playlist" (restart via sdrctl).

Run:  SCANNERCTL_PORT=5050 SDRANGEL_REST=http://127.0.0.1:8091 python3 app.py
"""
from __future__ import annotations
import os, subprocess, sys
from flask import Flask, jsonify, render_template, request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "clients"))
try:
    from sdrangel_client import SDRangel
    from sdrtrunk_client import SDRTrunk
except Exception:
    SDRangel = SDRTrunk = None

SDRCTL = os.path.abspath(os.path.join(HERE, "..", "bin", "sdrctl"))
APISERVICE = "sdrplay_apiService"
MATCH = {"sdrangel": "SDRangel.app/Contents/MacOS/SDRangel", "sdrtrunk": "io.github.dsheirer"}

# explicit root_path + instance_path so Flask never probes os.getcwd() (fails in sandboxed runners)
app = Flask(__name__, root_path=HERE, instance_path=os.path.join(HERE, "instance"))
SA = SDRangel(os.environ.get("SDRANGEL_REST", "http://127.0.0.1:8091")) if SDRangel else None
ST = SDRTrunk() if SDRTrunk else None


# ----------------------------------------------------------------------------- fleet
def _proc_up(pat: str, exact: bool = False) -> bool:
    flag = "-x" if exact else "-f"
    return subprocess.run(["pgrep", flag, pat], capture_output=True).returncode == 0

def fleet_status() -> dict:
    sa = _proc_up(MATCH["sdrangel"]); st = _proc_up(MATCH["sdrtrunk"])
    return {"apiservice": _proc_up(APISERVICE, exact=True), "sdrangel": sa, "sdrtrunk": st}

def _sdrctl(action: str, consumer: str) -> None:
    """Fire sdrctl in the background — start/stop can take 20-60s (verify + restart throttle),
    so we never block the web request. The next status poll reflects the result."""
    try:
        with open("/tmp/scannerctl-sdrctl.log", "ab") as log:
            subprocess.Popen(["/usr/bin/python3", SDRCTL, action, consumer],
                             stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    except OSError as e:
        app.logger.warning("sdrctl %s %s failed: %s", action, consumer, e)


# ----------------------------------------------------------------------------- analog read
def analog_summary() -> dict:
    """Compact SDRangel view: per device-set, device type + center freq + channel demods."""
    if not SA:
        return {"ok": False, "error": "no client"}
    inst = SA.instance_summary()
    if "error" in inst:
        return {"ok": False, "error": inst["error"]}
    sets = []
    for ds in (inst.get("devicesetlist", {}).get("deviceSets", []) or []):
        sd = ds.get("samplingDevice", {}) or {}            # device fields live under samplingDevice
        chans = ds.get("channels", []) or []
        sets.append({
            "hw": sd.get("hwType") or "?",
            "serial": sd.get("serial"),
            "state": sd.get("state"),                       # running / idle
            "center_mhz": round((sd.get("centerFrequency") or 0) / 1e6, 4) or None,
            "channels": [c.get("title") or c.get("id") for c in chans],
            "nchan": ds.get("channelcount", len(chans)),
        })
    return {"ok": True, "version": inst.get("version"), "sets": sets}


# ----------------------------------------------------------------------------- routes
@app.after_request
def _no_cache(resp):
    # live dashboard — never let the browser/phone serve a stale page or status
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def status():
    fleet = fleet_status()
    out = {"fleet": fleet, "analog": None, "digital": None}
    if fleet["sdrangel"]:
        out["analog"] = analog_summary()
    if fleet["sdrtrunk"] and ST:
        out["digital"] = {"ok": True, "system": ST.system_info(),
                          "cc": ST.cc_health(), "calls": ST.recent_calls(25)}
    # Real invariant is ONE SDRplay-apiService (RSPduo) consumer. SDRTrunk always is; SDRangel is
    # only if it has an RSP/SDRplay device set. SDRangel on RTL-SDRs coexists with SDRTrunk fine.
    analog_uses_rsp = any(("RSP" in (s.get("hw") or "")) or ("SDRplay" in (s.get("hw") or ""))
                          for s in ((out["analog"] or {}).get("sets") or []))
    consumers = (1 if fleet["sdrtrunk"] else 0) + (1 if analog_uses_rsp else 0)
    fleet["conflict"] = consumers > 1                                  # genuine apiService contention
    fleet["coexist"] = fleet["sdrangel"] and fleet["sdrtrunk"] and not fleet["conflict"]
    fleet["active"] = ("sdrangel" if fleet["sdrangel"] else None) if not fleet["sdrtrunk"] \
        else ("sdrtrunk" if not fleet["sdrangel"] else "both")
    return jsonify(out)

# ---- fleet control (single-consumer, via sdrctl) ----
@app.route("/api/fleet/start/<consumer>", methods=["POST"])
def fleet_start(consumer):
    if consumer not in ("sdrangel", "sdrtrunk"):
        return jsonify({"error": "unknown consumer"}), 400
    _sdrctl("start", consumer)
    return jsonify({"ok": True, "starting": consumer,
                    "note": "sdrctl stops the other consumer first; allow ~20-30s"})

@app.route("/api/fleet/stop/<consumer>", methods=["POST"])
def fleet_stop(consumer):
    if consumer not in ("sdrangel", "sdrtrunk", "all"):
        return jsonify({"error": "unknown consumer"}), 400
    _sdrctl("stop", consumer)
    return jsonify({"ok": True, "stopping": consumer})

# ---- analog (SDRangel) controls — best-effort writes (verify channel layout) ----
@app.route("/api/scan/<onoff>", methods=["POST"])
def scan(onoff):
    if not SA:
        return jsonify({"error": "no SDRangel client"}), 503
    ds = int(request.args.get("deviceset", 0)); ch = int(request.args.get("channel", 2))
    return jsonify(SA.scanner_set_running(ds, ch, onoff == "start"))

@app.route("/api/squelch", methods=["POST"])
def squelch():
    if not SA:
        return jsonify({"error": "no SDRangel client"}), 503
    b = request.get_json(force=True, silent=True) or {}
    return jsonify(SA.set_squelch(int(b.get("deviceset", 0)), int(b.get("channel", 0)), float(b.get("dbfs", -55))))

# ---- digital (SDRTrunk) — coarse: reload playlist (restart via sdrctl) ----
@app.route("/api/digital/restart", methods=["POST"])
def digital_restart():
    _sdrctl("restart", "sdrtrunk")
    return jsonify({"ok": True, "note": "SDRTrunk restarting via sdrctl (reloads playlist on start)"})


if __name__ == "__main__":
    port = int(os.environ.get("SCANNERCTL_PORT", "5050"))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("SCANNERCTL_DEBUG")))
