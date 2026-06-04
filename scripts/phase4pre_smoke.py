#!/usr/bin/env python3
"""Phase 4-pre extended smoke test (file source).

Why file source, not the digital RSPduo
---------------------------------------

The original Phase 4-pre plan called for stopping op25 to free the
digital RSPduo (serial 180903EF32) and running chirp's SDR adapter
against it.  Phase 4b-retry already documented the blocker: the
``sdrplay_apiService`` on Micro will not expose ANY device to a third
client while both rtl-airband daemons hold connections.  Empirically
re-verified for Phase 4-pre — with op25 stopped (10 s wait, well past
SDRplay API release latency), ``SoapySDRUtil --find=driver=sdrplay``
enumerates zero devices.  And the HARD RULE prohibits restarting either
rtl-airband daemon.

So this smoke test runs against a SYNTHETIC IQ file source instead.  It
fully exercises the LO scheduler's runtime path: state machine, dwell
timer, cluster_hop events, hit-log tagging, get_status, is_parked
transitions.  What it does NOT exercise is real RF demod alignment
across LO hops — that lands in Phase 4d cutover when one of the
rtl-airband daemons is stopped to make room for chirp.

The smoke test still:

* Captures rtl-airband-airband + rtl-airband-ground PIDs and
  ActiveEnterTimestamps BEFORE chirp starts and AFTER chirp stops, so
  the report can prove production was not touched.
* Confirms op25 stays running throughout (we don't stop it this time).
* Runs chirp for 3 minutes (>= 6 hops at 30 s dwell).
* Tails the daemon's event stream + emits a JSON summary.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/home/ubuntu/scannerproject")
PY = "/usr/bin/python3"

CHIRP_CMD_PORT = 7402  # separate from prod airband (7400) / ground (7401)
EVENT_PORT = 7409

# Four production airband freqs split into TWO clusters at iq_bw=2 MHz.
# Cluster A: 127.175 + 128.300 (center 127.7375 MHz).
# Cluster B: 133.125 + 133.500 (center 133.3125 MHz).
# Names lifted from the real rtl-airband-airband config so the smoke
# log shows recognisable channels.
CHANNELS = [
    {"id": "khop127_175", "freq_mhz": 127.175, "mode": "am", "squelch_dbfs": -45.0,
     "label": "ZNY Sector 42 East Texas High"},
    {"id": "khop128_300", "freq_mhz": 128.300, "mode": "am", "squelch_dbfs": -45.0,
     "label": "ZNY Sector 66 MANTA Low"},
    {"id": "khop133_125", "freq_mhz": 133.125, "mode": "am", "squelch_dbfs": -45.0,
     "label": "ZDC Sector 59 Sea Isle High"},
    {"id": "khop133_500", "freq_mhz": 133.500, "mode": "am", "squelch_dbfs": -45.0,
     "label": "ZNY Sector 86 Atlantic Oceanic"},
]


def _write_synthetic_iq(path: Path, samp_rate: float, seconds: float) -> None:
    """Generate a 2 Msps synthetic IQ file: noise + a single AM carrier
    at +200 kHz baseband.  Looped by the file source so 30 s of file
    backs a 3 min smoke test.
    """
    import numpy as np
    n = int(samp_rate * seconds)
    t = np.arange(n, dtype=np.float64) / samp_rate
    env = 0.5 * (1.0 + 0.8 * np.sin(2 * np.pi * 1000.0 * t))
    iq = (env * np.exp(2j * np.pi * 200e3 * t)).astype(np.complex64)
    rng = np.random.default_rng(13)
    noise = (rng.normal(0, 0.002, n)
             + 1j * rng.normal(0, 0.002, n)).astype(np.complex64)
    (iq + noise).tofile(path)


def _send_udp(port: int, body: dict, timeout: float = 1.5) -> dict:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(json.dumps(body).encode(), ("127.0.0.1", port))
        data, _ = s.recvfrom(65536)
        return json.loads(data.decode())
    finally:
        s.close()


def _sh(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def main() -> int:
    log_dir = Path("/tmp/chirp_phase4pre_smoke")
    log_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = log_dir / "smoke.log"
    chirp_stdout = log_dir / "chirp.stdout"
    chirp_stderr = log_dir / "chirp.stderr"
    audio_fallback = log_dir / "audio_fallback.f32"
    audio_out = log_dir / "audio.f32"
    hit_log = log_dir / "hits.jsonl"
    state_path = log_dir / "state.json"
    snapshots = log_dir / "snapshots.jsonl"
    events_jsonl = log_dir / "events.jsonl"
    iq_path = log_dir / "synthetic.iq"

    # Snapshot prod state for the report.
    rtla_airband_pid = _sh(
        ["systemctl", "show", "-p", "MainPID", "--value",
         "rtl-airband-airband.service"], check=False,
    ).stdout.strip()
    rtla_ground_pid = _sh(
        ["systemctl", "show", "-p", "MainPID", "--value",
         "rtl-airband-ground.service"], check=False,
    ).stdout.strip()
    rtla_airband_starttime = _sh(
        ["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value",
         "rtl-airband-airband.service"], check=False,
    ).stdout.strip()
    rtla_ground_starttime = _sh(
        ["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value",
         "rtl-airband-ground.service"], check=False,
    ).stdout.strip()
    op25_active_before = _sh(
        ["systemctl", "is-active", "scanner-digital-op25.service"],
        check=False,
    ).stdout.strip()
    print(f"PROD baseline: airband pid={rtla_airband_pid} since={rtla_airband_starttime}", flush=True)
    print(f"PROD baseline: ground  pid={rtla_ground_pid} since={rtla_ground_starttime}", flush=True)
    print(f"PROD baseline: op25 active={op25_active_before}", flush=True)

    # Generate a 30 s synthetic IQ file (loops under FileIQSource).
    samp_rate = 2_000_000
    _write_synthetic_iq(iq_path, samp_rate, 30.0)
    print(f"wrote synthetic IQ: {iq_path} ({iq_path.stat().st_size} bytes)", flush=True)

    # Spawn chirp daemon as a subprocess.
    env = os.environ.copy()
    env.update({
        "CHIRP_BAND": "airband",
        "CHIRP_CMD_PORT": str(CHIRP_CMD_PORT),
        "CHIRP_SOURCE": f"file:{iq_path}",
        "CHIRP_SOURCE_SAMP_RATE": str(samp_rate),
        "CHIRP_AUDIO_OUT": f"file:{audio_out}",
        "CHIRP_LO_DWELL_SEC": "30",
        "CHIRP_LO_MAX_CLUSTERS": "3",
        "CHIRP_MAX_CHANNELS": "8",
        "CHIRP_HIT_LOG": str(hit_log),
        "CHIRP_STATE_PATH": str(state_path),
        "CHIRP_EVENT_SINK": f"127.0.0.1:{EVENT_PORT}",
        "CHIRP_LOG_LEVEL": "INFO",
        "PYTHONPATH": str(REPO),
    })
    # Bind event listener BEFORE chirp emits anything (daemon_ready is
    # the first event we want).
    ev_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ev_sock.bind(("127.0.0.1", EVENT_PORT))
    ev_sock.settimeout(0.3)

    chirp_proc = subprocess.Popen(
        [PY, "-m", "chirp.daemon"],
        cwd=str(REPO),
        env=env,
        stdout=open(chirp_stdout, "wb"),
        stderr=open(chirp_stderr, "wb"),
    )
    print(f"chirp pid={chirp_proc.pid}", flush=True)

    # 3) Wait for daemon ready.
    deadline = time.time() + 12.0
    ready = False
    while time.time() < deadline:
        try:
            resp = _send_udp(CHIRP_CMD_PORT, {
                "v": 1, "id": "ping", "cmd": "get_status", "args": {},
            }, timeout=0.5)
            if resp.get("status") == "ok":
                ready = True
                break
        except (socket.timeout, ConnectionRefusedError, OSError):
            time.sleep(0.3)
    if not ready:
        print("ERROR: chirp daemon did not come up", flush=True)
        chirp_proc.terminate()
        chirp_proc.wait(timeout=5)
        return 1

    # 4) Add channels.
    resp = _send_udp(CHIRP_CMD_PORT, {
        "v": 1, "id": "add", "cmd": "add_channel",
        "args": {"channels": CHANNELS},
    }, timeout=2.0)
    print(f"add_channel resp: status={resp.get('status')} data={resp.get('data')}", flush=True)

    # 5) Capture for 3 minutes (>= 6 hops at 30s dwell).
    capture_seconds = 180
    start = time.time()
    sn = snapshots.open("w")
    ev = events_jsonl.open("w")
    snapshot_interval = 5.0
    last_snap = 0.0
    while time.time() - start < capture_seconds:
        # Pull events as they arrive.
        for _ in range(50):
            try:
                data, _addr = ev_sock.recvfrom(65536)
            except socket.timeout:
                break
            ev.write(data.decode(errors="replace").rstrip() + "\n")
        # Snapshot status every 5 s.
        if time.time() - last_snap >= snapshot_interval:
            try:
                st = _send_udp(CHIRP_CMD_PORT, {
                    "v": 1, "id": f"st{int(time.time())}",
                    "cmd": "get_status", "args": {},
                }, timeout=1.5)
                sn.write(json.dumps(st) + "\n")
                sn.flush()
                last_snap = time.time()
            except Exception as e:
                print(f"get_status failed: {e}", flush=True)
        ev.flush()
    sn.close()
    ev.close()

    # 6) Tear down chirp.
    chirp_proc.terminate()
    try:
        chirp_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        chirp_proc.kill()
        chirp_proc.wait(timeout=5)
    ev_sock.close()

    # 7) (op25 was never stopped — file-source path.)

    # 8) Compare prod state post-test.
    after_airband_pid = _sh(
        ["systemctl", "show", "-p", "MainPID", "--value",
         "rtl-airband-airband.service"], check=False,
    ).stdout.strip()
    after_airband_starttime = _sh(
        ["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value",
         "rtl-airband-airband.service"], check=False,
    ).stdout.strip()
    after_ground_pid = _sh(
        ["systemctl", "show", "-p", "MainPID", "--value",
         "rtl-airband-ground.service"], check=False,
    ).stdout.strip()
    after_ground_starttime = _sh(
        ["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value",
         "rtl-airband-ground.service"], check=False,
    ).stdout.strip()
    op25_active_after = _sh(
        ["systemctl", "is-active", "scanner-digital-op25.service"],
        check=False,
    ).stdout.strip()

    # Summarize.
    summary = {
        "test_mode": "file_source (sdr path blocked by sdrplay_apiService — see Phase 4b retry)",
        "rtla_airband_unchanged": (
            rtla_airband_pid == after_airband_pid
            and rtla_airband_starttime == after_airband_starttime
        ),
        "rtla_ground_unchanged": (
            rtla_ground_pid == after_ground_pid
            and rtla_ground_starttime == after_ground_starttime
        ),
        "op25_unchanged": op25_active_before == op25_active_after == "active",
        "rtla_airband_before": {"pid": rtla_airband_pid, "since": rtla_airband_starttime},
        "rtla_airband_after": {"pid": after_airband_pid, "since": after_airband_starttime},
        "rtla_ground_before": {"pid": rtla_ground_pid, "since": rtla_ground_starttime},
        "rtla_ground_after": {"pid": after_ground_pid, "since": after_ground_starttime},
        "op25_before": op25_active_before,
        "op25_after": op25_active_after,
    }
    # Count cluster_hops + hits in captured streams.
    hops = 0
    hit_starts = 0
    hit_ends = 0
    plan_failed = 0
    centers_seen: list[float] = []
    with events_jsonl.open() as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            n = e.get("evt")
            if n == "cluster_hop":
                hops += 1
                if e.get("to_center_hz") is not None:
                    centers_seen.append(float(e["to_center_hz"]))
            elif n == "hit_start":
                hit_starts += 1
            elif n == "hit_end":
                hit_ends += 1
            elif n == "scheduler_plan_failed":
                plan_failed += 1
    summary.update({
        "cluster_hop_count": hops,
        "hit_start_count": hit_starts,
        "hit_end_count": hit_ends,
        "plan_failed_count": plan_failed,
        "unique_cluster_centers_hz": sorted(set(centers_seen)),
        "hit_log_lines": hit_log.read_text().count("\n") if hit_log.exists() else 0,
        "audio_out_bytes": audio_out.stat().st_size if audio_out.exists() else 0,
    })
    # Read the final status snapshot for icecast bytes_sent.
    final_status = None
    if snapshots.exists():
        lines = [ln for ln in snapshots.read_text().splitlines() if ln.strip()]
        if lines:
            try:
                final_status = json.loads(lines[-1])
            except Exception:
                pass
    if final_status:
        d = final_status.get("data", {})
        summary["final_icecast_state"] = d.get("icecast_state")
        summary["final_icecast_bytes_sent"] = d.get("icecast_bytes_sent")
        summary["final_lo_scheduler"] = d.get("lo_scheduler", {})
    smoke_log.write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
