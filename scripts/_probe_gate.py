#!/usr/bin/env python3
"""Quick probe to verify input_gate fix is live on both chirp daemons."""
import socket, json
for port in (7400, 7401):
    try:
        s = socket.socket()
        s.settimeout(3)
        s.connect(("127.0.0.1", port))
        s.sendall(json.dumps({"cmd": "get_status"}).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        d = json.loads(buf.split(b"\n", 1)[0].decode())
        ch = d.get("channels", [])
        parked = sum(1 for c in ch if c.get("parked"))
        gated = sum(1 for c in ch if c.get("input_gated"))
        has = sum(1 for c in ch if "input_gated" in c)
        ap = d.get("audio_path", {}) or {}
        band = d.get("band", "?")
        print(
            f"port={port} band={band} ch={len(ch)} parked={parked} "
            f"input_gated={gated} has_field={has} "
            f"open={ap.get('open_count')} live={ap.get('live_count')} "
            f"health={ap.get('audio_path_health')}"
        )
    except Exception as e:
        print(f"port={port} err: {e}")
