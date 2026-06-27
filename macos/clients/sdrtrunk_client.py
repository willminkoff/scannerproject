#!/usr/bin/env python3
"""sdrtrunk_client.py — integration scaffolding for SDRTrunk.

⚠️ REALITY CHECK on SDRTrunk's control surface (this is the project's hard part):
SDRTrunk is a Java GUI app. Unlike SDRangel, it has **no rich runtime REST API**
for set/get of channels, squelch, tuning, etc. What we actually have to work with:

  1. PLAYLIST (config-side control): SDRTrunk's systems/channels/aliases live in an
     XML playlist (~/SDRTrunk/playlist/*.xml). "Control" = edit the XML + restart
     SDRTrunk (or toggle a channel's auto-start). This is how P25 systems (MTRTRS,
     TACN control channels + talkgroup aliases) get configured. See data/hpdb_to_sdrtrunk.py.
  2. EVENT/STATE OUT (read-side): SDRTrunk emits decoded activity to:
       - log files (~/SDRTrunk/logs) — the existing scripts/sdrtrunk-local-monitor.py
         already scrapes these for a live "what's decoding" view; reuse that.
       - the built-in Icecast/Broadcastify/RdioScanner *broadcasters* (audio + call
         metadata) — the audio path for remote listening.
  3. AUDIO: CoreAudio out (local → BOOM) + the Icecast broadcaster (remote).

So the integration model for scannerctl/Claude is:
  - READ live decode state  → tail/scrape SDRTrunk logs (or its rdioscanner feed).
  - CHANGE what's monitored → rewrite the playlist XML + restart via launchctl.
  - There is NO "set squelch on channel X at runtime over HTTP" like SDRangel.

This file is a STUB documenting that boundary + sketching the two real levers.
Verify against the installed SDRTrunk version before relying on paths/formats.
"""
from __future__ import annotations
import os, subprocess, glob

SDRTRUNK_HOME = os.environ.get("SDRTRUNK_HOME", os.path.expanduser("~/SDRTrunk"))
SDRTRUNK_LOGS = os.environ.get("SDRTRUNK_LOG", os.path.join(SDRTRUNK_HOME, "logs"))
PLAYLIST_DIR  = os.path.join(SDRTRUNK_HOME, "playlist")
LAUNCHD_LABEL = "com.scannerproject.sdrtrunk"


class SDRTrunk:
    # ---- read-side: scrape logs for live decode state ----
    def latest_log(self) -> str | None:
        logs = sorted(glob.glob(os.path.join(SDRTRUNK_LOGS, "*.log")),
                      key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
        return logs[-1] if logs else None

    def recent_activity(self, lines: int = 40) -> list[str]:
        """Last N log lines mentioning calls/talkgroups. TODO: tighten the filter
        once we see the real log format from a running instance."""
        log = self.latest_log()
        if not log:
            return ["(no SDRTrunk log found — is it running? check SDRTRUNK_LOG)"]
        try:
            out = subprocess.run(["tail", "-n", str(lines), log],
                                 capture_output=True, text=True, timeout=5).stdout
            return [l for l in out.splitlines()
                    if any(k in l.lower() for k in ("call", "talkgroup", "tg:", "p25", "control"))]
        except Exception as e:
            return [f"(log read error: {e})"]

    def is_running(self) -> bool:
        try:
            return subprocess.run(["pgrep", "-f", "io.github.dsheirer"],
                                  capture_output=True).returncode == 0
        except Exception:
            return False

    # ---- config-side: playlist + restart ----
    def playlists(self) -> list[str]:
        return sorted(glob.glob(os.path.join(PLAYLIST_DIR, "*.xml")))

    def restart(self):
        """Restart SDRTrunk via launchd so a new playlist takes effect.
        (No hot-reload — SDRTrunk reloads the playlist on start.)"""
        uid = os.getuid()
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{LAUNCHD_LABEL}"], check=False)


if __name__ == "__main__":
    st = SDRTrunk()
    print("SDRTrunk running:", st.is_running())
    print("playlists:", st.playlists() or "(none)")
    print("latest log:", st.latest_log())
    print("recent activity:")
    for l in st.recent_activity():
        print("  ", l)
