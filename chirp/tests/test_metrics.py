"""SB6 Phase 1 — chirp.metrics stdlib exporter unit tests.

No GNU Radio dependency, so these run anywhere. They pin the Prometheus text
exposition format (label escaping, cumulative histogram buckets, +Inf == count)
and the /metrics HTTP endpoint contract.
"""
from __future__ import annotations

import urllib.request

import pytest

from chirp.metrics import MetricsRegistry, serve


def _lines(reg: MetricsRegistry):
    return reg.render().splitlines()


def test_gauge_and_counter_render():
    r = MetricsRegistry()
    r.set_gauge("chirp_config_load_status", 1, labels={"daemon": "airband"}, help="h")
    r.inc_counter("chirp_daemon_restart_total", 1,
                  labels={"daemon": "airband", "reason": "start"}, help="r")
    r.inc_counter("chirp_daemon_restart_total", 2,
                  labels={"daemon": "airband", "reason": "start"})
    out = _lines(r)
    assert "# TYPE chirp_config_load_status gauge" in out
    assert 'chirp_config_load_status{daemon="airband"} 1' in out
    assert "# TYPE chirp_daemon_restart_total counter" in out
    assert 'chirp_daemon_restart_total{daemon="airband",reason="start"} 3' in out


def test_set_counter_overwrites_absolute():
    r = MetricsRegistry()
    r.set_counter("chirp_flowgraph_alive_seconds_total", 5.0, labels={"daemon": "x"})
    r.set_counter("chirp_flowgraph_alive_seconds_total", 9.0, labels={"daemon": "x"})
    assert 'chirp_flowgraph_alive_seconds_total{daemon="x"} 9' in _lines(r)


def test_clear_others_keeps_single_series():
    r = MetricsRegistry()
    r.set_gauge("chirp_config_path", 1, labels={"daemon": "a", "path": "/old.json"},
                clear_others=True)
    r.set_gauge("chirp_config_path", 1, labels={"daemon": "a", "path": "/new.json"},
                clear_others=True)
    rows = [l for l in _lines(r) if l.startswith("chirp_config_path{")]
    assert rows == ['chirp_config_path{daemon="a",path="/new.json"} 1']


def test_histogram_buckets_cumulative_and_inf_equals_count():
    r = MetricsRegistry()
    for v in (0.0003, 0.02, 0.7, 3.0):
        r.observe("chirp_cmd_bus_request_seconds", v, labels={"command": "get_status"})
    out = _lines(r)
    assert "# TYPE chirp_cmd_bus_request_seconds histogram" in out

    def bucket(le):
        pref = f'chirp_cmd_bus_request_seconds_bucket{{command="get_status",le="{le}"}} '
        return int(next(l for l in out if l.startswith(pref)).split()[-1])

    # cumulative & monotonic
    assert bucket("0.001") == 1
    assert bucket("0.025") == 2
    assert bucket("1") == 3
    assert bucket("+Inf") == 4
    assert 'chirp_cmd_bus_request_seconds_count{command="get_status"} 4' in out
    # sum is the float total
    s = float(next(l for l in out
                   if l.startswith('chirp_cmd_bus_request_seconds_sum{command="get_status"} ')
                   ).split()[-1])
    assert abs(s - (0.0003 + 0.02 + 0.7 + 3.0)) < 1e-9


def test_label_value_escaping():
    r = MetricsRegistry()
    r.set_gauge("chirp_config_path", 1,
                labels={"daemon": "a", "path": 'a"b\\c'})
    line = next(l for l in _lines(r) if l.startswith("chirp_config_path{"))
    assert r'path="a\"b\\c"' in line


def test_pull_callback_runs_at_render():
    r = MetricsRegistry()
    ticks = {"n": 0}

    def cb(reg):
        ticks["n"] += 1
        reg.set_counter("chirp_flowgraph_alive_seconds_total", float(ticks["n"]),
                        labels={"daemon": "x"})

    r.register_callback(cb)
    _lines(r)         # render 1 -> n=1
    out = _lines(r)   # render 2 -> n=2
    assert 'chirp_flowgraph_alive_seconds_total{daemon="x"} 2' in out


def test_bad_callback_does_not_break_render():
    r = MetricsRegistry()
    r.set_gauge("ok", 1)
    r.register_callback(lambda reg: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "ok 1" in _lines(r)  # render survives the broken collector


def test_http_endpoint_serves_metrics():
    r = MetricsRegistry()
    r.set_gauge("chirp_config_load_status", 1, labels={"daemon": "airband"})
    httpd = serve(0, "127.0.0.1", registry=r)  # port 0 = ephemeral
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
            assert resp.status == 200
            body = resp.read().decode()
        assert 'chirp_config_load_status{daemon="airband"} 1' in body
        ctype = resp.headers.get("Content-Type", "")
        assert "text/plain" in ctype
    finally:
        httpd.shutdown()


def test_http_404_on_unknown_path():
    r = MetricsRegistry()
    httpd = serve(0, "127.0.0.1", registry=r)
    try:
        port = httpd.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
        assert ei.value.code == 404
    finally:
        httpd.shutdown()
