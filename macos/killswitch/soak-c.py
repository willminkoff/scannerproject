#!/usr/bin/env python3
# ============================================================================
# soak-c.py — 20-minute soak test for fleet-policy arrangement C (revision 4.1)
#
#   DIGITAL  : SDRTrunk P25 on the remaining RSP (expected serial 1809063632),
#              native SDRplay API, dual-tuner.
#   AIRBAND  : chirp AM on RTL 83241970  -> icecast /ANALOG.mp3
#   GROUND   : chirp NFM on RTL 61108285 -> icecast /ANALOG_GROUND.mp3
#
# WHAT IT DOES
#   1. Confirms the expected RSP serial is actually attached (SoapySDRUtil).
#      Aborts with a clear message if it isn't.
#   2. Brings up scanner mode via sdr-killswitch (or attaches if already up).
#   3. Samples for --duration seconds (default 1200 = 20 min):
#        digital : SDRTrunk decode/call/sync activity, tuner presence, broker
#                  lease stability on the RSP, 0x6bed / apiService errors.
#        analog  : each mount live the whole window + audio level (ffmpeg
#                  volumedetect) with the broadband-noise-signature check;
#                  broker lease stability on each RTL; chirp health metrics.
#        general : launchd agent crashes/respawns + exit codes, icecast mounts
#                  present, CPU load + thermal throttling.
#   4. Prints a per-band PASS/FAIL summary with evidence + an overall verdict,
#      and writes a timestamped log file. A clearly delimited PASTE-BACK block
#      is printed at the end for you to copy to us.
#
# HONEST LIMITS (things a script cannot judge — you must):
#   * Subjective AUDIO QUALITY. The script detects the broadband-NOISE failure
#     signature and confirms the mount is live, but it cannot tell "clear speech"
#     from "garbled/over-driven but non-noise." LISTEN to /ANALOG.mp3 and
#     /ANALOG_GROUND.mp3 during the run (open them in the sb5 player or VLC).
#   * P25 audio intelligibility. The script counts decode/call events; it can't
#     confirm the voice sounds right. Spot-listen to /DIGITAL.mp3.
#   * If a data source is missing (SDRTrunk log path, chirp metrics port, etc.)
#     the script marks that signal UNAVAILABLE and fails the affected band
#     conservatively rather than guessing.
#
# It does NOT modify any config. Safe to run repeatedly.
# ============================================================================
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# config / defaults (env-overridable so it works on the real box)
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
KILLSWITCH = os.environ.get("KILLSWITCH", os.path.join(HERE, "sdr-killswitch"))

# SoapySDRUtil binary. QUICK FIX for "SoapySDRUtil not found on PATH": point this
# at your install, e.g. SOAK_SOAPYSDRUTIL=/opt/homebrew/bin/SoapySDRUtil. It is
# OPTIONAL — the RSP-serial preflight also works without Soapy (see preflight).
SOAK_SOAPYSDRUTIL = os.environ.get("SOAK_SOAPYSDRUTIL", "")
SOAPY_CANDIDATES = [p for p in (
    SOAK_SOAPYSDRUTIL,
    "SoapySDRUtil",                        # PATH
    "/opt/homebrew/bin/SoapySDRUtil",      # Homebrew, Apple Silicon
    "/usr/local/bin/SoapySDRUtil",         # Homebrew, Intel
) if p]

EXPECT_RSP = os.environ.get("SOAK_RSP_SERIAL", "1809063632")   # the remaining RSP
RTL_AIRBAND = os.environ.get("SOAK_RTL_AIRBAND", "83241970")
RTL_GROUND = os.environ.get("SOAK_RTL_GROUND", "61108285")

ICECAST = os.environ.get("SOAK_ICECAST", "http://127.0.0.1:8000")
MOUNT_AIR = "/ANALOG.mp3"
MOUNT_GND = "/ANALOG_GROUND.mp3"
MOUNT_DIG = "/DIGITAL.mp3"

CHIRP_METRICS_AIR = os.environ.get("SOAK_CHIRP_AIR", "http://127.0.0.1:9101/metrics")
CHIRP_METRICS_GND = os.environ.get("SOAK_CHIRP_GND", "http://127.0.0.1:9102/metrics")

SDRTRUNK_HOME = os.environ.get("SDRTRUNK_HOME", os.path.expanduser("~/SDRTrunk"))
SDRTRUNK_LOGDIR = os.environ.get("SDRTRUNK_LOG", os.path.join(SDRTRUNK_HOME, "logs"))
SDRTRUNK_OUT = os.environ.get("SDRTRUNK_OUT", "/tmp/sdrtrunk.out.log")
SDRTRUNK_ERR = os.environ.get("SDRTRUNK_ERR", "/tmp/sdrtrunk.err.log")

BROKER_PY = os.environ.get("BROKER_PY", "/opt/scannerproject/venv/bin/python")
REPO_DIR = os.environ.get("SCANNERPROJECT_DIR", "/opt/scannerproject/app")
BROKER_SOCKET = os.environ.get("SCANNER_BROKER_SOCKET", "/opt/scannerproject/run/broker.sock")

