#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${AIRBAND_UI_ENV_FILE:-/etc/airband-ui.conf}"

if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
fi

if [[ -z "${LIBACARS_BRIDGE_CMD:-}" ]]; then
    echo "LIBACARS_BRIDGE_CMD is not set" >&2
    exit 1
fi

BACKEND_BIN="${LIBACARS_BACKEND_BIN:-${ROOT_DIR}/scripts/libacars_backend}"
if [[ ! -x "${BACKEND_BIN}" ]]; then
    echo "backend binary not executable: ${BACKEND_BIN}" >&2
    exit 1
fi

echo "bridge command: ${LIBACARS_BRIDGE_CMD}"
echo "backend binary: ${BACKEND_BIN}"

export LIBACARS_BRIDGE_CMD
export LIBACARS_BACKEND_BIN="${BACKEND_BIN}"
export PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

cd "${ROOT_DIR}"

python3 <<'PY'
import json
import os
from ui import libacars_bridge

sample = {
    "callsign": "UAL777",
    "vdl2": {
        "t": {"sec": 1712800002},
        "avlc": {
            "x25": {"clnp": {"pdu_type": "DT"}},
            "adsc": {
                "wx": {
                    "vertical_profile": {
                        "reports": [
                            {
                                "altitude": {"value": 280, "unit": "fl"},
                                "wind": {"direction": 240, "speed": 68, "unit": "kt"},
                            }
                        ]
                    }
                }
            },
        },
    },
}

raw, obs = libacars_bridge.decode_vdl2_frame_to_observations(sample)
if raw is None or len(obs) != 1:
    raise SystemExit("smoke test failed: expected one normalized observation")

result = {
    "backend": raw.decode_meta.get("backend"),
    "source": raw.source,
    "source_id": obs[0].source_id,
    "altitude_ft": obs[0].altitude_ft,
    "wind_speed_kt": obs[0].wind_speed_kt,
}
print(json.dumps(result, indent=2, sort_keys=True))
PY
