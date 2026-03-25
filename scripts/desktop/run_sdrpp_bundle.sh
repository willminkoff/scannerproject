#!/bin/bash
set -euo pipefail

APPDIR="${SDRPP_APPDIR:-$HOME/Applications/SDRPlusPlus}"
BIN_PATH="${APPDIR}/usr/bin/sdrpp"
LIB_PATH="${APPDIR}/usr/lib"
RUNTIME_LIB_PATH="${APPDIR}/runtime/usr/lib/x86_64-linux-gnu"
ROOT_PATH="${APPDIR}/root"
ZENITY_BIN="${ZENITY_BIN:-/usr/bin/zenity}"

if [[ ! -x "${BIN_PATH}" ]]; then
  echo "SDR++ is not installed at ${BIN_PATH}" >&2
  exit 1
fi

if [[ ! -f "${ROOT_PATH}/config.json" ]]; then
  echo "SDR++ config is missing at ${ROOT_PATH}/config.json" >&2
  exit 1
fi

if systemctl is-active --quiet rtl-airband || systemctl is-active --quiet scanner-digital; then
  MSG="SB3 is still using the RTL dongles. Turn SB3 Off in SB3 Power Control first."
  if [[ -n "${DISPLAY:-}" && -x "${ZENITY_BIN}" ]]; then
    "${ZENITY_BIN}" --error --title="SDR++" --width=420 --text="${MSG}" >/dev/null 2>&1 || true
  fi
  echo "${MSG}" >&2
  exit 1
fi

if [[ -n "${XDG_RUNTIME_DIR:-}" && -S "${XDG_RUNTIME_DIR}/bus" && -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
  export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
fi

LD_PATH="${LIB_PATH}:${RUNTIME_LIB_PATH}"
if [[ -n "${LD_LIBRARY_PATH:-}" ]]; then
  LD_PATH="${LD_PATH}:${LD_LIBRARY_PATH}"
fi

export LD_LIBRARY_PATH="${LD_PATH}"

exec "${BIN_PATH}" -r "${ROOT_PATH}" "$@"
