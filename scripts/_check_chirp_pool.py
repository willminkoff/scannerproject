#!/usr/bin/env python3
"""Read airband + ground daemon get_status to see live channel pool."""
import socket, json

for port, band in ((7400, "airband"), (7401, "ground")):
    print(f"\n=== {band} port {port} ===")
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
        data = d.get("data") or d
        chs = data.get("channels", []) or []
        print(f"live channels: {len(chs)}")
        for c in chs[:12]:
            print(f"  freq={c.get('freq_mhz')} sq={c.get('squelch_dbfs')} parked={c.get('is_parked')} id={c.get('id')!r} label={c.get('label')!r}")
        aps = data.get("audio_path_state") or data.get("audio_path") or {}
        if isinstance(aps, dict):
            print(f"audio_path_state: live={aps.get('live_count')} parked={aps.get('parked_count')} open={aps.get('open_count')} health={aps.get('audio_path_health')}")
    except Exception as e:
        print(f"err: {e}")
