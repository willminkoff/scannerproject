#!/usr/bin/env python3
"""SprontPi Radio Control UI - Main entry point."""
import os
import sys

# Setup paths - add parent directory so ui can be imported as a package
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Import and run the app as a module
if __name__ == "__main__":
    from ui.app import main
    main()
