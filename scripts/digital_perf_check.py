#!/usr/bin/env python3
"""Sample /api/digital/scheduler and report digital canary metrics."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request


def _fetch_json(url: str, timeout_sec: float) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    payload = json.loads(raw or "{}")
    return payload if isinstance(payload, dict) else {}


def _fmt_pct(value: float) -> str:
    return f"{value * 100.0:.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description="Digital scheduler canary perf check")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:5050/api/digital/scheduler",
        help="Scheduler endpoint URL",
    )
    parser.add_argument("--duration-sec", type=float, default=180.0, help="Sampling duration")
    parser.add_argument("--interval-sec", type=float, default=1.0, help="Sample interval")
    parser.add_argument("--timeout-sec", type=float, default=2.0, help="HTTP timeout")
    parser.add_argument("--min-fast-tick-ratio", type=float, default=0.90)
    parser.add_argument("--max-median-rotation", type=float, default=1.5)
    parser.add_argument("--max-apply-error-rate", type=float, default=0.05)
    parser.add_argument("--max-lock-timeout-ratio", type=float, default=0.50)
    parser.add_argument("--min-switches", type=int, default=2)
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when checks fail")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    args = parser.parse_args()

    duration_sec = max(5.0, float(args.duration_sec))
    interval_sec = max(0.2, float(args.interval_sec))
    timeout_sec = max(0.5, float(args.timeout_sec))

    samples: list[tuple[float, dict]] = []
    errors: list[str] = []

    start = time.time()
    end = start + duration_sec
    next_tick = start

    while True:
        now = time.time()
        if now >= end:
            break
        if now < next_tick:
            time.sleep(min(0.2, next_tick - now))
            continue

        try:
            payload = _fetch_json(args.url, timeout_sec)
            samples.append((now, payload))
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            errors.append(str(exc))

        next_tick += interval_sec

    active_samples: list[tuple[float, dict]] = []
    for ts, payload in samples:
        mode = str(payload.get("digital_scheduler_mode") or payload.get("digital_scan_mode") or "").strip().lower()
        systems = payload.get("digital_scheduler_systems") or []
        if mode == "timeslice_multi_system" and isinstance(systems, list) and len(systems) > 1:
            active_samples.append((ts, payload))

    tick_ok = 0
    tick_total = 0
    apply_error_count = 0
    switch_intervals: list[float] = []
    switch_reasons: dict[str, int] = {}
    last_active_system = ""
    last_switch_ts = 0.0

    for ts, payload in active_samples:
        tick_ms = int(payload.get("digital_scheduler_tick_interval_ms") or 0)
        if tick_ms > 0:
            tick_total += 1
            if tick_ms <= 300:
                tick_ok += 1

        if str(payload.get("digital_scheduler_last_apply_error") or "").strip():
            apply_error_count += 1

        active_system = str(payload.get("digital_scheduler_active_system") or "").strip()
        if active_system:
            if last_active_system and active_system != last_active_system:
                if last_switch_ts > 0:
                    switch_intervals.append(max(0.0, ts - last_switch_ts))
                reason = str(payload.get("digital_scheduler_switch_reason") or "").strip().lower() or "unknown"
                switch_reasons[reason] = int(switch_reasons.get(reason) or 0) + 1
                last_switch_ts = ts
            elif not last_active_system:
                last_switch_ts = ts
            last_active_system = active_system

    fast_tick_ratio = (tick_ok / tick_total) if tick_total > 0 else 0.0
    apply_error_rate = (apply_error_count / len(active_samples)) if active_samples else 0.0
    switch_count = sum(switch_reasons.values())
    lock_timeout_count = int(switch_reasons.get("lock_timeout") or 0)
    lock_timeout_ratio = (lock_timeout_count / switch_count) if switch_count > 0 else 0.0
    median_rotation = statistics.median(switch_intervals) if switch_intervals else 0.0

    checks = {
        "fast_tick_ratio": fast_tick_ratio >= float(args.min_fast_tick_ratio),
        "median_rotation": (switch_count >= int(args.min_switches) and median_rotation <= float(args.max_median_rotation)),
        "apply_error_rate": apply_error_rate <= float(args.max_apply_error_rate),
        "lock_timeout_ratio": (switch_count == 0 or lock_timeout_ratio <= float(args.max_lock_timeout_ratio)),
    }

    report = {
        "ok": bool(all(checks.values()) and len(active_samples) > 0),
        "duration_sec": duration_sec,
        "interval_sec": interval_sec,
        "samples_total": len(samples),
        "samples_active": len(active_samples),
        "fetch_errors": len(errors),
        "fast_tick_ratio": fast_tick_ratio,
        "median_rotation_sec": median_rotation,
        "switch_count": switch_count,
        "switch_reasons": switch_reasons,
        "lock_timeout_ratio": lock_timeout_ratio,
        "apply_error_rate": apply_error_rate,
        "checks": checks,
        "thresholds": {
            "min_fast_tick_ratio": float(args.min_fast_tick_ratio),
            "max_median_rotation": float(args.max_median_rotation),
            "max_apply_error_rate": float(args.max_apply_error_rate),
            "max_lock_timeout_ratio": float(args.max_lock_timeout_ratio),
            "min_switches": int(args.min_switches),
        },
    }

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("Digital Scheduler Perf Check")
        print(f"- URL: {args.url}")
        print(f"- Samples: {len(samples)} total, {len(active_samples)} active")
        print(f"- Fetch errors: {len(errors)}")
        print(f"- Fast tick <=300ms: {_fmt_pct(fast_tick_ratio)}")
        if switch_count > 0:
            print(f"- Switches observed: {switch_count} ({switch_reasons})")
            print(f"- Median rotation: {median_rotation:.2f}s")
            print(f"- Lock-timeout ratio: {_fmt_pct(lock_timeout_ratio)}")
        else:
            print("- Switches observed: none")
        print(f"- Apply error rate: {_fmt_pct(apply_error_rate)}")
        print("- Checks:")
        print(f"  fast_tick_ratio: {'PASS' if checks['fast_tick_ratio'] else 'FAIL'}")
        print(f"  median_rotation: {'PASS' if checks['median_rotation'] else 'FAIL'}")
        print(f"  apply_error_rate: {'PASS' if checks['apply_error_rate'] else 'FAIL'}")
        print(f"  lock_timeout_ratio: {'PASS' if checks['lock_timeout_ratio'] else 'FAIL'}")

    if args.strict and not report["ok"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
