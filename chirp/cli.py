"""chirp.cli — operator CLI for the UDP JSON command bus.

Phase 3. Speaks the same JSON envelope shape as the daemon's UDP command
server. Useful for manual smoke testing during Phase 3/4 and as a
quick-look "is the daemon alive?" tool.

Usage:
    python3 -m chirp.cli --port 7400 status
    python3 -m chirp.cli --port 7400 add-channel --id ch01 --freq 0.2 \\
            --mode am --squelch -50 --gain 0
    python3 -m chirp.cli --port 7400 remove-channel --id ch01
    python3 -m chirp.cli --port 7400 set-squelch --id ch01 --dbfs -40
    python3 -m chirp.cli --port 7400 set-freq    --id ch01 --mhz 0.2
    python3 -m chirp.cli --port 7400 set-gain    --id ch01 --db  3
    python3 -m chirp.cli --port 7400 set-master-gain --db 6
    python3 -m chirp.cli --port 7400 reset
    python3 -m chirp.cli --port 7400 events --filter hit_start,hit_end

Pretty-prints via `rich` if installed; otherwise plain JSON. Output to a
non-tty (pipe) is always plain JSON for grep/jq.

`events` opens a separate UDP socket, sends a `subscribe` command from that
socket's bound port so the daemon knows where to deliver events, then prints
each event as it arrives. Ctrl-C cleanly unsubscribes.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import uuid
from typing import Any, Optional


# ---------------------------------------------------------------------------
# UDP helpers
# ---------------------------------------------------------------------------


def _send(port: int, host: str, payload: dict, timeout: float = 2.0) -> dict:
    """Send a command envelope and return the parsed response dict.

    Raises RuntimeError if the daemon doesn't reply within `timeout`.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(json.dumps(payload).encode("utf-8"), (host, port))
        data, _ = sock.recvfrom(65536)
        return json.loads(data.decode("utf-8"))
    finally:
        sock.close()


def _envelope(cmd: str, args: dict) -> dict:
    return {"v": 1, "id": uuid.uuid4().hex[:12], "cmd": cmd, "args": args}


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------


def _pretty(obj: Any) -> str:
    """JSON dump; if `rich` is installed AND stdout is a tty, use it."""
    try:
        import rich  # noqa: F401
        from rich.console import Console
        from rich.json import JSON
        if sys.stdout.isatty():
            Console().print(JSON(json.dumps(obj)))
            return ""
    except Exception:
        pass
    return json.dumps(obj, indent=2, sort_keys=True)


def _emit(obj: Any) -> int:
    """Print a response and return a process exit code based on status."""
    out = _pretty(obj)
    if out:
        print(out)
    if isinstance(obj, dict):
        st = obj.get("status")
        if st == "ok":
            return 0
        if st == "rejected":
            return 2
        return 3  # error / unknown
    return 0


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_status(args, host: str, port: int) -> int:
    resp = _send(port, host, _envelope("get_status", {}))
    return _emit(resp)


def cmd_add_channel(args, host: str, port: int) -> int:
    body = {
        "id": args.id,
        "freq_mhz": args.freq,
        "mode": args.mode,
        "squelch_dbfs": args.squelch,
        "gain_db": args.gain,
    }
    if args.label:
        body["label"] = args.label
    return _emit(_send(port, host, _envelope("add_channel", body)))


def cmd_remove_channel(args, host: str, port: int) -> int:
    return _emit(_send(port, host, _envelope("remove_channel", {"id": args.id})))


def cmd_set_squelch(args, host: str, port: int) -> int:
    return _emit(_send(port, host, _envelope("set_squelch",
                                              {"id": args.id, "dbfs": args.dbfs})))


def cmd_set_freq(args, host: str, port: int) -> int:
    return _emit(_send(port, host, _envelope("set_freq",
                                              {"id": args.id, "mhz": args.mhz})))


def cmd_set_gain(args, host: str, port: int) -> int:
    return _emit(_send(port, host, _envelope("set_gain",
                                              {"id": args.id, "db": args.db})))


def cmd_set_master_gain(args, host: str, port: int) -> int:
    return _emit(_send(port, host, _envelope("set_master_gain", {"db": args.db})))


def cmd_reset(args, host: str, port: int) -> int:
    return _emit(_send(port, host, _envelope("reset", {})))


