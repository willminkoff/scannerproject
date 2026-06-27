#!/usr/bin/env python3
"""scannerctl — thin mobile-first web UI over SDRangel (analog) + SDRTrunk (digital).

The macOS replacement for airband-ui's *user-facing* role: the few controls Will
reaches for on his phone. Deep config stays in the native SDRangel/SDRTrunk apps;
this is the quick panel. (Path B — conversational control via Claude — complements
it.)

STATUS: skeleton. Routes + client wiring are real; the actual SDRangel field names
and SDRTrunk levers need confirming against running instances (see clients/).

Run:  SCANNERCTL_PORT=5050 SDRANGEL_REST=http://127.0.0.1:8091 python3 app.py
"""
from __future__ import annotations
import os, sys
from flask import Flask, jsonify, render_template, request

# make ../clients importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "clients"))
try:
    from sdrangel_client import SDRangel
    from sdrtrunk_client import SDRTrunk
except Exception:  # allow the skeleton to import even if clients move
    SDRangel = SDRTrunk = None

app = Flask(__name__)
SA = SDRangel(os.environ.get("SDRANGEL_REST", "http://127.0.0.1:8091")) if SDRangel else None
ST = SDRTrunk() if SDRTrunk else None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    """Unified status the mobile UI polls: analog (SDRangel REST) + digital (SDRTrunk logs)."""
    out = {"analog": {"ok": False}, "digital": {"ok": False}}
    if SA:
        inst = SA.instance_summary()
        out["analog"] = {"ok": "error" not in inst,
                         "summary": inst,
                         "devicesets": SA.devicesets() if "error" not in inst else None}
    if ST:
        out["digital"] = {"ok": ST.is_running(),
                          "running": ST.is_running(),
                          "recent": ST.recent_activity(20)}
    return jsonify(out)


# ---- analog (SDRangel) controls ----
@app.route("/api/scan/<onoff>", methods=["POST"])
def scan(onoff):
    if not SA:
        return jsonify({"error": "no SDRangel client"}), 503
    ds = int(request.args.get("deviceset", 0)); ch = int(request.args.get("channel", 2))  # FreqScanner usually R0:2
    return jsonify(SA.scanner_set_running(ds, ch, onoff == "start"))


@app.route("/api/squelch", methods=["POST"])
def squelch():
    if not SA:
        return jsonify({"error": "no SDRangel client"}), 503
    b = request.get_json(force=True, silent=True) or {}
    return jsonify(SA.set_squelch(int(b.get("deviceset", 0)), int(b.get("channel", 0)), float(b.get("dbfs", -55))))


# ---- digital (SDRTrunk) — coarse: restart with a new playlist ----
@app.route("/api/digital/restart", methods=["POST"])
def digital_restart():
    if not ST:
        return jsonify({"error": "no SDRTrunk client"}), 503
    ST.restart()
    return jsonify({"ok": True, "note": "SDRTrunk kickstarted (reloads playlist on start)"})


if __name__ == "__main__":
    port = int(os.environ.get("SCANNERCTL_PORT", "5050"))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("SCANNERCTL_DEBUG")))
