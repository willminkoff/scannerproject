"""sb3.reconciler — Phase 4.1: the PASSIVE reconciler.

Runs as ``com.scannerproject.sb3-reconciler``, reads live backend state every
30 s, classifies how it differs from the loaded profiles, and writes one
structured line per role to ``~/Library/Logs/sb3/reconciler.log``.

**It does not act.**  Phase 4.1 exists to answer a question before granting any
power: *what does drift on this box actually look like?*  Letting something
correct state it has never watched is how you get ``sdrangel-restore.py``,
which re-asserts a stored config every 10 minutes and would clobber a human
mid-tune.  §4.4 is explicit that on divergence the LIVE backend wins.

Layout:
  * :mod:`sb3.reconciler.classifier` — pure drift categories (CLEAN / BENIGN /
    RECOVERABLE / BROKEN).  No I/O; fully unit-testable.
  * :mod:`sb3.reconciler.observer`   — the loop.  Reads only via
    :mod:`sb3.backends` (read-only by contract) and logs.
"""

from __future__ import annotations

from .classifier import (BENIGN, BROKEN, CLEAN, RECOVERABLE, Classification,
                         classify_digital, classify_role, classify_system,
                         format_line)
from .observer import Observer, main

__all__ = [
    "CLEAN", "BENIGN", "RECOVERABLE", "BROKEN",
    "Classification", "classify_role", "classify_digital", "classify_system",
    "format_line", "Observer", "main",
]
