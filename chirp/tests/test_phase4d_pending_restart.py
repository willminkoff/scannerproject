"""Phase 4d cleanup — response-shape regression tests for the three
per-band airband endpoints under chirp vs the legacy rtl-airband path.

The contract this file pins down:

  * Legacy (USE_GR_DEMOD=false) — `/api/airband/squelch`,
    `/api/airband/gain`, and `/api/airband/squelch_preset` MUST ship
    `pending_restart: bool` in the success response.  sb5's per-band
    card uses it to render the "pending · applies on next rtl-airband
    restart" hint and to fire the 6 s auto-apply countdown.

  * Chirp (USE_GR_DEMOD=true) — the same three endpoints MUST OMIT
    `pending_restart` entirely.  There is no "next restart" under
    chirp; squelch + gain apply live via the chirp client.  Returning
    the field — even as `False` — would be misleading and would cause
    the sb5 hint widget to flicker between empty and "pending" on
    every slider commit (the regression Will reported reading as an
    "error" when adjusting ground squelch on 2026-06-04).

Tests exercise the real `handlers.do_POST` dispatch with a
`_FakePostRequest`, mocking the chirp probe + filesystem touches so
the test is hermetic.  Pattern lifted from
`tests/test_api_workflows.py` (the established convention for
handler-level tests in this repo) but inlined here so chirp's
test target (`pytest chirp/tests/`) picks it up.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ui import handlers  # noqa: E402


# --- minimal POST request stub --------------------------------------------


class _FakePostRequest:
    """Stand-in for BaseHTTPRequestHandler exposing the same surface
    handlers.Handler.do_POST consumes (path + headers + rfile + _send).
    """

    def __init__(self, path: str, body: str,
                 ctype: str = "application/x-www-form-urlencoded"):
        self.path = path
        payload = body.encode("utf-8")
        self.headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": ctype,
        }
        self.rfile = io.BytesIO(payload)
        self.sent: list = []

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="ignore")
        self.sent.append((code, body, ctype))
        return code, body, ctype


def _post_json(path: str, body: dict):
    """POST as JSON body so the handler's form parsing pulls the keys."""
    raw = json.dumps(body)
    req = _FakePostRequest(path, raw, ctype="application/json")
    code, resp_body, _ = handlers.Handler.do_POST(req)
    return code, json.loads(resp_body)


# --- /api/airband/squelch + /api/airband/gain -----------------------------


@pytest.fixture(autouse=True)
def _stub_controls_io():
    """All three endpoints under test do filesystem I/O on controls
    files.  Stub it out so the tests don't depend on a writable
    runtime layout.  We also disable the managed-override persist
    helper — it's exercised in test_managed_analog_controls.py.
    """
    with mock.patch.object(handlers, "resolve_controls_path",
                           return_value="/tmp/test-controls.conf"), \
         mock.patch.object(handlers, "parse_controls",
                           return_value=(32.8, 10.0, -40.0, "dbfs")), \
         mock.patch.object(handlers, "write_controls",
                           return_value=True), \
         mock.patch("ui.managed_analog_controls."
                    "persist_managed_controls_override",
                    return_value=None):
        yield


@pytest.mark.parametrize("path,body", [
    ("/api/airband/gain", {"band": "air", "gain_db": 30}),
    ("/api/airband/squelch", {"band": "air", "threshold_dbfs": -35}),
])
def test_gain_and_squelch_omit_pending_restart_under_chirp(path, body):
    """Phase 4d contract: with USE_GR_DEMOD=true the response MUST
    NOT contain `pending_restart`.  sb5 (`_bandCommitAfter`) treats
    missing-field as authoritative "not pending" and never paints
    the rtl-airband restart hint."""
    with mock.patch.object(handlers, "_chirp_use_gr_demod",
                           return_value=True):
        code, payload = _post_json(path, body)
    assert code == 200, payload
    assert payload["ok"] is True
    assert "pending_restart" not in payload, (
        f"{path} leaked pending_restart under chirp: {payload}"
    )
    # The "changed" boolean is still ours to report — it's a pure
    # state-delta signal independent of the restart concept.
    assert "changed" in payload


