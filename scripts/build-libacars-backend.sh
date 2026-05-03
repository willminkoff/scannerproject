#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_FILE="${SCRIPT_DIR}/libacars_backend.c"
OUTPUT_FILE="${1:-${SCRIPT_DIR}/libacars_backend}"
CC_BIN="${CC:-cc}"
PKG_CONFIG_BIN="${PKG_CONFIG:-pkg-config}"

if [[ ! -f "${SOURCE_FILE}" ]]; then
    echo "missing source: ${SOURCE_FILE}" >&2
    exit 1
fi

LIBACARS_PKG=""
if "${PKG_CONFIG_BIN}" --exists libacars 2>/dev/null; then
    LIBACARS_PKG="libacars"
elif "${PKG_CONFIG_BIN}" --exists libacars-2 2>/dev/null; then
    LIBACARS_PKG="libacars-2"
else
    echo "pkg-config could not find libacars" >&2
    exit 1
fi

LIBACARS_CFLAGS="$("${PKG_CONFIG_BIN}" --cflags "${LIBACARS_PKG}")"
LIBACARS_LIBS="$("${PKG_CONFIG_BIN}" --libs "${LIBACARS_PKG}")"

JANSSON_CFLAGS=""
JANSSON_LIBS="-ljansson"
if "${PKG_CONFIG_BIN}" --exists jansson 2>/dev/null; then
    JANSSON_CFLAGS="$("${PKG_CONFIG_BIN}" --cflags jansson)"
    JANSSON_LIBS="$("${PKG_CONFIG_BIN}" --libs jansson)"
fi

"${CC_BIN}" -O2 -std=c11 -Wall -Wextra -Werror ${LIBACARS_CFLAGS} ${JANSSON_CFLAGS} "${SOURCE_FILE}" -o "${OUTPUT_FILE}" ${LIBACARS_LIBS} ${JANSSON_LIBS}
chmod 0755 "${OUTPUT_FILE}"
echo "built ${OUTPUT_FILE}"
