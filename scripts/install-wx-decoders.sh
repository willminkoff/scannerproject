#!/bin/bash
# install-wx-decoders.sh
# Install acarsdec, dumpvdl2, and radiosonde_auto_rx decoder binaries on the Micro box.
# Run as root: sudo bash scripts/install-wx-decoders.sh
# Set FORCE_REBUILD=1 to rebuild even if artifacts already exist.

set -euo pipefail

ACARSDEC_REPO="https://github.com/TLeconte/acarsdec.git"
DUMPVDL2_REPO="https://github.com/szpajder/dumpvdl2.git"
LIBACARS_REPO="https://github.com/szpajder/libacars.git"
AUTORX_REPO="https://github.com/projecthorus/radiosonde_auto_rx.git"
AUTORX_DIR="/opt/radiosonde_auto_rx"
FORCE="${FORCE_REBUILD:-0}"

# Temp build directory with trap cleanup
BUILD_DIR="$(mktemp -d /tmp/wx-decoder-build.XXXXXXXXXX)"
trap 'rm -rf "$BUILD_DIR"' EXIT

# Source environment for GROUND_RTL_SERIAL
[[ -f /etc/airband-ui.conf ]] && source /etc/airband-ui.conf
GROUND_SERIAL="${GROUND_RTL_SERIAL:-70613472}"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: this script must be run as root (sudo bash scripts/install-wx-decoders.sh)."
    exit 1
fi

echo "=== Wx Decoder Installation ==="
echo "  Ground RTL-SDR serial: ${GROUND_SERIAL}"
echo "  Force rebuild: ${FORCE}"
echo ""

# ---------------------------------------------------------------------------
# Artifact checks — decide what needs to be (re)built
# ---------------------------------------------------------------------------
need_acarsdec=true
need_libacars=true
need_dumpvdl2=true
need_autorx_clone=true
need_autorx_demod=true
need_autorx_venv=true
need_station_cfg=true

if [[ "$FORCE" != "1" ]]; then
    [[ -x /usr/local/bin/acarsdec ]] && need_acarsdec=false
    [[ -f /usr/local/lib/libacars-2.so ]] && need_libacars=false
    [[ -x /usr/local/bin/dumpvdl2 ]] && need_dumpvdl2=false
    [[ -f "$AUTORX_DIR/auto_rx.py" ]] && need_autorx_clone=false
    [[ -x "$AUTORX_DIR/dft_detect" && -x "$AUTORX_DIR/rs41mod" ]] && need_autorx_demod=false
    if [[ -d "$AUTORX_DIR/venv" ]] && head -1 "$AUTORX_DIR/auto_rx.py" 2>/dev/null | grep -q "^#!/opt/radiosonde_auto_rx/venv/bin/python3"; then
        need_autorx_venv=false
    fi
    if [[ -f "$AUTORX_DIR/station.cfg" ]] \
        && grep -q "payload_summary_port = 55673" "$AUTORX_DIR/station.cfg" \
        && grep -q "device_idx = ${GROUND_SERIAL}" "$AUTORX_DIR/station.cfg" \
        && grep -q "sdr_type = RTLSDR" "$AUTORX_DIR/station.cfg" \
        && grep -q "min_freq = 400.05" "$AUTORX_DIR/station.cfg" \
        && grep -q "max_freq = 406.0" "$AUTORX_DIR/station.cfg" \
        && grep -q "sdr_fm_path = rtl_fm" "$AUTORX_DIR/station.cfg" \
        && grep -q "sondehub_enabled = False" "$AUTORX_DIR/station.cfg"; then
        need_station_cfg=false
    fi
fi