AGENTS = [
    "com.scannerproject.tuner-broker",
    "com.scannerproject.icecast",
    "com.scannerproject.chirp-airband",
    "com.scannerproject.chirp-ground",
    "com.scannerproject.sdrtrunk",
    "com.scannerproject.airband-ui",
]

# broadband-noise signature: loud AND flat (no dynamic range / no squelch gaps)
NOISE_MEAN_DBFS = float(os.environ.get("SOAK_NOISE_MEAN_DBFS", "-20.0"))   # louder than this ...
NOISE_FLAT_DB = float(os.environ.get("SOAK_NOISE_FLAT_DB", "6.0"))         # ... and max-mean tighter than this
SILENT_MEAN_DBFS = float(os.environ.get("SOAK_SILENT_DBFS", "-50.0"))     # quieter than this = squelch-closed

# ANSI
def c(s, code):
    return s if os.environ.get("NO_COLOR") else f"\033[{code}m{s}\033[0m"
BOLD = lambda s: c(s, "1")
GREEN = lambda s: c(s, "32")
RED = lambda s: c(s, "31")
YEL = lambda s: c(s, "33")

LOG_LINES: list[str] = []
PREFLIGHT_NOTES: list[str] = []   # serial-confirmation status, surfaced in the paste-back
def log(msg: str = "") -> None:
    print(msg)
    LOG_LINES.append(re.sub(r"\033\[[0-9;]*m", "", msg))

def run(cmd, timeout=25):
    """Run a command; return (rc, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, text=True)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout: {' '.join(cmd)}"
    except Exception as e:  # noqa: BLE001
        return 1, "", f"{type(e).__name__}: {e}"

# ---------------------------------------------------------------------------
# parsers (unit-tested by --selftest; kept pure so they can run without hardware)
# ---------------------------------------------------------------------------
def parse_soapy_serials(text: str) -> list[str]:
    """Serials from `SoapySDRUtil --find` output (lines like `serial = 1809063632`)."""
    return re.findall(r"serial\s*=\s*([0-9A-Fa-f]+)", text)

def parse_usb_sdrplay(text: str):
    """From `system_profiler SPUSBDataType`: (device_present, [serials]).
    SDRplay = USB vendor 0x1df7; the RSPduo shows as an SDRplay/RSP device.
    system_profiler prints the device NAME line and its 'Serial Number:' a few
    lines apart (often across a blank line), so we open a short line-window when
    we see an SDRplay marker and harvest any Serial Number inside it. The serial
    may not be exposed at all on some firmware — then serials is empty but
    present is still True."""
    present = bool(re.search(r"sdrplay|rspduo|rspdx|0x1df7|\b1df7\b", text, re.I))
    serials: list[str] = []
    window = 0
    for ln in text.splitlines():
        if re.search(r"sdrplay|rspduo|rspdx|1df7", ln, re.I):
            window = 20
        m = re.search(r"Serial Number:\s*([0-9A-Za-z]{6,})", ln)
        if m and window > 0 and m.group(1) not in serials:
            serials.append(m.group(1))
        if window > 0:
            window -= 1
    return present, serials

def parse_sdrtrunk_serials(text: str) -> list[str]:
    """Serials SDRTrunk actually discovered, from its 'SER#<serial>' tuner ids."""
    return re.findall(r"SER#([0-9A-Fa-f]{6,})", text)

def parse_volumedetect(stderr: str):
    """(mean_dbfs, max_dbfs) from ffmpeg volumedetect stderr, or (None, None)."""
    mean = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", stderr)
    mx = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", stderr)
    return (float(mean.group(1)) if mean else None,
            float(mx.group(1)) if mx else None)

def classify_audio(mean, mx):
    """-> 'noise' | 'signal' | 'silent' | 'unknown' for an analog sample."""
    if mean is None or mx is None:
        return "unknown"
    if mean > NOISE_MEAN_DBFS and (mx - mean) < NOISE_FLAT_DB:
        return "noise"           # loud + flat = the MA-mode broadband-noise failure
    if mean < SILENT_MEAN_DBFS:
        return "silent"          # squelch closed (normal between transmissions)
    return "signal"              # audible, dynamic content

def parse_prom_metric(text: str, name: str):
    """Sum of all series for a Prometheus metric name (labels ignored). None if absent."""
    vals = re.findall(rf"^{re.escape(name)}(?:\{{[^}}]*\}})?\s+([0-9eE.+-]+)\s*$",
                      text, flags=re.MULTILINE)
    if not vals:
        return None
    try:
        return sum(float(v) for v in vals)
    except ValueError:
        return None

def parse_broker_leases(text: str) -> dict:
    """serial -> lease_id from `broker.client status` JSON. {} on any trouble."""
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return {}
    snap = data.get("status", data)
    leases = snap.get("leases") if isinstance(snap, dict) else None
    out = {}
    if isinstance(leases, list):
        for L in leases:
            if isinstance(L, dict) and L.get("serial"):
                out[str(L["serial"])] = str(L.get("lease_id", ""))
    return out

def parse_launchctl_list(text: str, label: str):
    """(pid, last_exit) from `launchctl list` table. pid None if not loaded."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-1] == label:
            pid = None if parts[0] in ("-", "") else _int(parts[0])
            ex = _int(parts[1])
            return pid, ex
    return None, None

