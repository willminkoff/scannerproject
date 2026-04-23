#!/bin/bash
# rtl-airband-filter.sh
# Wrapper for rtl_airband that applies a low-pass filter via SoX
# Reads filter configuration from JSON files

# set -u catches unset variables in the config section above.
# Intentionally NOT using -e or -o pipefail: the final rtl_airband|sox pipeline
# must survive transient sox exits (SIGPIPE, audio glitch) without triggering a
# systemd restart.  We propagate rtl_airband's exit code manually via PIPESTATUS.
set -u

# Configuration
RTLAIRBAND_BIN="${RTLAIRBAND_BIN:-/usr/local/bin/rtl_airband}"
CONFIG_FILE="${1:-}"
FILTER_CONFIG_DIR="${FILTER_CONFIG_DIR:-/run}"

# Validate config file
if [[ -z "$CONFIG_FILE" ]]; then
    echo "Usage: rtl-airband-filter.sh <config_file>" >&2
    exit 1
fi
if [[ ! -x "$RTLAIRBAND_BIN" ]]; then
    echo "rtl_airband binary not executable: $RTLAIRBAND_BIN" >&2
    exit 1
fi
if ! command -v sox >/dev/null 2>&1; then
    echo "sox not found in PATH" >&2
    exit 1
fi

# Determine which filter config file to use based on config file path
if [[ "$CONFIG_FILE" == *"ground"* ]]; then
    FILTER_CONFIG="$FILTER_CONFIG_DIR/rtl_airband_ground_filter.json"
else
    FILTER_CONFIG="$FILTER_CONFIG_DIR/rtl_airband_filter.json"
fi

# Default filter cutoff (Hz) - tuned for voice clarity
DEFAULT_CUTOFF=3500

# Parse filter cutoff from JSON config
parse_filter_cutoff() {
    local config_file="$1"
    if [[ ! -f "$config_file" ]]; then
        echo "$DEFAULT_CUTOFF"
        return
    fi
    # Extract cutoff_hz from JSON using grep to get the value
    # Pattern: "cutoff_hz": 3200.0
    local cutoff=$(grep -o '"cutoff_hz"[^,}]*' "$config_file" 2>/dev/null | sed 's/.*: *//')
    if [[ -z "$cutoff" ]] || ! [[ "$cutoff" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        echo "$DEFAULT_CUTOFF"
    else
        echo "$cutoff"
    fi
}

# Get the current cutoff frequency
CUTOFF=$(parse_filter_cutoff "$FILTER_CONFIG")
if [[ -z "$CUTOFF" ]] || [[ "$CUTOFF" == "None" ]]; then
    CUTOFF="$DEFAULT_CUTOFF"
fi

# Execute rtl_airband with output piped through sox low-pass filter.
# rtl_airband outputs raw PCM audio to stdout; sox applies the lowpass filter.
# We use PIPESTATUS to exit with rtl_airband's code, not sox's.  This means
# a transient sox failure (SIGPIPE, audio glitch, momentary EOF) does NOT
# cause systemd to restart the service — only a real rtl_airband crash does.
"$RTLAIRBAND_BIN" -F -e -c "$CONFIG_FILE" | sox -t raw -r 48000 -b 16 -c 1 -e signed-integer - -t raw - lowpass "$CUTOFF"
exit ${PIPESTATUS[0]}
