#!/bin/bash
# rtl-airband-filter.sh
# Wrapper for rtl_airband that applies a low-pass filter via SoX
# Reads filter configuration from JSON files

# Configuration
RTLAIRBAND_BIN="${RTLAIRBAND_BIN:-/usr/local/bin/rtl_airband}"
CONFIG_FILE="${1:-}"
FILTER_CONFIG_DIR="${FILTER_CONFIG_DIR:-/run}"
SCANNER1_RTL_DEVICE="${SCANNER1_RTL_DEVICE:-}"
SCANNER2_RTL_DEVICE="${SCANNER2_RTL_DEVICE:-}"

# Validate config file
if [[ -z "$CONFIG_FILE" ]]; then
    echo "Usage: rtl-airband-filter.sh <config_file>" >&2
    exit 1
fi


# Inject serial/index bindings for standalone configs when missing
TMP_CONFIG=""
cleanup_tmp() {
    if [[ -n "$TMP_CONFIG" ]] && [[ -f "$TMP_CONFIG" ]]; then
        rm -f "$TMP_CONFIG"
    fi
}
trap cleanup_tmp EXIT

inject_device_binding() {
    if grep -qE '^\s*serial\s*=' "$CONFIG_FILE"; then
        return
    fi
    local airband_flag
    airband_flag=$(grep -m1 -E '^\s*airband\s*=' "$CONFIG_FILE" | tr -d ' ;' | awk -F= '{print $2}')
    local serial=""
    local index=""
    if [[ "$airband_flag" == "true" ]]; then
        serial="$SCANNER1_RTL_DEVICE"
        index="0"
    else
        serial="$SCANNER2_RTL_DEVICE"
        index="1"
    fi
    if [[ -z "$serial" ]]; then
        return
    fi
    TMP_CONFIG="/run/rtl_airband_injected_$$.conf"
    awk -v serial="$serial" -v index="$index" '
        {print}
        /type[[:space:]]*=[[:space:]]"rtlsdr"/ && !inserted {
            print "  serial = \"" serial "\";";
            print "  index = " index ";";
            inserted=1
        }
    ' "$CONFIG_FILE" > "$TMP_CONFIG"
    if grep -qE '^\s*serial\s*=' "$TMP_CONFIG"; then
        CONFIG_FILE="$TMP_CONFIG"
    else
        rm -f "$TMP_CONFIG"
        TMP_CONFIG=""
    fi
}

inject_device_binding

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

# Execute rtl_airband with output piped through sox low-pass filter
# rtl_airband outputs raw PCM audio to stdout
# sox applies low-pass filter to reduce high-frequency noise
# Final output goes to stdout (connected to Icecast by the shell)
# Note: We do NOT use 'exec' here so the pipe to sox is properly maintained
"$RTLAIRBAND_BIN" -F -e -c "$CONFIG_FILE" | sox -t raw -r 48000 -b 16 -c 1 -e signed-integer - -t raw - lowpass "$CUTOFF"