@pytest.mark.parametrize("path,body", [
    ("/api/airband/gain", {"band": "air", "gain_db": 30}),
    ("/api/airband/squelch", {"band": "air", "threshold_dbfs": -35}),
])
def test_gain_and_squelch_keep_pending_restart_on_rtl_airband(path, body):
    """Regression guard for the legacy path: with the chirp flag OFF
    the rtl-airband response shape MUST still include
    `pending_restart`.  sb5's auto-apply countdown depends on the
    field being present + truthy to schedule reset_radios."""
    with mock.patch.object(handlers, "_chirp_use_gr_demod",
                           return_value=False):
        code, payload = _post_json(path, body)
    assert code == 200, payload
    assert payload["ok"] is True
    assert "pending_restart" in payload
    assert isinstance(payload["pending_restart"], bool)


# --- /api/airband/squelch_preset ------------------------------------------


def _stub_plan(*, target: str = "airband", preset: str = "balanced") -> dict:
    """Shape of the dict returned by squelch_preset.apply_preset /
    chirp_adapter.apply_squelch_preset_via_chirp on a successful
    apply.  `changed=True` triggers `pending_restart=True` in the
    legacy path; the chirp path drops the field regardless."""
    return {
        "target": target,
        "preset": preset,
        "margin_db": 6,
        "threshold_median": -64.0,
        "noise_floor_median": -70.0,
        "freqs": [121.0, 122.0, 123.0],
        "stats_available": True,
        "changed": True,
        "threshold_count": 3,
        "written_at_ms": 1_700_000_000_000,
    }


def test_squelch_preset_omits_pending_restart_under_chirp():
    """Same contract as squelch/gain: chirp path drops the field."""
    with mock.patch.object(handlers, "_chirp_use_gr_demod",
                           return_value=True), \
         mock.patch.object(handlers._chirp_adapter,
                           "apply_squelch_preset_via_chirp",
                           return_value=_stub_plan()):
        code, payload = _post_json("/api/airband/squelch_preset",
                                   {"band": "air", "preset": "balanced"})
    assert code == 200, payload
    assert payload["ok"] is True
    assert "pending_restart" not in payload, (
        f"squelch_preset leaked pending_restart under chirp: {payload}"
    )
    # Apply-result metadata survives so the UI can still hydrate the
    # readout from the response.
    assert payload["preset"] == "balanced"
    assert payload["threshold_median"] == -64.0
    assert payload["noise_floor_median"] == -70.0


def test_squelch_preset_keeps_pending_restart_on_rtl_airband():
    """Legacy path: pending_restart must still be present (sb5's
    auto-apply countdown depends on it on the rtl-airband side)."""
    with mock.patch.object(handlers, "_chirp_use_gr_demod",
                           return_value=False), \
         mock.patch.object(handlers, "squelch_apply_preset",
                           return_value=_stub_plan()):
        code, payload = _post_json("/api/airband/squelch_preset",
                                   {"band": "air", "preset": "balanced"})
    assert code == 200, payload
    assert payload["ok"] is True
    assert "pending_restart" in payload
    assert payload["pending_restart"] is True


def test_squelch_preset_chirp_probe_failure_falls_back_to_legacy_shape():
    """Defensive: if the _chirp_use_gr_demod probe RAISES (which
    handlers.py wraps in try/except), the endpoint must fall back
    to the legacy rtl-airband response shape — including
    `pending_restart` — so a flaky probe never silently strips a
    field the legacy frontend depends on."""
    def _boom():
        raise RuntimeError("simulated probe failure")
    with mock.patch.object(handlers, "_chirp_use_gr_demod",
                           side_effect=_boom), \
         mock.patch.object(handlers, "squelch_apply_preset",
                           return_value=_stub_plan()):
        code, payload = _post_json("/api/airband/squelch_preset",
                                   {"band": "air", "preset": "balanced"})
    assert code == 200, payload
    assert "pending_restart" in payload, (
        "probe failure should NOT strip pending_restart — that field "
        "is the rtl-airband UI's only signal to schedule reset_radios"
    )