# ---------------------------------------------------------------------------
# Step 1/9: System dependencies
# ---------------------------------------------------------------------------
echo "1/9: Installing system dependencies..."
apt-get update -qq
# acarsdec: build-essential cmake git librtlsdr-dev libusb-1.0-0-dev libjansson-dev
# dumpvdl2: libglib2.0-dev (+ libacars built from source)
# radiosonde_auto_rx (upstream native guide): python3 python3-venv sox git
#   build-essential libtool cmake usbutils libusb-1.0-0-dev rng-tools
#   libsamplerate-dev libatlas3-base libgfortran5 libopenblas-dev rtl-sdr
# rtl-sdr provides rtl_fm and rtl_power used by auto_rx (sdr_fm_path, sdr_power_path)
apt-get install -y -qq \
    build-essential cmake git libtool \
    librtlsdr-dev libusb-1.0-0-dev libjansson-dev \
    libglib2.0-dev \
    rtl-sdr usbutils \
    python3 python3-pip python3-venv \
    libsndfile1-dev libsamplerate-dev \
    libatlas3-base libgfortran5 libopenblas-dev \
    sox rng-tools
echo "  done"
echo ""

# ---------------------------------------------------------------------------
# Step 2/9: Build and install acarsdec
# ---------------------------------------------------------------------------
echo "2/9: Building acarsdec..."
if [[ "$need_acarsdec" == "false" ]]; then
    echo "  /usr/local/bin/acarsdec present, skipping (FORCE_REBUILD=1 to rebuild)"
else
    git clone --depth 1 "$ACARSDEC_REPO" "$BUILD_DIR/acarsdec"
    mkdir -p "$BUILD_DIR/acarsdec/build"
    cd "$BUILD_DIR/acarsdec/build"
    cmake .. -Drtl=ON
    make -j"$(nproc)"
    install -m 0755 acarsdec /usr/local/bin/acarsdec
    cd /
    echo "  installed /usr/local/bin/acarsdec"
fi
echo ""

# ---------------------------------------------------------------------------
# Step 3/9: Build and install libacars (required by dumpvdl2)
# ---------------------------------------------------------------------------
echo "3/9: Building libacars..."
if [[ "$need_libacars" == "false" ]]; then
    echo "  libacars-2 present, skipping (FORCE_REBUILD=1 to rebuild)"
else
    git clone --depth 1 "$LIBACARS_REPO" "$BUILD_DIR/libacars"
    mkdir -p "$BUILD_DIR/libacars/build"
    cd "$BUILD_DIR/libacars/build"
    cmake ..
    make -j"$(nproc)"
    make install
    ldconfig
    cd /
    echo "  installed libacars"
fi
echo ""

# ---------------------------------------------------------------------------
# Step 4/9: Build and install dumpvdl2
# ---------------------------------------------------------------------------
echo "4/9: Building dumpvdl2..."
if [[ "$need_dumpvdl2" == "false" ]]; then
    echo "  /usr/local/bin/dumpvdl2 present, skipping (FORCE_REBUILD=1 to rebuild)"
else
    git clone --depth 1 "$DUMPVDL2_REPO" "$BUILD_DIR/dumpvdl2"
    mkdir -p "$BUILD_DIR/dumpvdl2/build"
    cd "$BUILD_DIR/dumpvdl2/build"
    cmake ..
    make -j"$(nproc)"
    install -m 0755 dumpvdl2 /usr/local/bin/dumpvdl2
    cd /
    echo "  installed /usr/local/bin/dumpvdl2"
fi
echo ""

# ---------------------------------------------------------------------------
# Step 5/9: Clone radiosonde_auto_rx
# Step 6/9: Build demodulator binaries
# ---------------------------------------------------------------------------
# build.sh runs "make -C .." from auto_rx/, so it needs the full repo tree
# (parent Makefile, scan/, demod/, utils/, etc.). We clone the full repo,
# build in-place, then copy the auto_rx/ directory (now containing compiled
# binaries) to /opt/radiosonde_auto_rx.
if [[ "$need_autorx_clone" == "false" && "$need_autorx_demod" == "false" ]]; then
    echo "5/9: $AUTORX_DIR/auto_rx.py present, skipping clone"
    echo "6/9: dft_detect + rs41mod present, skipping build"
else
    echo "5/9: Cloning radiosonde_auto_rx..."
    git clone --depth 1 "$AUTORX_REPO" "$BUILD_DIR/radiosonde_auto_rx"

    echo "6/9: Building radiosonde demodulator binaries..."
    cd "$BUILD_DIR/radiosonde_auto_rx/auto_rx"
    bash build.sh
    cd /

    # Install: copy auto_rx/ contents (with compiled binaries) to /opt
    mkdir -p "$AUTORX_DIR"
    cp -a "$BUILD_DIR/radiosonde_auto_rx/auto_rx/." "$AUTORX_DIR/"
    chmod +x "$AUTORX_DIR/auto_rx.py"
    echo "  installed to $AUTORX_DIR with demod binaries"
