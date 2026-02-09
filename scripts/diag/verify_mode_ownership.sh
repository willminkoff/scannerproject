#!/bin/bash
set -euo pipefail

CONF="/etc/airband-ui.conf"
if [[ -f "$CONF" ]]; then
  # shellcheck disable=SC1090
  . "$CONF"
fi

UNIT_RTL="${UNIT_RTL:-rtl-airband}"
UNIT_DMR="${UNIT_DMR:-dmr-decode}"
UNIT_DMR_CONTROLLER="${UNIT_DMR_CONTROLLER:-dmr-controller}"
UNIT_GROUND="${UNIT_GROUND:-rtl-airband-ground}"

SCANNER1_RTL_DEVICE="${SCANNER1_RTL_DEVICE:-00000002}"
SCANNER2_RTL_DEVICE="${SCANNER2_RTL_DEVICE:-70613472}"
COMBINED_CONF="${COMBINED_CONFIG_PATH:-/usr/local/etc/rtl_airband_combined.conf}"
GROUND_SELECTED_PATH="${GROUND_SELECTED_PATH:-/run/airband_ui_ground_selected.json}"

read_ground_mode() {
  if [[ ! -f "$GROUND_SELECTED_PATH" ]]; then
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$GROUND_SELECTED_PATH" <<'PY'
import json
import sys
path = sys.argv[1]
try:
    data = json.load(open(path, "r", encoding="utf-8"))
except Exception:
    sys.exit(0)
mode = data.get("mode")
if mode in ("analog", "dmr"):
    print(mode)
PY
  fi
}

conf_has_serial() {
  local conf="$1"
  local serial="$2"
  if [[ -f "$conf" ]] && grep -q "serial = \"$serial\"" "$conf"; then
    return 0
  fi
  return 1
}

printf '[diag] SCANNER1_RTL_DEVICE=%s\n' "$SCANNER1_RTL_DEVICE"
printf '[diag] SCANNER2_RTL_DEVICE=%s\n' "$SCANNER2_RTL_DEVICE"

if command -v rtl_test >/dev/null 2>&1; then
  echo "[diag] rtl_test -t:"
  rtl_test -t 2>&1 | sed 's/^/  /'
else
  echo "[diag] rtl_test not found"
fi

if command -v lsof >/dev/null 2>&1; then
  echo "[diag] USB device owners (rtl_*):"
  lsof /dev/bus/usb/*/* 2>/dev/null | grep -E 'rtl_airband|rtl_fm' | sed 's/^/  /' || echo "  none"
else
  echo "[diag] lsof not found"
fi

rtl_active=0
systemctl is-active --quiet "$UNIT_RTL" && rtl_active=1

ground_active=0
systemctl is-active --quiet "$UNIT_GROUND" && ground_active=1

dmr_active=0
systemctl is-active --quiet "$UNIT_DMR" && dmr_active=1

dmr_controller_active=0
systemctl is-active --quiet "$UNIT_DMR_CONTROLLER" && dmr_controller_active=1

mode="unknown"
if [[ "$dmr_active" -eq 1 ]]; then
  mode="dmr"
elif [[ "$rtl_active" -eq 1 ]]; then
  mode="analog"
fi

selected_mode="$(read_ground_mode || true)"
if [[ -n "$selected_mode" ]]; then
  mode="$selected_mode"
fi

echo "[diag] services: rtl=${rtl_active} ground=${ground_active} dmr=${dmr_active} dmr_controller=${dmr_controller_active}"
echo "[diag] mode=${mode}"
echo "[diag] combined_conf=${COMBINED_CONF}"

has_s1=0
has_s2=0
if conf_has_serial "$COMBINED_CONF" "$SCANNER1_RTL_DEVICE"; then
  has_s1=1
fi
if conf_has_serial "$COMBINED_CONF" "$SCANNER2_RTL_DEVICE"; then
  has_s2=1
fi

echo "[diag] combined_conf serials: scanner1=${has_s1} scanner2=${has_s2}"

if [[ "$mode" == "dmr" ]]; then
  if [[ "$has_s2" -eq 1 ]]; then
    echo "[diag] FAIL: DMR mode but combined config still includes Scanner2" >&2
    echo "[diag] check: GROUND_CONFIG_PATH symlink should point to rtl_airband_none_ground.conf" >&2
    exit 2
  fi
  if [[ "$has_s1" -ne 1 ]]; then
    echo "[diag] FAIL: DMR mode but combined config missing Scanner1" >&2
    exit 3
  fi
fi

if [[ "$mode" == "analog" ]]; then
  if [[ "$has_s1" -ne 1 || "$has_s2" -ne 1 ]]; then
    echo "[diag] FAIL: Analog mode expects both Scanner1 + Scanner2 in combined config" >&2
    exit 4
  fi
fi

if [[ "$dmr_active" -eq 1 && "$ground_active" -eq 1 ]]; then
  echo "[diag] FAIL: rtl-airband-ground active during DMR mode" >&2
  exit 5
fi

echo "[diag] PASS: ownership looks sane"
