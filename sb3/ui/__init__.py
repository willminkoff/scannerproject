"""sb3.ui — the SB3 web UI shim.

Serves the frozen `ui/sb3.html` (the late-May SB3 UI, reused verbatim) and
implements the `/api/*` endpoints it polls, mapping each onto the SB3 backend
observers (sb3.backends) and the profile/translator layer.

Phase 3.1 (this): serve the page + `/api/status` + `/api/heartbeat` (read-only),
prove the kill-switch owns it. Phases 3.2/3.3 wire the write endpoints
(tune/squelch/volume/scan/digital) onto sb3.sdrangel + sb3.translator.

STDLIB ONLY — no Flask. The brief said Flask, but the sb3 package is stdlib-only
and deploys via a git sparse-checkout with no pip/venv on Neptune; adding Flask
would need a runtime install the clean git-deploy model does not have. This uses
http.server, matching the old airband-ui (ui/app.py) pattern.
"""
