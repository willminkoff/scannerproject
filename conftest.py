"""Root pytest conftest — deterministic import paths for the test suite.

This repo is a "run from the checkout" layout: ``ui``, ``chirp``, and
``broker`` are top-level packages imported as ``import ui.config`` etc., and a
handful of test modules import sibling helpers by bare name (e.g. the tuner
broker suite's ``from test_tuner_broker_helpers import ...``). Neither is
reliable across pytest's import modes unless the two relevant directories are
explicitly on ``sys.path``:

  * the repo root — so ``import ui`` / ``import chirp`` / ``import broker``
    resolve to the packages here regardless of pytest's rootdir guess or
    ``--import-mode`` (under ``importlib`` mode pytest does NOT prepend rootdir,
    which is what produced the "'ui' is not a package" collection error);
  * the ``tests`` directory — so cross-test helper modules import by bare name
    under any import mode (they otherwise only work under the legacy "prepend"
    mode that happens to add each test's own dir).

Pinning both here lets the ENTIRE suite run under a single invocation
(``pytest --import-mode=importlib``) — which is what the SB7.6 CI will use — so
broker, ui, and chirp tests stop needing mutually-incompatible invocations.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
for _p in (_ROOT, os.path.join(_ROOT, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# No test reaches a real radio.
# ---------------------------------------------------------------------------
# Armed here, at conftest import, which happens BEFORE pytest imports any test
# module -- so a module that reaches the network while merely being imported is
# caught too. That is the exact shape of both outages on 2026-11-10: importing
# sb3/ui/routes.py was enough to be able to command the airband radio and to
# cut power to the rack. See tests/netguard.py for the full story, the opt-out,
# and why the refusal is a BaseException.
import netguard  # noqa: E402  (needs the sys.path entries above)

netguard.install()

import pytest  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"{netguard.MARKER}: test genuinely needs off-box network access; "
        f"lifts the loopback-only socket guard for its duration",
    )


def pytest_report_header(config):
    if netguard.is_installed():
        return "netguard: armed (sockets restricted to loopback)"
    return (
        f"netguard: DISARMED by {netguard.ENV_OPT_OUT} -- tests can reach live radios"
    )


@pytest.fixture(autouse=True)
def _netguard(request):
    """Fail the test if it tried to reach off-box, even if the attempt was
    swallowed.

    netguard raises a BaseException at the call site, which is enough on its
    own in the main thread. This fixture is the backstop for the two cases
    where that is not enough: an attempt made on a background thread (where
    the exception dies with the thread), and a caller that catches BaseException
    on its way past. Without it, a blocked call could still leave a green test.
    """
    if request.node.get_closest_marker(netguard.MARKER):
        with netguard.allowed():
            yield
        return
    before = netguard.violation_count()
    yield
    leaked = netguard.violations()[before:]
    if leaked:
        pytest.fail(netguard.format_violations(leaked), pytrace=False)
