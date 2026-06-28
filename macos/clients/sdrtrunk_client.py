#!/usr/bin/env python3
"""sdrtrunk_client.py — integration for SDRTrunk (read decode state + coarse control).

⚠️ SDRTrunk's control surface (the project's hard part): it's a Java GUI app with NO rich
runtime REST API. What we actually have:
  1. PLAYLIST (config-side control): systems/channels/aliases live in ~/SDRTrunk/playlist/*.xml.
     "Control" = edit the XML + restart SDRTrunk. (See data/hpdb_to_sdrtrunk.py.)
  2. STATE OUT (read-side): SDRTrunk writes decoded activity to ~/SDRTrunk/event_logs/:
       *_call_events.log     — per-call rows (talkgroup, freq, encrypted, duration)  <- the live feed
       *_decoded_messages.log — control-channel TSBKs / IDEN / SYNC LOSS              <- CC health + system id
     (~/SDRTrunk/logs/sdrtrunk_app.log has NO decoded TSBKs — don't scrape it for activity.)

So the integration model is: READ decode via event_logs; CHANGE what's monitored via playlist +
restart. There is no "set squelch on channel X at runtime over HTTP" like SDRangel.
"""
from __future__ import annotations
import csv, glob, io, os, subprocess

SDRTRUNK_HOME = os.environ.get("SDRTRUNK_HOME", os.path.expanduser("~/SDRTrunk"))
SDRTRUNK_LOGS = os.environ.get("SDRTRUNK_LOG", os.path.join(SDRTRUNK_HOME, "logs"))
EVENT_LOGS    = os.path.join(SDRTRUNK_HOME, "event_logs")
PLAYLIST_DIR  = os.path.join(SDRTRUNK_HOME, "playlist")


def _newest(pattern: str) -> str | None:
    files = [p for p in glob.glob(pattern) if os.path.exists(p)]
    return max(files, key=os.path.getmtime) if files else None


def _tail(path: str, nbytes: int = 65536) -> str:
    """Read the last nbytes of a file as text (cheap for large, growing logs)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
            data = f.read()
        return data.decode("utf-8", "replace")
    except OSError:
        return ""


class SDRTrunk:
    def is_running(self) -> bool:
        try:
            return subprocess.run(["pgrep", "-f", "io.github.dsheirer"],
                                  capture_output=True).returncode == 0
        except Exception:
            return False

    # ---- read-side: live decode from event_logs --------------------------------------
    def recent_calls(self, n: int = 25) -> list[dict]:
        """Most recent call events (newest first): talkgroup, freq, encrypted, duration.
        call_events columns: TIMESTAMP,DURATION_MS,PROTOCOL,EVENT,FROM,TO,CHANNEL_NUMBER,
        FREQUENCY,TIMESLOT,DETAILS,EVENT_ID."""
        ce = _newest(os.path.join(EVENT_LOGS, "*_call_events.log"))
        if not ce:
            return []
        rows = list(csv.reader(io.StringIO(_tail(ce))))
        calls = []
        for r in rows:
            if len(r) < 11 or r[0].startswith("TIMESTAMP"):
                continue
            ts, dur, proto, event, frm, to, chan, freq, tslot, details, eid = r[:11]
            if "Call" not in event:                  # keep voice/data calls, skip Page/Response/etc
                continue
            tg_alias, tg = _split_to(to)
            try:
                fmhz = float(freq)
            except ValueError:
                fmhz = 0.0
            # Reject implausible freqs: a corrupt IDEN_UPDATE (marginal CC lock) poisons the
            # channel plan and SDRTrunk then computes garbage traffic freqs (e.g. >960 MHz). Only
            # trust values inside the P25 public-safety range; otherwise the grant wasn't mappable.
            plausible = 130.0 < fmhz < 960.0
            enc = "ENCRYPT" in details.upper()
            calls.append({
                "time": ts.split(":", 3)[-1] if ts.count(":") >= 3 else ts,  # HH:MM:SS
                "event": event, "from": frm.strip(), "tg": tg, "alias": tg_alias,
                "freq": round(fmhz, 4) if plausible else 0, "followed": plausible, "enc": enc,
                "dur_ms": int(dur) if dur.isdigit() else None,
            })
        return list(reversed(calls))[:n]

    def system_info(self) -> dict:
        """Trunked-system identity from the latest control-channel broadcasts."""
        dm = _newest(os.path.join(EVENT_LOGS, "*_decoded_messages.log"))
        info: dict = {}
        if not dm:
            return info
        text = _tail(dm)
        import re
        for key, pat in (("nac", r"NAC:\d+/x([0-9A-Fa-f]+)"),
                         ("wacn", r"WACN:\d+/x([0-9A-Fa-f]+)"),
                         ("system", r"SYSTEM:\d+/x([0-9A-Fa-f]+)"),
                         ("rfss", r"RFSS:(\d+)"), ("site", r"SITE:(\d+)")):
            m = re.findall(pat, text)
            if m:
                info[key] = m[-1]
        return info

    def cc_health(self, lines: int = 600) -> dict:
        """Rough control-channel lock quality over the recent decode window."""
        dm = _newest(os.path.join(EVENT_LOGS, "*_decoded_messages.log"))
        if not dm:
            return {"ok": False}
        recent = _tail(dm).splitlines()[-lines:]
        total = len(recent) or 1
        sync_loss = sum(1 for l in recent if "SYNC LOSS" in l)
        grants = sum(1 for l in recent if "GRP_VCH_GR" in l)
        valid = total - sync_loss
        return {"ok": valid > 0, "valid": valid, "lines": total,
                "sync_loss_pct": round(sync_loss / total * 100), "grants": grants}

    # ---- config-side: playlist -------------------------------------------------------
    def playlists(self) -> list[str]:
        return sorted(glob.glob(os.path.join(PLAYLIST_DIR, "*.xml")))


def _split_to(to: str) -> tuple[str | None, str | None]:
    """SDRTrunk formats the TO column as '[Alias] (TGID)' or ' (TGID)' (no alias).
    '[VU Dispatch] (3207)' -> ('VU Dispatch', '3207');  ' (65535)' -> (None, '65535')."""
    to = to.strip()
    if to.endswith(")") and "(" in to:
        alias, tg = to.rsplit("(", 1)
        alias = alias.strip().strip("[]").strip()
        return (alias or None, tg.rstrip(")").strip() or None)
    return (None, to or None)


if __name__ == "__main__":
    st = SDRTrunk()
    print("running:", st.is_running())
    print("system:", st.system_info())
    print("cc_health:", st.cc_health())
    print("recent calls:")
    for c in st.recent_calls(10):
        print("  ", c)
