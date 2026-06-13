#!/usr/bin/env python3
"""chirp-audio-path-probe — sample the chirp daemon's audio_path snapshot.

Polls the cmd server's `get_status` once per second (default) and prints
the audio_path block.  Use to verify the all-muted-while-hits-fire
hypothesis from 2026-06-11.

Field meanings (see chirp/hit_detector.py):
  tick_lag_ms     time since the prior tick.  poll_s * 1000 in steady
                  state; > 500 ms with poll_s=0.2 signals Python thread
                  starvation under GR scheduler load.
  open_count      live channels with squelch open this tick.
  muted_count     live channels with priority_muted=True after the gate
                  update.  When priority_gate is disabled this stays 0.
  parked_count    channels skipped because the LO scheduler has them parked.
  live_count      claimed channels not parked this tick.
  selected_id     priority gate selection (None if no opens).
  audio_path_health
      "live"      at least one live, non-muted channel is squelch-open
                  → audio should flow.
      "all_muted" every live channel is priority-muted but at least one
                  is squelch-open.  THIS IS THE BUG.  The gate failed to
                  unmute the open channel.
      "no_open"   live but nothing open.  Expected quiet.
      "no_live"   every claimed channel is parked (cold start or
                  plan_failed).

Usage:
  python3 scripts/chirp-audio-path-probe.py            # airband (7400), 1 Hz, forever
  python3 scripts/chirp-audio-path-probe.py --port 7401   # ground
  python3 scripts/chirp-audio-path-probe.py --interval 0.2  # 5 Hz
  python3 scripts/chirp-audio-path-probe.py --once     # one sample then exit
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time


def get_status(host: str, port: int, timeout: float = 3.0) -> dict:
    s = socket.socket()
    s.settimeout(timeout)
    s.connect((host, port))
    try:
        s.sendall(json.dumps({"cmd": "get_status"}).encode() + b"\n")
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
        # First newline-terminated frame is the response.
        line = buf.split(b"\n", 1)[0]
        return json.loads(line.decode("utf-8"))
    finally:
        s.close()


def format_row(t: float, ap: dict, gate_enabled: bool, gate_selected) -> str:
    health = ap.get("audio_path_health", "?")
    lag = ap.get("tick_lag_ms")
    lag_s = f"{lag:>5.0f}" if isinstance(lag, (int, float)) else "  ?  "
    open_n = ap.get("open_count", 0)
    muted_n = ap.get("muted_count", 0)
    parked_n = ap.get("parked_count", 0)
    live_n = ap.get("live_count", 0)
    sel = ap.get("selected_id") or "-"
    # Truncate selected_id for table alignment.
    sel_s = sel[:30] if isinstance(sel, str) else "-"
    gate_s = "on " if gate_enabled else "off"
    marker = ""
    if health == "all_muted":
        marker = "  ◄── BUG SIGNATURE"
    elif health == "no_live":
        marker = "  (cold)"
    return (
        f"{time.strftime('%H:%M:%S', time.localtime(t))} "
        f"health={health:<10} lag={lag_s}ms "
        f"open={open_n} muted={muted_n} parked={parked_n} live={live_n} "
        f"gate={gate_s} sel={sel_s}{marker}"
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7400, help="7400 airband, 7401 ground")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    p.add_argument("--once", action="store_true", help="one sample then exit")
    p.add_argument("--json", action="store_true", help="raw JSONL output for piping")
    args = p.parse_args()

    print(
        f"# probing chirp daemon at {args.host}:{args.port} "
        f"interval={args.interval}s",
        file=sys.stderr,
    )

    while True:
        t0 = time.time()
        try:
            resp = get_status(args.host, args.port)
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            print(
                f"{time.strftime('%H:%M:%S', time.localtime(t0))} "
                f"# get_status failed: {e}",
                file=sys.stderr,
            )
            if args.once:
                return 1
            time.sleep(args.interval)
            continue

        data = resp.get("data") or resp
        # Daemon key is `audio_path_state` (the per-tick diagnostics dict);
        # the `audio_path` field at top-level is the audio output file path
        # string, which we don't want here.  Pre-2026-06-13 daemons emitted
        # the dict under `audio_path`; fall back so this probe still works
        # against an older daemon during rollouts.
        ap = data.get("audio_path_state") or data.get("audio_path") or {}
        pg = data.get("priority_gate") or {}
        if args.json:
            row = {
                "ts": t0,
                "host_port": f"{args.host}:{args.port}",
                "audio_path_state": ap,
                "priority_gate": pg,
            }
            print(json.dumps(row, separators=(",", ":")))
        else:
            print(format_row(t0, ap, pg.get("enabled", False), pg.get("selected")))
        sys.stdout.flush()
        if args.once:
            return 0
        # Drift-corrected sleep so we keep a steady cadence under load.
        dt = time.time() - t0
        if dt < args.interval:
            time.sleep(args.interval - dt)


if __name__ == "__main__":
    raise SystemExit(main())
