"""Pytest configuration for chirp tests.

Registers the `slow` marker used by Phase 2 stress tests so they can be
selected/deselected via `pytest -m slow` / `pytest -m "not slow"`.

Phase 2 default: slow tests RUN. The stress test is the primary smoke test
for the 31-channel architecture and Will explicitly asks for its results in
the report. To skip: `pytest -m "not slow" chirp/tests/`.
"""

from __future__ import annotations


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: stress / soak tests that take >10 s wall-clock",
    )
