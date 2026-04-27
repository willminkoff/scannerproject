"""Tests for ui.favorites_runtime._rspduo_tuner_ids() RSPduo discovery.

The discovery path uses SoapySDR.Device.enumerate(driver=sdrplay).  These
tests inject a fake SoapySDR module into sys.modules so they run without a
real SDRplay device or the python3-soapysdr package being installed.
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from ui import favorites_runtime


class _FakeSoapyDevice:
    """Stand-in for SoapySDR.Device.  Only ``enumerate`` is exercised."""

    enumerate_calls: list[dict] = []
    enumerate_return: list = []
    enumerate_raises: BaseException | None = None

    @classmethod
    def enumerate(cls, kwargs):
        cls.enumerate_calls.append(dict(kwargs))
        if cls.enumerate_raises is not None:
            raise cls.enumerate_raises
        return list(cls.enumerate_return)


def _install_fake_soapy(monkeypatch):
    """Install a fake SoapySDR module under sys.modules for the duration of a test."""
    fake = types.ModuleType("SoapySDR")
    fake.Device = _FakeSoapyDevice  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "SoapySDR", fake)
    _FakeSoapyDevice.enumerate_calls = []
    _FakeSoapyDevice.enumerate_return = []
    _FakeSoapyDevice.enumerate_raises = None


class _MonkeyPatch:
    """Tiny mimic of pytest's monkeypatch for use in unittest.TestCase.setUp."""

    def __init__(self):
        self._restore: list[tuple[dict, str, object, bool]] = []

    def setitem(self, container, key, value):
        existed = key in container
        prev = container.get(key) if existed else None
        container[key] = value
        self._restore.append((container, key, prev, existed))

    def undo(self):
        for container, key, prev, existed in reversed(self._restore):
            if existed:
                container[key] = prev
            else:
                container.pop(key, None)
        self._restore = []


class RspduoDiscoveryTests(unittest.TestCase):

    def setUp(self):
        self.mp = _MonkeyPatch()
        _install_fake_soapy(self.mp)

    def tearDown(self):
        self.mp.undo()

    # ----------------------------------------------------------------------
    # Empty / failure paths
    # ----------------------------------------------------------------------

    def test_no_soapysdr_module_returns_empty(self):
        # Remove the fake module so the import inside _rspduo_tuner_ids fails.
        self.mp.undo()
        # Make SoapySDR import fail by leaving sys.modules without it AND
        # blocking import via a meta_path that raises ImportError on import.
        import importlib

        class _Blocker:
            def find_module(self, name, path=None):  # legacy api
                if name == "SoapySDR":
                    return self

            def find_spec(self, name, path, target=None):
                if name == "SoapySDR":
                    raise ImportError(name)

            def load_module(self, name):
                raise ImportError(name)

        sys.modules.pop("SoapySDR", None)
        sys.meta_path.insert(0, _Blocker())
        try:
            self.assertEqual(favorites_runtime._rspduo_tuner_ids(), [])
        finally:
            sys.meta_path.pop(0)
            importlib.invalidate_caches()

    def test_empty_enumeration_returns_empty(self):
        _FakeSoapyDevice.enumerate_return = []
        self.assertEqual(favorites_runtime._rspduo_tuner_ids(), [])

    def test_enumeration_exception_returns_empty(self):
        _FakeSoapyDevice.enumerate_raises = RuntimeError("daemon unreachable")
        self.assertEqual(favorites_runtime._rspduo_tuner_ids(), [])

    def test_only_non_rspduo_devices_returns_empty(self):
        """An RSP1/RSP1A/RSPdx (anything not RSPduo) is filtered out."""
        _FakeSoapyDevice.enumerate_return = [
            {"driver": "sdrplay", "serial": "ABC1234", "label": "RSP1A"},
            {"driver": "sdrplay", "serial": "DEF5678", "label": "RSPdx"},
        ]
        self.assertEqual(favorites_runtime._rspduo_tuner_ids(), [])

    # ----------------------------------------------------------------------
    # Successful discovery
    # ----------------------------------------------------------------------

    def test_one_rspduo_yields_only_tuner_1(self):
        """Tuner 2 is intentionally suppressed — same-process MA/SL is broken."""
        _FakeSoapyDevice.enumerate_return = [
            {"driver": "sdrplay", "serial": "180903ef32", "label": "SDRplay Dev0 RSPduo 180903EF32 - Single Tuner"},
        ]
        self.assertEqual(
            favorites_runtime._rspduo_tuner_ids(),
            ["RSPduo Tuner 1 SER#180903EF32"],
        )

    def test_serial_normalised_to_uppercase(self):
        _FakeSoapyDevice.enumerate_return = [
            {"driver": "sdrplay", "serial": "abcdef0123", "label": "RSPduo something"},
        ]
        ids = favorites_runtime._rspduo_tuner_ids()
        self.assertEqual(ids, ["RSPduo Tuner 1 SER#ABCDEF0123"])

    def test_dedupes_repeated_enumeration_entries(self):
        """SoapySDR returns one entry per RSPduo *mode* (ST, DT, MA, SL).
        All four reference the same physical device — emit Tuner 1 once.
        """
        _FakeSoapyDevice.enumerate_return = [
            {"driver": "sdrplay", "serial": "180903EF32", "label": "RSPduo - Single Tuner", "mode": "ST"},
            {"driver": "sdrplay", "serial": "180903EF32", "label": "RSPduo - Dual Tuner", "mode": "DT"},
            {"driver": "sdrplay", "serial": "180903EF32", "label": "RSPduo - Master", "mode": "MA"},
            {"driver": "sdrplay", "serial": "180903EF32", "label": "RSPduo - Slave", "mode": "SL"},
        ]
        self.assertEqual(
            favorites_runtime._rspduo_tuner_ids(),
            ["RSPduo Tuner 1 SER#180903EF32"],
        )

    def test_two_rspduos_each_get_tuner_1(self):
        _FakeSoapyDevice.enumerate_return = [
            {"driver": "sdrplay", "serial": "180903EF32", "label": "RSPduo A"},
            {"driver": "sdrplay", "serial": "9F00112233", "label": "RSPduo B"},
        ]
        self.assertEqual(
            favorites_runtime._rspduo_tuner_ids(),
            [
                "RSPduo Tuner 1 SER#180903EF32",
                "RSPduo Tuner 1 SER#9F00112233",
            ],
        )

    def test_blank_serial_skipped(self):
        _FakeSoapyDevice.enumerate_return = [
            {"driver": "sdrplay", "serial": "", "label": "RSPduo no-serial"},
            {"driver": "sdrplay", "serial": "GOOD123", "label": "RSPduo OK"},
        ]
        self.assertEqual(
            favorites_runtime._rspduo_tuner_ids(),
            ["RSPduo Tuner 1 SER#GOOD123"],
        )

    def test_uses_hardware_field_when_label_absent(self):
        """SoapySDR sometimes exposes the hardware string via 'hardware', not 'label'."""
        _FakeSoapyDevice.enumerate_return = [
            {"driver": "sdrplay", "serial": "1234", "hardware": "RSPduo"},
        ]
        self.assertEqual(
            favorites_runtime._rspduo_tuner_ids(),
            ["RSPduo Tuner 1 SER#1234"],
        )

    def test_handles_real_soapy_kwargs_string_form(self):
        """Regression: SoapySDRKwargs has no .get() — must parse via str()."""
        class _RealKwargsFake:
            def __init__(self, repr_str):
                self._repr_str = repr_str
            def __str__(self):
                return self._repr_str
            # Deliberately NO .get(), .keys(), __contains__, __getitem__.

        _FakeSoapyDevice.enumerate_return = [
            _RealKwargsFake("{driver=sdrplay, label=SDRplay Dev0 RSPduo 180903EF32 - Single Tuner, mode=ST, serial=180903EF32}"),
            _RealKwargsFake("{driver=sdrplay, label=SDRplay Dev1 RSPduo 180903EF32 - Dual Tuner, mode=DT, serial=180903EF32}"),
        ]
        # Clean serial (no trailing brace) and dedup across the 4 modes.
        self.assertEqual(
            favorites_runtime._rspduo_tuner_ids(),
            ["RSPduo Tuner 1 SER#180903EF32"],
        )


