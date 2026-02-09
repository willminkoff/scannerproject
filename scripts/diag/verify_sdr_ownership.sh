#!/bin/bash
set -euo pipefail

CONF="/etc/airband-ui.conf"
if [[ -f "$CONF" ]]; then
  # shellcheck disable=SC1090
  . "$CONF"
fi

UNIT_RTL="${UNIT_RTL:-rtl-airband}"
UNIT_GROUND="${UNIT_GROUND:-rtl-airband-ground}"
UNIT_DMR="${UNIT_DMR:-dmr-decode}"
UNIT_DMR_CONTROLLER="${UNIT_DMR_CONTROLLER:-dmr-controller}"

printf '[diag] SCANNER1_RTL_DEVICE=%s\n' "${SCANNER1_RTL_DEVICE:-unset}"
printf '[diag] SCANNER2_RTL_DEVICE=%s\n' "${SCANNER2_RTL_DEVICE:-unset}"

if command -v rtl_test >/dev/null 2>&1; then
  echo "[diag] rtl_test -t:"
  rtl_test -t 2>&1 | sed 's/^/  /'
else
  echo "[diag] rtl_test not found"
fi

if command -v lsof >/dev/null 2>&1; then
  echo "[diag] USB device owners (rtl_*):"
  lsof /dev/bus/usb/*/* 2>/dev/null | grep -E 'rtl_|rtl-fm|rtl_airband' | sed 's/^/  /' || echo "  none"
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

echo "[diag] services: rtl=${rtl_active} ground=${ground_active} dmr=${dmr_active} dmr_controller=${dmr_controller_active}"

if [[ "$dmr_active" -eq 1 && ("$rtl_active" -eq 1 || "$ground_active" -eq 1) ]]; then
  echo "[diag] FAIL: DMR and analog ground are active simultaneously" >&2
  exit 2
fi

echo "[diag] PASS: ownership looks sane"
