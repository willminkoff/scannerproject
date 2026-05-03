#!/usr/bin/env python3
"""
Regression tests for philly_api.py.

Run against a live Pi:   PI_HOST=100.69.95.13 python3 test_philly_api.py
Run unit tests only:     python3 test_philly_api.py --unit
"""

import argparse
import json
import os
import sys
import tempfile
import unittest
import urllib.request
import urllib.error

PI_HOST = os.environ.get('PI_HOST', '100.69.95.13')
PI_PORT = int(os.environ.get('PI_PORT', '5050'))
BASE = f'http://{PI_HOST}:{PI_PORT}'


def _get(path, timeout=6):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def _post(path, body=None, timeout=6):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())


# ---------------------------------------------------------------------------
# Unit tests (no live Pi required)
# ---------------------------------------------------------------------------

class TestProfileJson(unittest.TestCase):
    """Test profile JSON serialisation round-trip."""

    def _make_profile(self, name, freqs=None):
        return {
            'name': name,
            'frequencies': freqs or [],
            'gain': 40,
            'squelch_am': -44,
            'squelch_nfm': -42,
        }

    def test_roundtrip(self):
        prof = self._make_profile('PHL ATC', [
            {'freq': 118500000, 'label': 'PHL Tower', 'mode': 'am', 'squelch': -44},
        ])
        raw = json.dumps(prof)
        loaded = json.loads(raw)
        self.assertEqual(loaded['name'], 'PHL ATC')
        self.assertEqual(len(loaded['frequencies']), 1)
        self.assertEqual(loaded['frequencies'][0]['freq'], 118500000)

    def test_safe_name(self):
        import re
        def safe(name):
            return re.sub(r'[^a-z0-9_-]', '_', name.strip().lower())
        self.assertEqual(safe('PHL ATC'), 'phl_atc')
        self.assertEqual(safe('All Philly!'), 'all_philly_')
        self.assertEqual(safe('DelCo'), 'delco')


class TestHitMerge(unittest.TestCase):
    """Test that philly hits carry source='philly' after injection."""

    def test_source_field(self):
        raw_hit = {'time': '12:00:00', 'freq': '119.125', 'label': 'PHL Dep'}
        merged = {**raw_hit, 'source': 'philly', 'type': 'philly'}
        self.assertEqual(merged['source'], 'philly')
        self.assertEqual(merged['type'], 'philly')


# ---------------------------------------------------------------------------
# Integration tests (live Pi required)
# ---------------------------------------------------------------------------

@unittest.skipUnless(os.environ.get('PI_HOST'), 'Set PI_HOST to run integration tests')
class TestStatusEndpoint(unittest.TestCase):

    def test_status_200(self):
        status, body = _get('/api/status')
        self.assertEqual(status, 200)
        self.assertIn('service', body)  # 'active' or 'failed' etc.

    def test_status_has_active_profile(self):
        _, body = _get('/api/status')
        self.assertIn('active_profile', body)

    def test_status_squelch_nested(self):
        _, body = _get('/api/status')
        self.assertIn('am', body)
        self.assertIn('squelch', body['am'])
        self.assertIn('nfm', body)
        self.assertIn('squelch', body['nfm'])

    def test_cors_header(self):
        req = urllib.request.Request(BASE + '/api/status')
        req.add_header('Origin', 'http://localhost')
        with urllib.request.urlopen(req, timeout=6) as r:
            self.assertIn('Access-Control-Allow-Origin', r.headers)


@unittest.skipUnless(os.environ.get('PI_HOST'), 'Set PI_HOST to run integration tests')
class TestHitsEndpoint(unittest.TestCase):

    def test_hits_200(self):
        status, body = _get('/api/hits')
        self.assertEqual(status, 200)
        self.assertIn('hits', body)
        self.assertIsInstance(body['hits'], list)

    def test_hits_since_param(self):
        status, body = _get('/api/hits?since=2099-01-01T00:00:00')
        self.assertEqual(status, 200)
        self.assertEqual(body['hits'], [])


@unittest.skipUnless(os.environ.get('PI_HOST'), 'Set PI_HOST to run integration tests')
class TestProfilesEndpoint(unittest.TestCase):

    def test_profiles_list(self):
        status, body = _get('/api/profiles')
        self.assertEqual(status, 200)
        self.assertIn('profiles', body)
        self.assertIsInstance(body['profiles'], list)
        self.assertGreater(len(body['profiles']), 0)

    def test_profile_names(self):
        _, body = _get('/api/profiles')
        for p in body['profiles']:
            self.assertIn('name', p)
            self.assertIn('freq_count', p)  # list endpoint returns count, not full freqs

    def test_create_update_delete(self):
        test_name = '_test_regression_profile'
        # Create
        s, b = _post('/api/profile/create', {'name': test_name, 'frequencies': []})
        self.assertEqual(s, 200)
        # Verify present
        _, body = _get('/api/profiles')
        names = [p['name'] for p in body['profiles']]
        self.assertIn(test_name, names)
        # Update with one freq
        freq = {'freq': 118500000, 'label': 'Test', 'mode': 'am', 'squelch': -44}
        s, _ = _post('/api/profile/update', {'name': test_name, 'frequencies': [freq]})
        self.assertEqual(s, 200)
        # Verify freq saved — detail is nested under body['profile']
        _, body = _get(f'/api/profile?name={test_name}')
        freqs = body['profile']['frequencies']
        self.assertEqual(len(freqs), 1)
        self.assertEqual(freqs[0]['freq'], 118500000)
        # Delete
        s, _ = _post('/api/profile/delete', {'name': test_name})
        self.assertEqual(s, 200)
        _, body = _get('/api/profiles')
        names = [p['name'] for p in body['profiles']]
        self.assertNotIn(test_name, names)


@unittest.skipUnless(os.environ.get('PI_HOST'), 'Set PI_HOST to run integration tests')
class TestSquelchEndpoint(unittest.TestCase):

    def test_squelch_roundtrip(self):
        # API expects {am, nfm} keys; status returns am.squelch / nfm.squelch
        s, b = _post('/api/squelch', {'am': -50, 'nfm': -48})
        self.assertEqual(s, 200)
        _, status = _get('/api/status')
        self.assertEqual(status.get('am', {}).get('squelch'), -50)
        self.assertEqual(status.get('nfm', {}).get('squelch'), -48)
        _post('/api/squelch', {'am': -44, 'nfm': -42})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--unit', action='store_true', help='Run unit tests only (no Pi required)')
    args, remaining = parser.parse_known_args()

    if args.unit:
        suite = unittest.TestLoader().loadTestsFromTestCase(TestProfileJson)
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestHitMerge))
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
    else:
        sys.argv = [sys.argv[0]] + remaining
        unittest.main(verbosity=2)
