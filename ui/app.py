"""Main server application."""
from http.server import HTTPServer
from socketserver import ThreadingMixIn

try:
    from .config import UI_PORT
    from .handlers import Handler
    from .server_workers import start_config_worker, start_icecast_monitor
except ImportError:
    from ui.config import UI_PORT
    from ui.handlers import Handler
    from ui.server_workers import start_config_worker, start_icecast_monitor


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
    start_config_worker()
    start_icecast_monitor()
    server = ThreadedHTTPServer(("0.0.0.0", UI_PORT), Handler)
    logging.info(f"UI listening on 0.0.0.0:{UI_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