def cmd_events(args, host: str, port: int) -> int:
    """Subscribe to async events from the daemon and print them as they arrive.

    Opens a UDP socket bound to a random local port; sends a `subscribe`
    command FROM that socket so the daemon records the (host, port) tuple.
    Drains events until Ctrl-C, then sends `unsubscribe` and exits.
    """
    sub_events = [e.strip() for e in (args.filter or "").split(",") if e.strip()]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Bind on the loopback so the daemon's UDP reply path is unambiguous.
    sock.bind(("127.0.0.1", 0))
    bound = sock.getsockname()  # (host, port)
    print(f"# chirp.cli events: listening on {bound[0]}:{bound[1]} filter={sub_events or '*'}",
          file=sys.stderr)

    # Send subscribe FROM this socket so the daemon records OUR port.
    sub_env = _envelope("subscribe", {"events": sub_events})
    sock.sendto(json.dumps(sub_env).encode("utf-8"), (host, port))

    # Drain reply to subscribe (may interleave with events; that's fine).
    sock.settimeout(2.0)
    try:
        data, _ = sock.recvfrom(65536)
        first = json.loads(data.decode("utf-8"))
        if first.get("id") == sub_env["id"]:
            if first.get("status") != "ok":
                print(json.dumps(first), file=sys.stderr)
                return 3
        else:
            # It was already an event; print it.
            print(json.dumps(first))
    except socket.timeout:
        print("# subscribe ack timed out — daemon may not be running",
              file=sys.stderr)
        return 3

    sock.settimeout(None)
    rc = 0
    try:
        while True:
            data, _ = sock.recvfrom(65536)
            try:
                obj = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            print(json.dumps(obj), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        # Polite unsubscribe.
        try:
            sock.sendto(json.dumps(_envelope("unsubscribe", {})).encode("utf-8"),
                        (host, port))
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
    return rc


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chirp-cli",
                                description="UDP JSON CLI for chirp daemon.")
    p.add_argument("--host", default="127.0.0.1",
                   help="daemon host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=7400,
                   help="daemon UDP port (default 7400 airband, use 7401 for ground)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="get_status — pretty-print daemon snapshot")

    sp = sub.add_parser("add-channel", help="add a channel to the pool")
    sp.add_argument("--id", required=True)
    sp.add_argument("--freq", type=float, required=True,
                    help="freq in MHz (file source: offset from center in MHz)")
    sp.add_argument("--mode", default="am", choices=("am", "nfm"))
    sp.add_argument("--squelch", type=float, required=True,
                    help="squelch threshold dBFS in [-120, 0]")
    sp.add_argument("--gain", type=float, default=0.0,
                    help="per-channel gain dB in [-20, 40]")
    sp.add_argument("--label", default=None)

    sp = sub.add_parser("remove-channel", help="remove a channel from the pool")
    sp.add_argument("--id", required=True)

    sp = sub.add_parser("set-squelch", help="change squelch threshold for a channel")
    sp.add_argument("--id", required=True)
    sp.add_argument("--dbfs", type=float, required=True)

    sp = sub.add_parser("set-freq", help="retune a channel")
    sp.add_argument("--id", required=True)
    sp.add_argument("--mhz", type=float, required=True)

    sp = sub.add_parser("set-gain", help="change per-channel gain")
    sp.add_argument("--id", required=True)
    sp.add_argument("--db", type=float, required=True)

    sp = sub.add_parser("set-master-gain", help="post-mixer master gain trim")
    sp.add_argument("--db", type=float, required=True)

    sub.add_parser("reset", help="clear all channels + reset state")

    sp = sub.add_parser("events", help="subscribe to UDP event stream")
    sp.add_argument("--filter", default="",
                    help="comma-separated event names (empty = all)")

    return p


DISPATCH = {
    "status": cmd_status,
    "add-channel": cmd_add_channel,
    "remove-channel": cmd_remove_channel,
    "set-squelch": cmd_set_squelch,
    "set-freq": cmd_set_freq,
    "set-gain": cmd_set_gain,
    "set-master-gain": cmd_set_master_gain,
    "reset": cmd_reset,
    "events": cmd_events,
}


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    fn = DISPATCH[args.cmd]
    try:
        return fn(args, args.host, args.port)
    except socket.timeout:
        print(json.dumps({"status": "error", "error": "timeout — daemon unreachable"}))
        return 3
    except ConnectionRefusedError:
        print(json.dumps({"status": "error",
                          "error": f"connection refused {args.host}:{args.port}"}))
        return 3
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"status": "error", "error": f"{type(e).__name__}: {e}"}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
