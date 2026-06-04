"""Sanity tests — does the chirp module structure import cleanly?

These are import-only tests. They exercise the package layout and the ham2mon
GR 3.10 port at the import-graph level (does `import` succeed?). They do NOT
build a flowgraph or open hardware — that's exercised by Phase 1's
test_flowgraph_smoke.py.

Run from repo root:
    python3 -m pytest chirp/tests/ -v
"""


def test_import_chirp():
    import chirp  # noqa: F401
    assert chirp.__version__


def test_import_chirp_dsp():
    from chirp.dsp.ham2mon import receiver, scanner  # noqa: F401
    # Receiver should expose ham2mon's hier_blocks at module level.
    # ham2mon's receiver.py defines TunerDemodAM and TunerDemodNBFM.
    assert hasattr(receiver, "TunerDemodAM"), "TunerDemodAM missing from ported receiver"
    assert hasattr(receiver, "TunerDemodNBFM"), "TunerDemodNBFM missing from ported receiver"


def test_import_chirp_cmd():
    from chirp.cmd import schema, server  # noqa: F401
    # Schema placeholder must at least expose PROTOCOL_VERSION.
    assert schema.PROTOCOL_VERSION == 1


def test_chirp_dsp_pkg():
    import chirp.dsp  # noqa: F401
    import chirp.dsp.ham2mon  # noqa: F401
