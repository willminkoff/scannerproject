#!/usr/bin/env python3
"""Regression tests for monitoring/icecast_exporter.py.

Pure-stdlib; no network, no running Icecast. Run with:

    python3 -m pytest monitoring/test_icecast_exporter.py -q
    # or, with no pytest installed:
    python3 monitoring/test_icecast_exporter.py
"""

import importlib.util
import os
import re
import xml.etree.ElementTree as ET

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "icecast_exporter", os.path.join(_HERE, "icecast_exporter.py")
)
ie = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ie)


def _stats(bytes_analog, bytes_vfo):
    return f"""<?xml version="1.0"?>
<icestats>
  <clients>4</clients>
  <sources>2</sources>
  <listeners>3</listeners>
  <source mount="/ANALOG.mp3">
    <listeners>2</listeners>
    <listener_peak>5</listener_peak>
    <audio_bitrate>64000</audio_bitrate>
    <total_bytes_sent>{bytes_analog}</total_bytes_sent>
    <total_bytes_read>9999</total_bytes_read>
  </source>
  <source mount="/VFO.mp3">
    <listeners>1</listeners>
    <listener_peak>1</listener_peak>
    <audio_bitrate>48000</audio_bitrate>
    <total_bytes_sent>{bytes_vfo}</total_bytes_sent>
    <total_bytes_read>5000</total_bytes_read>
  </source>
</icestats>"""


def _rewind(seconds):
    """Pretend `seconds` elapsed since the last poll (for rate math)."""
    with ie.STATE.lock:
        for m in ie.STATE.prev.values():
            m["ts"] -= seconds


def _byte_rate(text, mount):
    m = re.search(
        r'icecast_listener_byte_rate\{mount="%s"\} ([\d.]+)' % re.escape(mount),
        text,
    )
    assert m, f"no byte_rate for {mount} in payload"
    return float(m.group(1))


def setup_function(_):
    # Each test starts with clean rate state.
    with ie.STATE.lock:
        ie.STATE.prev.clear()


def test_first_poll_has_no_rate_baseline():
    out = ie.build_metrics(ET.fromstring(_stats(1_000_000, 500_000)), 0.01, True)
    assert "icecast_up 1" in out
    # No prior sample -> rate defaults to 0 on the very first poll.
    assert _byte_rate(out, "/ANALOG.mp3") == 0.0


def test_byte_rate_and_wedge_detection():
    ie.build_metrics(ET.fromstring(_stats(1_000_000, 500_000)), 0.01, True)
    _rewind(15.0)
    # ANALOG advanced 120000 bytes over ~15s -> ~8000 B/s (healthy).
    # VFO advanced 0 bytes -> 0 B/s with a listener (the VLC-wedge canary).
    out = ie.build_metrics(ET.fromstring(_stats(1_120_000, 500_000)), 0.01, True)
    assert 7900 < _byte_rate(out, "/ANALOG.mp3") < 8100
    assert _byte_rate(out, "/VFO.mp3") == 0.0
    assert 'icecast_listener_count{mount="/VFO.mp3"} 1' in out


def test_listener_and_bitrate_gauges():
    out = ie.build_metrics(ET.fromstring(_stats(10, 20)), 0.01, True)
    assert 'icecast_listener_count{mount="/ANALOG.mp3"} 2' in out
    assert 'icecast_listener_peak{mount="/ANALOG.mp3"} 5' in out
    assert 'icecast_audio_bitrate_bps{mount="/ANALOG.mp3"} 64000' in out
    assert 'icecast_total_bytes_sent_total{mount="/ANALOG.mp3"} 10' in out
    assert "icecast_sources 2" in out


def test_counter_reset_on_source_restart_does_not_emit_negative_rate():
    ie.build_metrics(ET.fromstring(_stats(1_000_000, 500_000)), 0.01, True)
    _rewind(15.0)
    # Source reconnected -> counter reset to a smaller value. Rate must clamp
    # to 0 rather than emit a negative spike.
    out = ie.build_metrics(ET.fromstring(_stats(2_000, 500_000)), 0.01, True)
    assert _byte_rate(out, "/ANALOG.mp3") == 0.0


def test_disappearing_mount_clears_rate_state():
    ie.build_metrics(ET.fromstring(_stats(1_000_000, 500_000)), 0.01, True)
    with ie.STATE.lock:
        assert "/VFO.mp3" in ie.STATE.prev
    # Next poll: only ANALOG present (VFO source disconnected).
    one_mount = """<?xml version="1.0"?>
<icestats><clients>1</clients><sources>1</sources><listeners>2</listeners>
  <source mount="/ANALOG.mp3"><listeners>2</listeners><listener_peak>5</listener_peak>
    <audio_bitrate>64000</audio_bitrate><total_bytes_sent>1100000</total_bytes_sent>
    <total_bytes_read>9999</total_bytes_read></source>
</icestats>"""
    ie.build_metrics(ET.fromstring(one_mount), 0.01, True)
    with ie.STATE.lock:
        assert "/VFO.mp3" not in ie.STATE.prev


def test_failure_path_sets_up_zero_without_crashing():
    out = ie.build_metrics(None, 0.5, False)
    assert "icecast_up 0" in out
    assert "icecast_scrape_duration_seconds 0.5" in out


def test_label_escaping():
    # A mount name with a quote must not break the exposition format.
    weird = """<?xml version="1.0"?>
<icestats><clients>0</clients><sources>1</sources><listeners>0</listeners>
  <source mount='/a"b.mp3'><listeners>0</listeners><total_bytes_sent>1</total_bytes_sent></source>
</icestats>"""
    out = ie.build_metrics(ET.fromstring(weird), 0.01, True)
    assert r'mount="/a\"b.mp3"' in out


if __name__ == "__main__":
    # Minimal runner so the file works without pytest installed.
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        setup_function(fn)
        fn()
        passed += 1
        print(f"PASS {fn.__name__}")
    print(f"\n{passed}/{len(fns)} tests passed")