def parse_icecast_mounts(text: str) -> set:
    """Set of mount paths currently live per icecast status-json.xsl."""
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return set()
    src = (data.get("icestats") or {}).get("source")
    if src is None:
        return set()
    if isinstance(src, dict):
        src = [src]
    out = set()
    for s in src:
        lu = s.get("listenurl") or ""
        m = re.search(r"(/[^/]+\.mp3)$", lu)
        if m:
            out.add(m.group(1))
        elif s.get("mount"):
            out.add(s["mount"])
    return out

def count_sdrtrunk_decodes(text: str) -> int:
    """Heuristic count of P25 decode/call/sync activity lines in a SDRTrunk log.
    SDRTrunk's exact format varies by version — this matches the common tokens."""
    pat = re.compile(r"(CALL|DECODE|TALKGROUP|talkgroup|P25|sync|SYNC|control|CC:|traffic)",
                     re.IGNORECASE)
    return sum(1 for ln in text.splitlines() if pat.search(ln))

def count_errors(text: str) -> dict:
    """Count the failure signatures we care about in a log blob."""
    return {
        "0x6bed": len(re.findall(r"0x6bed", text, re.IGNORECASE)),
        "apiservice": len(re.findall(r"apiservice|sdrplay_api\w*\s*(error|fail)",
                                     text, re.IGNORECASE)),
        "tuner_lost": len(re.findall(r"tuner.*(lost|unavailable|error|removed)",
                                     text, re.IGNORECASE)),
        "broker_denied": len(re.findall(r"claim DENIED|BrokerDenied", text)),
    }

def _int(s):
    try:
        return int(s)
    except (ValueError, TypeError):
        return None

# ---------------------------------------------------------------------------
# live probes (call the parsers on real command output)
# ---------------------------------------------------------------------------
def probe_soapy():
    """Try SoapySDRUtil at the configured path, then Homebrew, then PATH.
    Returns (serials|None, raw_output, resolved_exe_or_reason)."""
    for cand in SOAPY_CANDIDATES:
        exe = shutil.which(cand) or (cand if os.path.exists(cand) else None)
        if not exe:
            continue
        rc, out, err = run([exe, "--find=driver=sdrplay"], timeout=30)
        if rc == 127:
            continue
        return parse_soapy_serials(out + err), (out + err).strip(), exe
    return None, "", "SoapySDRUtil not found (tried: " + ", ".join(SOAPY_CANDIDATES) + ")"

def probe_usb_sdrplay():
    """(device_present, [serials]) from system_profiler — no SoapySDR needed.
    This is what macos/install/post-install-checks.sh uses to see the RSPduo."""
    rc, out, _ = run(["system_profiler", "SPUSBDataType"], timeout=40)
    if rc != 0:
        return False, []
    return parse_usb_sdrplay(out)

