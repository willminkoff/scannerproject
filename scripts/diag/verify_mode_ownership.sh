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

SCANNER2_RTL_DEVICE="${SCANNER2_RTL_DEVICE:-70613472}"
ACTIVE_CONF="${RTLAIRBAND_ACTIVE_CONFIG_PATH:-/usr/local/etc/rtl_airband_active.conf}"
COMBINED_CONF="${COMBINED_CONFIG_PATH:-/usr/local/etc/rtl_airband_combined.conf}"
AIRONLY_CONF="${AIRONLY_CONFIG_PATH:-/usr/local/etc/rtl_airband_aironly.conf}"

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

echo "[diag] services: rtl=${rtl_active} ground=${ground_active} dmr=${dmr_active} dmr_controller=${dmr_controller_active}"
echo "[diag] mode=${mode}"

active_conf_real=""
if [[ -e "$ACTIVE_CONF" ]]; then
  active_conf_real=$(readlink -f "$ACTIVE_CONF" 2>/dev/null || echo "$ACTIVE_CONF")
fi

echo "[diag] active_conf=${ACTIVE_CONF} -> ${active_conf_real}"

has_scanner2=0
if [[ -f "$ACTIVE_CONF" ]] && grep -q "serial = \"$SCANNER2_RTL_DEVICE\"" "$ACTIVE_CONF"; then
  has_scanner2=1
elif [[ -f "$active_conf_real" ]] && grep -q "serial = \"$SCANNER2_RTL_DEVICE\"" "$active_conf_real"; then
  has_scanner2=1
fi

if [[ "$dmr_active" -eq 1 && "$has_scanner2" -eq 1 ]]; then
  echo "[diag] FAIL: DMR active but rtl_airband config still binds Scanner2" >&2
  exit 2
fi

if [[ "$dmr_active" -eq 1 && "$rtl_active" -eq 1 && "$active_conf_real" == "$COMBINED_CONF" ]]; then
  echo "[diag] FAIL: DMR active but rtl_airband is running combined config" >&2
  exit 3
fi

if [[ "$dmr_active" -eq 1 && "$ground_active" -eq 1 ]]; then
  echo "[diag] FAIL: rtl-airband-ground active during DMR mode" >&2
  exit 4
fi

echo "[diag] PASS: ownership looks sane"
