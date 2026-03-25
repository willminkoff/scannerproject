#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
/usr/bin/python3 "${SCRIPT_DIR}/sb3_power.py" off
/usr/bin/python3 "${SCRIPT_DIR}/sb3_power.py" status