def probe_sdrtrunk_discovered_serials():
    """Serials SDRTrunk itself discovered, scraped from its runtime logs (the
    digital engine's own view of the RSP — uses the native SDRplay API, not Soapy)."""
    blob = ""
    try:
        import glob
        logs = sorted(glob.glob(os.path.join(SDRTRUNK_LOGDIR, "*.log")),
                      key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
        if logs:
            with open(logs[-1], "r", errors="ignore") as f:
                blob += f.read()[-300000:]
    except Exception:  # noqa: BLE001
        pass
    for p in (SDRTRUNK_OUT, SDRTRUNK_ERR):
        try:
            with open(p, "r", errors="ignore") as f:
                blob += "\n" + f.read()[-150000:]
        except Exception:  # noqa: BLE001
            pass
    return parse_sdrtrunk_serials(blob)

def probe_broker():
    if not os.path.exists(BROKER_PY):
        return {}, f"{BROKER_PY} missing"
    rc, out, err = run([BROKER_PY, "-m", "broker.client", "status",
                        "--socket", BROKER_SOCKET], timeout=15)
    if rc != 0:
        return {}, (err or out).strip()[:200]
    return parse_broker_leases(out), ""

def probe_metrics(url):
    rc, out, _ = run(["curl", "-s", "-m", "6", url], timeout=10)
    return out if rc == 0 else ""

def probe_icecast():
    rc, out, _ = run(["curl", "-s", "-m", "6", f"{ICECAST}/status-json.xsl"], timeout=10)
    return parse_icecast_mounts(out) if rc == 0 else set()

def probe_launchd():
    rc, out, _ = run(["launchctl", "list"], timeout=10)
    return {a: parse_launchctl_list(out, a) for a in AGENTS}

def probe_audio(mount, secs):
    """(classification, mean, max) for a live icecast mount via ffmpeg volumedetect."""
    url = f"{ICECAST}{mount}"
    rc, _, err = run(["ffmpeg", "-nostats", "-hide_banner", "-i", url,
                      "-t", str(secs), "-af", "volumedetect", "-f", "null", "-"],
                     timeout=secs + 20)
    if rc == 127:
        return "unknown", None, None
    mean, mx = parse_volumedetect(err)
    return classify_audio(mean, mx), mean, mx

def probe_sdrtrunk():
    """(decode_count, errors_dict). Scans newest log + the launchd out/err logs."""
    blob = ""
    try:
        import glob
        logs = sorted(glob.glob(os.path.join(SDRTRUNK_LOGDIR, "*.log")),
                      key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
        if logs:
            with open(logs[-1], "r", errors="ignore") as f:
                blob += f.read()[-200000:]
    except Exception:  # noqa: BLE001
        pass
    for p in (SDRTRUNK_OUT, SDRTRUNK_ERR):
        try:
            with open(p, "r", errors="ignore") as f:
                blob += "\n" + f.read()[-100000:]
        except Exception:  # noqa: BLE001
            pass
    return count_sdrtrunk_decodes(blob), count_errors(blob), bool(blob)

def probe_system():
    load = None
    rc, out, _ = run(["uptime"])
    m = re.search(r"load averages?:\s*([\d.]+)", out)
    if m:
        load = float(m.group(1))
    throttled = False
    therm = ""
    rc, out, _ = run(["pmset", "-g", "therm"])
    if rc == 0:
        therm = out.strip()
        m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
        if m and int(m.group(1)) < 100:
            throttled = True
    return load, throttled, therm

# ---------------------------------------------------------------------------
# selftest — verify the parsers without any hardware
# ---------------------------------------------------------------------------
def selftest() -> int:
    ok = True
    def check(name, cond):
        nonlocal ok
        print(("  PASS " if cond else "  FAIL ") + name)
        ok = ok and cond

    check("soapy serials", parse_soapy_serials(
        "Found device 0\n  driver = sdrplay\n  serial = 1809063632\n") == ["1809063632"])
    present, us = parse_usb_sdrplay(
        "        SDRplay RSPduo:\n\n          Product ID: 0x3020\n"
        "          Vendor ID: 0x1df7  (SDRplay Limited)\n          Serial Number: 1809063632\n")
    check("usb sdrplay present+serial", present and us == ["1809063632"])
    check("usb no-sdrplay", parse_usb_sdrplay("Some Hub:\n  Serial Number: ABC123\n") == (False, []))
    check("sdrtrunk discovered serial",
          parse_sdrtrunk_serials("Added tuner RSPduo Tuner 1 SER#1809063632\n") == ["1809063632"])
    mean, mx = parse_volumedetect("[Parsed] mean_volume: -34.2 dB\nmax_volume: -8.0 dB\n")
    check("volumedetect parse", mean == -34.2 and mx == -8.0)
    check("classify noise", classify_audio(-6.0, -2.0) == "noise")     # loud+flat
    check("classify signal", classify_audio(-30.0, -3.0) == "signal")  # dynamic
    check("classify silent", classify_audio(-70.0, -55.0) == "silent") # squelch
    check("prom metric sum", parse_prom_metric(
        'chirp_audio_bytes_published_total{mount="/ANALOG.mp3"} 42\n',
        "chirp_audio_bytes_published_total") == 42.0)
    check("prom canary", parse_prom_metric(
        'chirp_config_load_status{daemon="airband"} 1\n', "chirp_config_load_status") == 1.0)
    leases = parse_broker_leases(json.dumps(
        {"status": {"leases": [{"serial": "1809063632", "lease_id": "L1", "consumer": "sdrtrunk"}]}}))
    check("broker leases", leases.get("1809063632") == "L1")
    pid, ex = parse_launchctl_list("PID\tStatus\tLabel\n1234\t0\tcom.scannerproject.sdrtrunk\n",
                                   "com.scannerproject.sdrtrunk")
    check("launchctl parse", pid == 1234 and ex == 0)
    check("icecast mounts", parse_icecast_mounts(json.dumps(
        {"icestats": {"source": [{"listenurl": "http://x:8000/ANALOG.mp3"}]}})) == {"/ANALOG.mp3"})
    check("sdrtrunk decode count", count_sdrtrunk_decodes(
        "12:00 CALL tg=1001\n12:01 idle\n12:02 P25 sync\n") == 2)
    check("error scan", count_errors("boom 0x6bed segfault")["0x6bed"] == 1)
    print(GREEN("selftest OK") if ok else RED("selftest FAILED"))
    return 0 if ok else 1

# ---------------------------------------------------------------------------
# main soak
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="20-minute soak test for arrangement C")
    ap.add_argument("--duration", type=int, default=1200, help="soak seconds (default 1200 = 20 min)")
    ap.add_argument("--sample-every", type=int, default=30, help="state sample interval s (default 30)")
    ap.add_argument("--audio-every", type=int, default=120, help="audio sample interval s (default 120)")
    ap.add_argument("--audio-secs", type=int, default=6, help="seconds of audio per sample (default 6)")
    ap.add_argument("--warmup", type=int, default=45, help="settle seconds after bring-up before measuring")
    ap.add_argument("--two-systems", action="store_true", help="expect BOTH RSP tuners decoding")
    ap.add_argument("--no-bringup", action="store_true", help="attach to a running stack; don't call the killswitch")
    ap.add_argument("--skip-serial-check", action="store_true",
                    help="proceed even if the RSP serial can't be confirmed by any method "
                         "(logs a loud 'serial NOT independently confirmed' warning)")
    ap.add_argument("--logdir", default=os.path.join(HERE, "soak-logs"), help="where to write the log file")
    ap.add_argument("--selftest", action="store_true", help="verify parsers offline and exit")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if sys.platform != "darwin":
        log(RED("This soak drives the mini's RF stack — run it on the Mac, not elsewhere."))
        return 2

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log(BOLD(f"soak-c  arrangement C (revision 4.1)  start {ts}  duration {args.duration}s"))
    log(f"expect RSP {EXPECT_RSP} · airband RTL {RTL_AIRBAND} · ground RTL {RTL_GROUND}")
    log("")

    # ---- preflight: confirm the remaining RSP, trying multiple methods ----
    # This box runs digital via SDRTrunk's NATIVE SDRplay API, so SoapySDR may
    # not be installed at all. Try Soapy, then fall back to USB enumeration and
    # SDRTrunk's own discovered-tuner logs. Only abort if NOTHING sees an RSP
    # (and even then, --skip-serial-check overrides).
    log(BOLD("preflight: confirm the RSP (serial " + EXPECT_RSP + ")"))
    serial_confirmed = False      # the EXACT expected serial was seen
    device_present = False        # some RSPduo is present (serial maybe unreadable)
    serial_method = "none"
    all_serials: list = []

    # method (a): SoapySDRUtil (optional)
    ss, raw, exe = probe_soapy()
    if ss is not None:
        device_present = device_present or bool(ss)
        all_serials += ss
        log(f"  [soapy]      {exe} -> {ss or '(no sdrplay devices)'}")
        if EXPECT_RSP in ss:
            serial_confirmed, serial_method = True, "SoapySDRUtil"
    else:
        log(YEL(f"  [soapy]      {raw} — skipping (not required)"))

    # method (b1): USB enumeration (system_profiler) — what post-install-checks uses
    if not serial_confirmed:
        usb_present, usb_serials = probe_usb_sdrplay()
        device_present = device_present or usb_present
        all_serials += usb_serials
        log(f"  [usb]        system_profiler: sdrplay_present={usb_present} serials={usb_serials or '(none exposed)'}")
        if EXPECT_RSP in usb_serials:
            serial_confirmed, serial_method = True, "system_profiler(USB)"

    # method (b2): SDRTrunk's own discovered-tuner logs (native SDRplay API view)
    if not serial_confirmed:
        st_serials = probe_sdrtrunk_discovered_serials()
        if st_serials:
            device_present = True
            all_serials += st_serials
            log(f"  [sdrtrunk]   discovered SER# in logs: {st_serials}")
            if EXPECT_RSP in st_serials:
                serial_confirmed, serial_method = True, "SDRTrunk logs"
        else:
            log("  [sdrtrunk]   no SER# in logs yet (SDRTrunk may not have run since boot)")

    # apiService liveness (informational — presence of the daemon, not the serial)
    rc, _, _ = run(["pgrep", "-x", "sdrplay_apiService"])
    log(f"  [apiservice] sdrplay_apiService running: {rc == 0}")

    others = sorted({s for s in all_serials if s and s != EXPECT_RSP})
    if others:
        log(YEL(f"  WARNING: a SECOND SDRplay serial appeared {others} — two RSPs on one host "
                f"is toxic. Confirm only {EXPECT_RSP} is attached before trusting this soak."))

    # decide
    if serial_confirmed:
        log(GREEN(f"  OK: RSP {EXPECT_RSP} confirmed via {serial_method}."))
        PREFLIGHT_NOTES.append(f"RSP {EXPECT_RSP} confirmed via {serial_method}")
    elif device_present:
        msg = (f"an RSPduo IS present but its serial could not be read as {EXPECT_RSP} "
               f"by any method — proceeding, serial NOT independently confirmed")
        log(YEL("  WARNING: " + msg + "."))
        log(       "           (If the serial differs, set SOAK_RSP_SERIAL and update the policy.)")
        PREFLIGHT_NOTES.append("WARNING: " + msg)
    elif args.skip_serial_check:
        msg = (f"NO method could see any RSP, but --skip-serial-check was given — proceeding; "
               f"RSP serial NOT independently confirmed")
        log(YEL("  WARNING: " + msg + "."))
        PREFLIGHT_NOTES.append("WARNING: " + msg)
    else:
        log(RED(f"  ABORT: no method could confirm an RSP is attached."))
        log(     "         Tried: SoapySDRUtil, system_profiler USB, SDRTrunk logs.")
        log(     "         Quick fixes:")
        log(     "           • if SoapySDRUtil exists: SOAK_SOAPYSDRUTIL=/opt/homebrew/bin/SoapySDRUtil "
                 "python3 macos/killswitch/soak-c.py")
        log(     "           • confirm the RSP by hand: system_profiler SPUSBDataType | grep -i -A6 sdrplay")
        log(     "           • or bypass the check: add --skip-serial-check")
        return 3
    log("")

    # ---- bring up scanner mode ----
    if not args.no_bringup:
        log(BOLD("bringing up scanner mode via sdr-killswitch"))
        if os.access(KILLSWITCH, os.X_OK):
            rc, out, err = run([KILLSWITCH, "scanner"], timeout=120)
            for ln in (out or "").splitlines():
                log("  " + ln)
            if rc != 0:
                log(YEL(f"  killswitch scanner returned {rc}; continuing to measure what's up"))
        else:
            log(YEL(f"  killswitch not executable at {KILLSWITCH}; assuming stack already up"))
    else:
        log("attach mode (--no-bringup): measuring the running stack")
    log(f"warmup {args.warmup}s for mounts + decoders to settle…")
    time.sleep(args.warmup)
    log("")

    # ---- sampling ----
    start = time.time()
    n_state = 0
    n_audio = 0
    last_audio = 0.0

    lease_first: dict = {}
    lease_drops: dict = {EXPECT_RSP: 0, RTL_AIRBAND: 0, RTL_GROUND: 0}
    lease_last: dict = {}
    lease_missing: dict = {EXPECT_RSP: 0, RTL_AIRBAND: 0, RTL_GROUND: 0}

    agent_pid_first: dict = {}
    agent_respawns: dict = {a: 0 for a in AGENTS}
    agent_bad_exit: dict = {a: 0 for a in AGENTS}
    agent_pid_last: dict = {}

    mount_missing = {MOUNT_AIR: 0, MOUNT_GND: 0, MOUNT_DIG: 0}

    dec_series: list = []          # (elapsed, sdrtrunk decode count)
    err_totals = {"0x6bed": 0, "apiservice": 0, "tuner_lost": 0, "broker_denied": 0}
    sdrtrunk_seen = False

    audio = {MOUNT_AIR: [], MOUNT_GND: [], MOUNT_DIG: []}   # list of (class, mean, max)

    chirp_alive = {"airband": [], "ground": []}
    chirp_canary = {"airband": [], "ground": []}
    chirp_bytes = {"airband": [], "ground": []}

    load_series: list = []
    throttled_ever = False
    therm_last = ""

    log(BOLD("sampling… (Ctrl-C to stop early and still get a verdict)"))
    try:
        while time.time() - start < args.duration:
            elapsed = int(time.time() - start)

            # broker leases
            leases, berr = probe_broker()
            for serial in (EXPECT_RSP, RTL_AIRBAND, RTL_GROUND):
                lid = leases.get(serial)
                if lid is None:
                    lease_missing[serial] += 1
                else:
                    lease_first.setdefault(serial, lid)
                    if serial in lease_last and lease_last[serial] != lid:
                        lease_drops[serial] += 1     # lease_id changed = drop+reclaim
                    lease_last[serial] = lid

            # launchd agents
            for a, (pid, ex) in probe_launchd().items():
                if pid is not None:
                    agent_pid_first.setdefault(a, pid)
                    if a in agent_pid_last and agent_pid_last[a] != pid:
                        agent_respawns[a] += 1
                    agent_pid_last[a] = pid
                if ex not in (None, 0):
                    agent_bad_exit[a] += 1

            # icecast mounts
            live = probe_icecast()
            for m in mount_missing:
                if m not in live:
                    mount_missing[m] += 1

            # sdrtrunk decode + errors
            dc, errs, seen = probe_sdrtrunk()
            sdrtrunk_seen = sdrtrunk_seen or seen
            dec_series.append((elapsed, dc))
            for k in err_totals:
                err_totals[k] = max(err_totals[k], errs.get(k, 0))   # cumulative in-log

            # chirp metrics
            for band, url in (("airband", CHIRP_METRICS_AIR), ("ground", CHIRP_METRICS_GND)):
                mt = probe_metrics(url)
                chirp_canary[band].append(parse_prom_metric(mt, "chirp_config_load_status"))
                chirp_alive[band].append(parse_prom_metric(mt, "chirp_flowgraph_alive_seconds_total"))
                chirp_bytes[band].append(parse_prom_metric(mt, "chirp_audio_bytes_published_total"))

            # system
            load, throttled, therm = probe_system()
            if load is not None:
                load_series.append(load)
            throttled_ever = throttled_ever or throttled
            therm_last = therm or therm_last

            # audio (heavier; less often)
            if time.time() - last_audio >= args.audio_every or n_audio == 0:
                for m in (MOUNT_AIR, MOUNT_GND, MOUNT_DIG):
                    cls, mean, mx = probe_audio(m, args.audio_secs)
                    audio[m].append((cls, mean, mx))
                last_audio = time.time()
                n_audio += 1

            n_state += 1
            log(f"  t+{elapsed:4d}s  leases={ {k:v[-8:] for k,v in lease_last.items()} }  "
                f"decode={dc}  load={load}  mounts_live={sorted(live)}")
            time.sleep(max(1, args.sample_every))
    except KeyboardInterrupt:
        log(YEL("\n  interrupted — producing verdict from samples so far"))

    dur = int(time.time() - start)
    return verdict(args, dur, ts, locals())

# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------
def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None

def _monotonic_nondecreasing(xs):
    xs = [x for x in xs if x is not None]
    return all(b >= a - 1e-6 for a, b in zip(xs, xs[1:])) if len(xs) >= 2 else None

def verdict(args, dur, ts, S) -> int:
    lease_drops = S["lease_drops"]; lease_missing = S["lease_missing"]; lease_first = S["lease_first"]
    agent_respawns = S["agent_respawns"]; agent_bad_exit = S["agent_bad_exit"]
    mount_missing = S["mount_missing"]; dec_series = S["dec_series"]; err_totals = S["err_totals"]
    audio = S["audio"]; chirp_canary = S["chirp_canary"]; chirp_bytes = S["chirp_bytes"]
    chirp_alive = S["chirp_alive"]; load_series = S["load_series"]; throttled_ever = S["throttled_ever"]
    n_state = S["n_state"]; sdrtrunk_seen = S["sdrtrunk_seen"]

    notes = {"digital": [], "airband": [], "ground": [], "general": []}
    verd = {}

    # ---------------- DIGITAL (RSP) ----------------
    dfail = []
    if lease_first.get(EXPECT_RSP) is None:
        dfail.append(f"broker never showed a lease on RSP {EXPECT_RSP}")
    if lease_drops.get(EXPECT_RSP, 0) > 0:
        dfail.append(f"RSP lease dropped/re-leased {lease_drops[EXPECT_RSP]}x")
    if lease_missing.get(EXPECT_RSP, 0) > 0:
        notes["digital"].append(f"RSP lease absent in {lease_missing[EXPECT_RSP]}/{n_state} samples")
    if agent_respawns.get("com.scannerproject.sdrtrunk", 0) > 0:
        dfail.append(f"SDRTrunk agent respawned {agent_respawns['com.scannerproject.sdrtrunk']}x")
    if agent_bad_exit.get("com.scannerproject.sdrtrunk", 0) > 0:
        dfail.append("SDRTrunk agent reported a nonzero exit")
    if err_totals["0x6bed"] > 0:
        dfail.append(f"0x6bed apiService segfault signature x{err_totals['0x6bed']}")
    if err_totals["apiservice"] > 0:
        dfail.append(f"sdrplay apiService errors x{err_totals['apiservice']}")
    if mount_missing[MOUNT_DIG] > 0:
        dfail.append(f"/DIGITAL.mp3 missing in {mount_missing[MOUNT_DIG]}/{n_state} samples")
    dec_counts = [d for _, d in dec_series]
    dec_total = max(dec_counts) if dec_counts else 0
    half = len(dec_series) // 2
    dec_early = dec_series[half][1] if half < len(dec_series) else 0
    dec_late = dec_counts[-1] if dec_counts else 0
    if not sdrtrunk_seen:
        dfail.append("no SDRTrunk log found — set SDRTRUNK_LOG (can't confirm decode)")
    elif dec_total == 0:
        dfail.append("zero P25 decode/sync activity — control channel should be constant "
                     "(check antenna/tuner lock)")
    elif dec_late <= dec_early:
        notes["digital"].append("decode activity present but not clearly growing in the "
                                 "second half — confirm it's still locked")
    if args.two_systems:
        notes["digital"].append("2-system requested: confirm BOTH tuners are decoding in "
                                "SDRTrunk (View -> Tuners / per-channel Preferred Tuner).")
    verd["digital"] = "PASS" if not dfail else "FAIL"
    notes["digital"][:0] = dfail

    # ---------------- ANALOG (airband / ground) ----------------
    def analog_band(name, mount, rtl, band_key):
        f = []
        if lease_first.get(rtl) is None:
            f.append(f"broker never showed a lease on RTL {rtl}")
        if lease_drops.get(rtl, 0) > 0:
            f.append(f"RTL {rtl} lease dropped/re-leased {lease_drops[rtl]}x")
        agent = "com.scannerproject.chirp-" + ("airband" if band_key == "airband" else "ground")
        if agent_respawns.get(agent, 0) > 0:
            f.append(f"{agent} respawned {agent_respawns[agent]}x")
        if agent_bad_exit.get(agent, 0) > 0:
            f.append(f"{agent} reported a nonzero exit")
        if mount_missing[mount] > 0:
            f.append(f"{mount} missing in {mount_missing[mount]}/{n_state} samples")
        # chirp config canary (voice-as-noise guard): must be 1 every sample
        canary = [x for x in chirp_canary[band_key] if x is not None]
        if canary and any(v < 1 for v in canary):
            f.append("chirp_config_load_status went 0 (config-load canary tripped)")
        elif not canary:
            notes[band_key].append(f"chirp metrics unavailable ({'9101' if band_key=='airband' else '9102'}) "
                                   "— mount liveness judged from icecast + audio only")
        # audio bytes should climb (mount actually pushing)
        if _monotonic_nondecreasing(chirp_bytes[band_key]) is False:
            notes[band_key].append("chirp_audio_bytes_published_total not monotonic — publish stalls?")
        # broadband-noise signature
        samples = audio[mount]
        classes = [cl for cl, _, _ in samples]
        means = [mn for _, mn, _ in samples]
        if "noise" in classes:
            f.append(f"BROADBAND-NOISE signature in {classes.count('noise')}/{len(classes)} audio "
                     f"samples (mean>{NOISE_MEAN_DBFS}dBFS & flat) — the MA-mode failure")
        if classes and all(cl == "silent" for cl in classes):
            notes[band_key].append("all audio samples were squelch-silent — mount is live but no "
                                   "transmission was captured; LISTEN to confirm real traffic")
        if classes and "signal" in classes:
            notes[band_key].append(f"captured live audio in {classes.count('signal')} sample(s) "
                                   f"(mean dBFS avg {_avg(means)})")
        if not classes:
            f.append("no audio samples captured (ffmpeg missing or mount unreachable)")
        return "PASS" if not f else "FAIL", f

    verd["airband"], af = analog_band("airband", MOUNT_AIR, RTL_AIRBAND, "airband")
    notes["airband"][:0] = af
    verd["ground"], gf = analog_band("ground", MOUNT_GND, RTL_GROUND, "ground")
    notes["ground"][:0] = gf

    # ---------------- GENERAL ----------------
    gfail = []
    total_respawns = sum(agent_respawns.values())
    total_badexit = sum(agent_bad_exit.values())
    if total_respawns:
        gfail.append(f"{total_respawns} agent respawn(s): "
                     + ", ".join(f"{a.split('.')[-1]}x{n}" for a, n in agent_respawns.items() if n))
    if total_badexit:
        gfail.append(f"{total_badexit} nonzero agent exit(s)")
    if err_totals["broker_denied"] > 0:
        gfail.append(f"broker claim DENIED x{err_totals['broker_denied']} (device-claim failure)")
    if throttled_ever:
        gfail.append("CPU thermal throttling observed (pmset CPU_Speed_Limit < 100)")
    load_max = max(load_series) if load_series else None
    ncpu = os.cpu_count() or 8
    if load_max is not None and load_max > ncpu * 2:
        notes["general"].append(f"peak 1-min load {load_max} (>{ncpu*2}) — high but not a hard fail "
                               "unless it drove a respawn")
    verd["general"] = "PASS" if not gfail else "FAIL"
    notes["general"][:0] = gfail
    notes["general"][:0] = PREFLIGHT_NOTES     # RSP-serial confirmation status up top

    overall = "PASS" if all(v == "PASS" for v in verd.values()) else "FAIL"

    # ---------------- report ----------------
    def band_line(k, title):
        v = verd[k]
        tag = GREEN("PASS") if v == "PASS" else RED("FAIL")
        return f"  {title:<9} {tag}"
    log("")
    log(BOLD("==================== SOAK VERDICT ===================="))
    ev = {
        "digital": f"decode_total={dec_total} early={dec_early} late={dec_late} "
                   f"lease_drops={lease_drops.get(EXPECT_RSP,0)} "
                   f"0x6bed={err_totals['0x6bed']} apisvc_err={err_totals['apiservice']} "
                   f"dig_mount_miss={mount_missing[MOUNT_DIG]}",
        "airband": f"lease_drops={lease_drops.get(RTL_AIRBAND,0)} "
                   f"mount_miss={mount_missing[MOUNT_AIR]} "
                   f"audio_classes={[c for c,_,_ in audio[MOUNT_AIR]]} "
                   f"mean_dbfs_avg={_avg([m for _,m,_ in audio[MOUNT_AIR]])}",
        "ground":  f"lease_drops={lease_drops.get(RTL_GROUND,0)} "
                   f"mount_miss={mount_missing[MOUNT_GND]} "
                   f"audio_classes={[c for c,_,_ in audio[MOUNT_GND]]} "
                   f"mean_dbfs_avg={_avg([m for _,m,_ in audio[MOUNT_GND]])}",
        "general": f"respawns={sum(agent_respawns.values())} bad_exits={sum(agent_bad_exit.values())} "
                   f"broker_denied={err_totals['broker_denied']} throttled={throttled_ever} "
                   f"load_max={max(load_series) if load_series else None} samples={n_state}",
    }
    for k, title in (("digital", "DIGITAL"), ("airband", "AIRBAND"),
                     ("ground", "GROUND"), ("general", "GENERAL")):
        log(band_line(k, title))
        log(f"            evidence: {ev[k]}")
        for nt in notes[k]:
            log(f"            - {nt}")
    log("")
    log(BOLD("  OVERALL: ") + (GREEN(overall) if overall == "PASS" else RED(overall)))
    log("  (audio quality + P25 voice intelligibility are NOT machine-judged — "
        "spot-listen to /ANALOG.mp3, /ANALOG_GROUND.mp3, /DIGITAL.mp3.)")
    log(BOLD("======================================================"))

    # ---- paste-back block ----
    log("")
    log("----- 8< ----- PASTE THIS BACK ----- 8< -----")
    log(f"soak-c revision4.1 {ts} dur={dur}s overall={overall}")
    log("RSP_SERIAL_CONFIRM=" + ("CONFIRMED via " + str(S.get("serial_method"))
        if S.get("serial_confirmed") else "NOT-INDEPENDENTLY-CONFIRMED"))
    log(f"DIGITAL={verd['digital']} | {ev['digital']}")
    log(f"AIRBAND={verd['airband']} | {ev['airband']}")
    log(f"GROUND={verd['ground']} | {ev['ground']}")
    log(f"GENERAL={verd['general']} | {ev['general']}")
    for k in ("digital", "airband", "ground", "general"):
        for nt in notes[k]:
            log(f"note[{k}]: {nt}")
    log("----- 8< ----------------------------------- 8< -----")

    # ---- write log file ----
    try:
        os.makedirs(args.logdir, exist_ok=True)
        path = os.path.join(args.logdir, f"soak-c-{ts}.log")
        with open(path, "w") as f:
            f.write("\n".join(LOG_LINES) + "\n")
        log(f"\nfull log written: {path}")
    except Exception as e:  # noqa: BLE001
        log(YEL(f"\ncould not write log file: {e}"))

    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