fi
echo ""

# ---------------------------------------------------------------------------
# Step 7/9: Python virtual environment + shebang
# ---------------------------------------------------------------------------
echo "7/9: Setting up Python venv for radiosonde_auto_rx..."
if [[ "$need_autorx_venv" == "false" ]]; then
    echo "  venv present and shebang patched, skipping"
else
    python3 -m venv "$AUTORX_DIR/venv"
    "$AUTORX_DIR/venv/bin/pip" install --upgrade pip
    if [[ -f "$AUTORX_DIR/requirements.txt" ]]; then
        "$AUTORX_DIR/venv/bin/pip" install -r "$AUTORX_DIR/requirements.txt"
    fi
    echo "  venv created and dependencies installed"

    # Patch shebang to use venv python (keeps deployed service file unchanged)
    if ! head -1 "$AUTORX_DIR/auto_rx.py" | grep -q "^#!/opt/radiosonde_auto_rx/venv/bin/python3"; then
        sed -i "1s|^#!.*|#!/opt/radiosonde_auto_rx/venv/bin/python3|" "$AUTORX_DIR/auto_rx.py"
        echo "  patched auto_rx.py shebang → venv python"
    fi
fi
echo ""

# ---------------------------------------------------------------------------
# Step 8/9: Generate station.cfg
# ---------------------------------------------------------------------------
# auto_rx is strict: every section and key must be present or it exits.
# Start from the upstream station.cfg.example (shipped in the clone) and
# sed the handful of values we need to override for local-only operation.
echo "8/9: Generating station.cfg..."
STATION_CFG="$AUTORX_DIR/station.cfg"
if [[ "$need_station_cfg" == "false" ]]; then
    echo "  station.cfg present with correct contract, skipping"
else
    if [[ ! -f "$AUTORX_DIR/station.cfg.example" ]]; then
        echo "  ERROR: station.cfg.example not found in $AUTORX_DIR"
        exit 1
    fi
    cp "$AUTORX_DIR/station.cfg.example" "$STATION_CFG"
    # SDR serial — first [sdr_1] device_idx only (leave [sdr_2] alone)
    sed -i "0,/^device_idx = 0$/s//device_idx = ${GROUND_SERIAL}/" "$STATION_CFG"
    # Search band — US full range
    sed -i 's/^max_freq = 403.0$/max_freq = 406.0/' "$STATION_CFG"
    # Disable uploads
    sed -i 's/^sondehub_enabled = True$/sondehub_enabled = False/' "$STATION_CFG"
    sed -i 's/^uploader_callsign = CHANGEME$/uploader_callsign = SB3/' "$STATION_CFG"
    sed -i 's/^upload_listener_position = True$/upload_listener_position = False/' "$STATION_CFG"
    # UDP telemetry to localhost only (not broadcast)
    sed -i 's/^payload_summary_host = <broadcast>$/payload_summary_host = 127.0.0.1/' "$STATION_CFG"
    sed -i 's/^ozi_host = <broadcast>$/ozi_host = 127.0.0.1/' "$STATION_CFG"
    echo "  wrote $STATION_CFG from upstream example (serial=${GROUND_SERIAL}, UDP=127.0.0.1:55673)"
fi
echo ""

# ---------------------------------------------------------------------------
# Step 9/9: Verify
# ---------------------------------------------------------------------------
echo "9/9: Verification..."
systemctl daemon-reload

PASS=true

if [[ -x /usr/local/bin/acarsdec ]]; then
    echo "  acarsdec ............. OK (/usr/local/bin/acarsdec)"
else
    echo "  acarsdec ............. MISSING"
    PASS=false
fi

if [[ -x /usr/local/bin/dumpvdl2 ]]; then
    echo "  dumpvdl2 ............. OK (/usr/local/bin/dumpvdl2)"
