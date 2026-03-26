"""Main server application."""
import os
import sys
from http.server import HTTPServer
from socketserver import ThreadingMixIn

try:
    from .config import UI_PORT
    from .handlers import Handler
    from .server_workers import start_config_worker, start_icecast_monitor
    from .favorites_runtime import sync_scan_pool_to_runtime
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
    from ui.favorites_runtime import sync_scan_pool_to_runtime
    from ui.v3_runtime import bootstrap_runtime


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server with threading support."""
    daemon_threads = True


def _rehydrate_wx_reader():
    """If the active ground profile is a wx decoder, ensure the decoder
    service is running and start the reader thread.

    Handles both airband-ui restarts (service already active) and full
    cold boots (service not yet started because it is not systemd-enabled).
    """
    import logging
    try:
        try:
            from .config import GROUND_CONFIG_PATH
            from .profile_config import read_wx_decoder
            from .systemd import unit_active, UNITS, start_acars, start_radiosonde
            from .server_workers import start_wx_reader
        except ImportError:
            from ui.config import GROUND_CONFIG_PATH
            from ui.profile_config import read_wx_decoder
            from ui.systemd import unit_active, UNITS, start_acars, start_radiosonde
            from ui.server_workers import start_wx_reader

        ground_conf = os.path.realpath(GROUND_CONFIG_PATH)
        decoder = read_wx_decoder(ground_conf)
        if not decoder or decoder not in UNITS:
            return

        # If the decoder service isn't running yet (cold boot), start it.
        if not unit_active(UNITS[decoder]):
            logging.info("Cold-boot: starting %s decoder service", decoder)
            _starters = {"acars": start_acars, "radiosonde": start_radiosonde}
            starter = _starters.get(decoder)
            if starter:
                ok, err = starter()
                if not ok:
                    logging.warning("Failed to start %s on boot: %s", decoder, err)
                    return

        logging.info("Rehydrating wx reader for active %s decoder", decoder)
        start_wx_reader(decoder)
    except Exception:
        logging.getLogger(__name__).debug("wx rehydration skipped", exc_info=True)


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
    try:
        runtime_sync = sync_scan_pool_to_runtime(force=True)
        logging.info(
            "Favorites runtime sync ok=%s changed=%s analog_changed=%s digital_changed=%s",
            bool(runtime_sync.get("ok")),
            bool(runtime_sync.get("changed")),
            bool((runtime_sync.get("analog") or {}).get("changed")),
            bool((runtime_sync.get("digital") or {}).get("changed")),
        )
    except Exception as e:
        logging.warning("Favorites runtime sync failed: %s", e)
    start_config_worker()
    start_icecast_monitor()
    _rehydrate_wx_reader()
    server = ThreadedHTTPServer(("0.0.0.0", UI_PORT), Handler)
    logging.info(f"UI listening on 0.0.0.0:{UI_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
