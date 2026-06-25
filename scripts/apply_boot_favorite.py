#!/usr/bin/env python3
"""Re-apply a band's favorite channels at daemon start (ExecStartPost).

Reads favorites_sets/nashville_<band>.json and add-channels each via the
local chirp cmd-bus, making analog channels reboot-persistent without a
daemon code change. Idempotent-tolerant: dup-id add failures are ignored.
Runs as the gr-demod service user; cmd-bus is a local UDP socket.
"""
import json, os, subprocess, sys, time

REPO = "/home/ubuntu/scannerproject"
BANDS = {"airband": (7400, "am"), "ground": (7401, "nfm")}
SQUELCH = "-56"

def main():
    band = sys.argv[1] if len(sys.argv) > 1 else ""
    if band not in BANDS:
        print(f"apply_boot_favorite: unknown band {band!r}", file=sys.stderr); return 1
    port, mode = BANDS[band]
    fav_path = os.path.join(REPO, "favorites_sets", f"nashville_{band}.json")
    try:
        fav = json.load(open(fav_path))
    except Exception as e:
        print(f"apply_boot_favorite: no favorite {fav_path}: {e}", file=sys.stderr); return 0
    chans = fav.get("custom_favorites", []) or []
    cli = [sys.executable, "-m", "chirp.cli", "--port", str(port)]
    for _ in range(40):                       # wait for cmd-bus ready
        if subprocess.run(cli + ["status"], capture_output=True, cwd=REPO).returncode == 0:
            break
        time.sleep(1)
    else:
        print(f"apply_boot_favorite: cmd-bus :{port} never ready", file=sys.stderr); return 0
    added = 0
    for c in chans:
        cid = str(c.get("id") or "").strip()
        freq = c.get("frequency")
        if not cid or freq is None:
            continue
        label = str(c.get("alpha_tag") or cid)
        r = subprocess.run(cli + ["add-channel", "--id", cid, "--freq", str(freq),
                                  "--mode", mode, "--squelch", SQUELCH, "--label", label],
                           capture_output=True, cwd=REPO)
        if r.returncode == 0:
            added += 1
    print(f"apply_boot_favorite: {band} added {added}/{len(chans)} channels")
    return 0

if __name__ == "__main__":
    sys.exit(main())
