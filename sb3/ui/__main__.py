"""`python3 -m sb3.ui` → start the UI server."""
import sys
from .server import main

if __name__ == "__main__":
    sys.exit(main())
