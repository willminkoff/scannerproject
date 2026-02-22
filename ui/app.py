"""Main server application."""
import os
import sys
from http.server import HTTPServer
from socketserver import ThreadingMixIn

try:
    from .config import UI_PORT
    from .handlers import Handler
    from .server_workers import start_config_worker, start_icecast_monitor
    from .v3_runtime import bootstrap_runtime
except ImportError:
    # Support direct execution (`python ui/app.py`) by adding repo root.
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from ui.config import UI_PORT
    from ui.handlers import Handler
    from ui.server_workers import start_config_worker, start_icecast_monitor
    from ui.v3_runtime import bootstrap_runtime


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server with threading support."""
    daemon_threads = True


def main():
    """Start the UI server."""
    import logging
    # Ensure existing handlers are reconfigured so DEBUG traffic is suppressed by default
    logging.basicConfig(level=logging.INFO, force=True, format='%(levelname)s %(name)s: %(message)s')
    # Explicitly set root logger level to INFO to be extra-safe
    logging.getLogger().setLevel(logging.INFO)
    try:
        compile_state = bootstrap_runtime()
        logging.info(
            "V3 compile status=%s issues=%s",
            compile_state.get("status"),
            len(compile_state.get("issues") or []),
        )
    except Exception as e:
        logging.warning("V3 runtime bootstrap failed: %s", e)
    start_config_worker()
    start_icecast_monitor()
    server = ThreadedHTTPServer(("0.0.0.0", UI_PORT), Handler)
    logging.info(f"UI listening on 0.0.0.0:{UI_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
