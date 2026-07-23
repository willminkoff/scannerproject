"""Entry point: ``python3 -m sb3.reconciler`` (and ``--once`` for one pass)."""

from __future__ import annotations

import sys

from .observer import main

if __name__ == "__main__":
    sys.exit(main())
