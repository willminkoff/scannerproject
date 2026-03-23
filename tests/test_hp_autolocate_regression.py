import unittest
from unittest import mock

from ui import handlers
from ui.hp_state import HPState


class HpAutoLocateRegressionTests(unittest.TestCase):
    def _new_state(self) -> HPState:
        state = HPState.default()
        state.enabled_service_tags = [2, 7, 23]
        return state

    def test_explicit_lat_lon_save_persists_without_zip_resolve(self):
        state = self._new_state()
        state.lat = 0.0
        state.lon = 0.0
        resolver = mock.Mock(return_value=(36.10, -86.80))

        handlers._apply_hp_state_form(
            state,
            {"use_location": True, "lat": 36.1234, "lon": -86.5678},
            resolve_postal_lookup=resolver,
        )

        self.assertTrue(state.use_location)
        self.assertAlmostEqual(36.1234, state.lat)
        self.assertAlmostEqual(-86.5678, state.lon)
        resolver.assert_not_called()

    def test_strict_location_toggle_persists(self):
        state = self._new_state()
        self.assertFalse(state.strict_location)

        handlers._apply_hp_state_form(state, {"strict_location": True})
        self.assertTrue(state.strict_location)

        handlers._apply_hp_state_form(state, {"strict_location": False})
        self.assertFalse(state.strict_location)

    def test_resolve_zip_true_resolves_only_when_use_location_true(self):
        state = self._new_state()
        state.lat = 10.0
        state.lon = 20.0
        resolver = mock.Mock(return_value=(36.0662, -86.9639))

        handlers._apply_hp_state_form(
            state,
            {"zip": "37221", "use_location": False, "resolve_zip": True},
            resolve_postal_lookup=resolver,
        )

        self.assertAlmostEqual(10.0, state.lat)
        self.assertAlmostEqual(20.0, state.lon)
        resolver.assert_not_called()

        handlers._apply_hp_state_form(
            state,
            {"use_location": True, "resolve_zip": True},
            resolve_postal_lookup=resolver,
        )
        resolver.assert_called_once_with("37221", "US")
        self.assertAlmostEqual(36.0662, state.lat)
        self.assertAlmostEqual(-86.9639, state.lon)

    def test_resolve_zip_true_missing_zip_returns_expected_error(self):
        state = self._new_state()
        state.zip = ""

        with self.assertRaisesRegex(ValueError, "missing zip"):
            handlers._apply_hp_state_form(
                state,
                {"use_location": True, "resolve_zip": True},
                resolve_postal_lookup=mock.Mock(),
            )

    def test_invalid_lat_or_lon_returns_expected_validation_error(self):
        for field in ("lat", "lon"):
            with self.subTest(field=field):
                state = self._new_state()
                payload = {field: "not-a-number"}
                with self.assertRaisesRegex(ValueError, f"invalid {field}"):
                    handlers._apply_hp_state_form(state, payload)

    def test_zip_resolution_failure_keeps_prior_coordinates(self):
        state = self._new_state()
        state.use_location = True
        state.zip = "37221"
        state.lat = 36.12
        state.lon = -86.67

        with self.assertRaisesRegex(ValueError, "unable to resolve zip"):
            handlers._apply_hp_state_form(
                state,
                {"resolve_zip": True},
                resolve_postal_lookup=mock.Mock(return_value=None),
            )

        self.assertAlmostEqual(36.12, state.lat)
        self.assertAlmostEqual(-86.67, state.lon)

    def test_state_save_triggers_runtime_sync_payload_contract(self):
        state = self._new_state()
        expected_sync = {"ok": True, "changed": True, "errors": []}
        with mock.patch.object(state, "save", return_value=None) as save_state, mock.patch.object(
            handlers,
            "sync_scan_pool_to_runtime",
            return_value=expected_sync,
        ) as sync_runtime:
            payload = handlers._save_hp_state_with_sync(state)

        save_state.assert_called_once_with()
        sync_runtime.assert_called_once_with(force=True)
        self.assertTrue(payload["ok"])
        self.assertIn("state", payload)
        self.assertIn("favorites_runtime_sync", payload)
        sync = payload["favorites_runtime_sync"]
        self.assertEqual(expected_sync["ok"], sync["ok"])
        self.assertEqual(expected_sync["changed"], sync["changed"])
        self.assertEqual(expected_sync["errors"], sync["errors"])
        self.assertTrue(sync["request_complete"])
        self.assertFalse(sync["pending"])

    def test_normalize_ip_geolookup_payload_accepts_ipapi_shape(self):
        payload = {
            "latitude": 36.174,
            "longitude": -86.767,
            "postal": "37201",
            "region": "Tennessee",
        }
        parsed = handlers._normalize_ip_geolookup_payload(payload)
        self.assertAlmostEqual(36.174, parsed["lat"])
        self.assertAlmostEqual(-86.767, parsed["lon"])
        self.assertEqual("37201", parsed["zip"])
        self.assertEqual("Tennessee", parsed["county"])

    def test_normalize_ip_geolookup_payload_accepts_ipwho_shape(self):
        payload = {
            "success": True,
            "lat": 36.124,
            "lon": -86.691,
            "zip": "37011",
            "region": "Tennessee",
        }
        parsed = handlers._normalize_ip_geolookup_payload(payload)
        self.assertAlmostEqual(36.124, parsed["lat"])
        self.assertAlmostEqual(-86.691, parsed["lon"])
        self.assertEqual("37011", parsed["zip"])
        self.assertEqual("Tennessee", parsed["county"])

    def test_normalize_reverse_payload_extracts_county_from_locality_info(self):
        payload = {
            "postcode": "37201",
            "localityInfo": {
                "administrative": [
                    {"name": "Davidson County", "description": "county"},
                ]
            },
        }
        parsed = handlers._normalize_reverse_geolookup_payload(payload)
        self.assertEqual("37201", parsed["zip"])
        self.assertEqual("Davidson County", parsed["county"])

    def test_resolve_ip_geolocation_falls_back_to_next_provider(self):
        providers = ("https://a.invalid/json/", "https://b.example/json/")
        with mock.patch.object(handlers, "_HP_IP_GEOLOOKUP_PROVIDERS", providers), mock.patch.object(
            handlers,
            "_fetch_json_url_with_tls_fallback",
            side_effect=[
                RuntimeError("first failed"),
                {"lat": 36.2, "lon": -86.8, "zip": "37221", "region": "Tennessee"},
            ],
        ) as fetch_mock:
            resolved = handlers._resolve_ip_geolocation()

        self.assertAlmostEqual(36.2, resolved["lat"])
        self.assertAlmostEqual(-86.8, resolved["lon"])
        self.assertEqual("37221", resolved["zip"])
        self.assertEqual("https://b.example/json/", resolved["provider"])
        self.assertEqual(2, fetch_mock.call_count)


if __name__ == "__main__":
    unittest.main()
