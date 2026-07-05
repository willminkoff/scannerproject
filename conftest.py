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