else
    echo "  dumpvdl2 ............. MISSING"
    PASS=false
fi

if [[ -f /usr/local/lib/libacars-2.so ]]; then
    echo "  libacars ............. OK"
else
    echo "  libacars ............. MISSING"
    PASS=false
fi

if [[ -x "$AUTORX_DIR/auto_rx.py" ]]; then
    echo "  auto_rx.py ........... OK ($AUTORX_DIR/auto_rx.py)"
else
    echo "  auto_rx.py ........... MISSING"
    PASS=false
fi

if [[ -x "$AUTORX_DIR/dft_detect" ]]; then
    echo "  dft_detect ........... OK"
else
    echo "  dft_detect ........... MISSING (demod build may have failed)"
    PASS=false
fi

if [[ -x "$AUTORX_DIR/rs41mod" ]]; then
    echo "  rs41mod .............. OK"
else
    echo "  rs41mod .............. MISSING"
    PASS=false
fi

if command -v rtl_fm &>/dev/null; then
    echo "  rtl_fm ............... OK ($(command -v rtl_fm))"
else
    echo "  rtl_fm ............... MISSING (rtl-sdr package not installed)"
    PASS=false
fi

if command -v rtl_power &>/dev/null; then
    echo "  rtl_power ............ OK ($(command -v rtl_power))"
else
    echo "  rtl_power ............ MISSING (rtl-sdr package not installed)"
    PASS=false
fi

if [[ -d "$AUTORX_DIR/venv" ]]; then
    echo "  venv ................. OK"
else
    echo "  venv ................. MISSING"
    PASS=false
fi

if head -1 "$AUTORX_DIR/auto_rx.py" 2>/dev/null | grep -q "^#!/opt/radiosonde_auto_rx/venv/bin/python3"; then
    echo "  shebang .............. OK (venv python)"
else
    echo "  shebang .............. BAD (not patched to venv)"
    PASS=false
fi

CFG_OK=true
if [[ ! -f "$STATION_CFG" ]]; then
    CFG_OK=false
else
    grep -q "payload_summary_port = 55673"    "$STATION_CFG" || CFG_OK=false
    grep -q "device_idx = ${GROUND_SERIAL}"   "$STATION_CFG" || CFG_OK=false
    grep -q "sdr_type = RTLSDR"              "$STATION_CFG" || CFG_OK=false
    grep -q "min_freq = 400.05"              "$STATION_CFG" || CFG_OK=false
    grep -q "max_freq = 406.0"               "$STATION_CFG" || CFG_OK=false
    grep -q "sdr_fm_path = rtl_fm"           "$STATION_CFG" || CFG_OK=false
    grep -q "sondehub_enabled = False"       "$STATION_CFG" || CFG_OK=false
fi
if [[ "$CFG_OK" == "true" ]]; then
    echo "  station.cfg .......... OK (serial=${GROUND_SERIAL}, UDP=55673, 400-406 MHz)"
else
    echo "  station.cfg .......... BAD (missing or contract mismatch)"
    PASS=false
fi

echo ""
echo "Service status:"
systemctl status acarsdec --no-pager 2>&1 | head -3 || true
echo ""
systemctl status dumpvdl2 --no-pager 2>&1 | head -3 || true
echo ""
systemctl status radiosonde-auto-rx --no-pager 2>&1 | head -3 || true
echo ""

if [[ "$PASS" == "true" ]]; then
    echo "=== Installation Complete ==="
else
    echo "=== Installation Complete (with warnings — review above) ==="
    exit 1
fi
echo ""
echo "IMPORTANT: Services are NOT auto-started by this script."
echo ""
echo "Next steps:"
echo "  1. Select 'ACARS Weather' or 'Radiosonde' from the ground profile dropdown."
echo "  2. Check /api/wx/status to confirm acars_installed / vdl2_installed / radiosonde_installed = true."
echo "  3. 'Poll ACARS' starts both acarsdec + dumpvdl2 for combined ACARS/VDL2 data."
echo "  4. Monitor logs:"
echo "       journalctl -u acarsdec -f"
echo "       journalctl -u dumpvdl2 -f"
echo "       journalctl -u radiosonde-auto-rx -f"