class ParseSoapyKwargsTests(unittest.TestCase):
    """_parse_soapy_kwargs handles both dict-like and SoapySDRKwargs string-rep inputs."""

    def test_plain_dict_passthrough(self):
        kw = {"driver": "sdrplay", "serial": "180903EF32"}
        self.assertEqual(
            favorites_runtime._parse_soapy_kwargs(kw),
            {"driver": "sdrplay", "serial": "180903EF32"},
        )

    def test_soapy_kwargs_str_form_strips_braces(self):
        """The Soapy-Kwargs str() form is '{key=value, key=value}' — outer braces stripped."""
        # Simulate by passing an object whose str() returns the canonical form.
        class _FakeKwargs:
            def __str__(self):
                return "{driver=sdrplay, label=SDRplay Dev0 RSPduo 180903EF32 - Single Tuner, mode=ST, serial=180903EF32}"

        result = favorites_runtime._parse_soapy_kwargs(_FakeKwargs())
        self.assertEqual(result["driver"], "sdrplay")
        self.assertEqual(result["serial"], "180903EF32")  # no trailing }
        self.assertEqual(result["mode"], "ST")
        self.assertEqual(
            result["label"],
            "SDRplay Dev0 RSPduo 180903EF32 - Single Tuner",
        )

    def test_serial_is_clean_no_brace_when_last_field(self):
        """Regression for the bug where serial='180903EF32}' broke discovery."""
        class _FakeKwargs:
            def __str__(self):
                return "{driver=sdrplay, serial=180903EF32}"

        result = favorites_runtime._parse_soapy_kwargs(_FakeKwargs())
        self.assertEqual(result["serial"], "180903EF32")

    def test_empty_input(self):
        self.assertEqual(favorites_runtime._parse_soapy_kwargs(None), {})
        self.assertEqual(favorites_runtime._parse_soapy_kwargs(""), {})

    def test_no_braces(self):
        """Bare 'a=1, b=2' (no surrounding braces) still parses."""
        class _FakeKwargs:
            def __str__(self):
                return "a=1, b=2"

        self.assertEqual(
            favorites_runtime._parse_soapy_kwargs(_FakeKwargs()),
            {"a": "1", "b": "2"},
        )


if __name__ == "__main__":
    unittest.main()
