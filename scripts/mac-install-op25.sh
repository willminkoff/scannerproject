#!/bin/bash
# mac-install-op25.sh
# Install op25 (boatbod fork) on macOS for use with SDRplay RSPduo.
# Assumes:
#   - macOS Apple Silicon (M-series) or Intel
#   - SDRplay API 3.x already installed (e.g. via SDRconnect or standalone .pkg)
#   - SDR++ already installed (you have it in /Applications)
#
# Idempotent — safe to re-run. Stops on first error.
#
# Run as your normal user (not sudo). The script will sudo where needed.

set -u  # error on unset vars; do NOT set -e — we want to continue past benign checks

OP25_DIR="${HOME}/op25"
PYTHON_BIN="$(command -v python3 || echo python3)"

step() { printf "\n\033[1;36m== %s ==\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }
err()  { printf "  \033[1;31m✗\033[0m %s\n" "$*"; }
die()  { err "$*"; exit 1; }

# ---------- 0. arch + sanity ----------
step "Architecture + macOS"
ARCH=$(uname -m); ok "arch=$ARCH"
sw_vers | sed 's/^/  /'

# ---------- 1. Homebrew ----------
step "Homebrew"
if command -v brew >/dev/null 2>&1; then
    ok "brew at $(command -v brew)"
else
    warn "no brew — installing"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || die "brew install failed"
    # M-series brew lives at /opt/homebrew/bin
    if [ "$ARCH" = "arm64" ] && [ -x /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
fi
ok "$(brew --version | head -1)"

# ---------- 2. Build deps via brew ----------
step "Brew dependencies for op25"
BREW_PKGS=(
  gnuradio
  cmake
  pkg-config
  swig
  cppunit
  libusb
  fftw
  log4cpp
  boost
  uhd
  python@3.12
  git
  pybind11        # gr 3.10 needs this to generate Python bindings for op25 blocks
)
for pkg in "${BREW_PKGS[@]}"; do
    if brew list "$pkg" >/dev/null 2>&1; then
        ok "$pkg already installed"
    else
        warn "installing $pkg"
        brew install "$pkg" || warn "$pkg install failed (may not be critical)"
    fi
done

# ---------- 3. SoapySDRPlay3 (bridges SoapySDR <-> SDRplay API) ----------
step "SoapySDRPlay3 driver (SoapySDR bridge to SDRplay API)"
if SoapySDRUtil --info 2>/dev/null | grep -q sdrplay; then
    ok "SoapySDRPlay3 already registered"
else
    warn "SoapySDRPlay3 not present — installing via brew"
    brew install soapysdr 2>/dev/null || true
    # SoapySDRPlay3 is built from source against the SDRplay API
    if [ ! -d "${HOME}/SoapySDRPlay3" ]; then
        git clone https://github.com/pothosware/SoapySDRPlay3.git "${HOME}/SoapySDRPlay3" || die "SoapySDRPlay3 clone failed"
    fi
    pushd "${HOME}/SoapySDRPlay3" >/dev/null
    mkdir -p build && cd build
    cmake .. && make -j4 && sudo make install || die "SoapySDRPlay3 build failed"
    popd >/dev/null
    ok "SoapySDRPlay3 installed"
fi

# ---------- 4. Verify SDRplay API + RSPduo ----------
step "SDRplay API daemon + RSPduo presence"
if pgrep -af sdrplay_apiService >/dev/null; then
    ok "sdrplay_apiService running"
else
    warn "sdrplay_apiService NOT running — make sure you've installed the SDRplay API .pkg (or SDRconnect, which bundles it). Start it from /Applications/SDRconnect.app once."
fi
if system_profiler SPUSBDataType 2>/dev/null | grep -iqE 'sdrplay|rsp'; then
    ok "RSPduo (or other SDRplay) detected on USB"
    system_profiler SPUSBDataType 2>/dev/null | grep -iE 'sdrplay|rsp' -A 4 | sed 's/^/    /'
else
    err "no SDRplay USB device found — check the cable"
fi
if SoapySDRUtil --find 2>/dev/null | grep -q sdrplay; then
    ok "Soapy enumerates the RSPduo"
    SoapySDRUtil --find 2>/dev/null | sed 's/^/    /'
else
    warn "Soapy did NOT find the SDRplay — daemon may not be running OR SoapySDRPlay3 not loaded"
fi

# ---------- 5. op25 clone ----------
step "op25 source"
if [ -d "$OP25_DIR" ]; then
    ok "$OP25_DIR exists — pulling latest"
    git -C "$OP25_DIR" pull --rebase || warn "op25 pull failed (continuing)"
else
    git clone https://github.com/boatbod/op25.git "$OP25_DIR" || die "op25 clone failed"
fi

# ---------- 6. op25 build ----------
step "op25 build"
pushd "$OP25_DIR" >/dev/null

# Patch op25's CMakeLists.txt for CMake 4 compat:
#   - cmake_minimum_required(VERSION 3.0) -> 3.10 (CMake 4 dropped OLD-policy support for <3.5)
#   - cmake_policy(SET CMP0026 OLD) -> NEW (CMake 4 forbids OLD for pre-3.0 policies)
#   - cmake_policy(SET CMP0045 OLD) -> NEW (same reason)
#
# Idempotent — sed only replaces if the OLD text is still there.
if grep -q "cmake_policy(SET CMP0026 OLD)" CMakeLists.txt 2>/dev/null; then
    warn "patching CMakeLists.txt for CMake 4 compat"
    sed -i.bak 's/cmake_policy(SET CMP0026 OLD)/cmake_policy(SET CMP0026 NEW)/' CMakeLists.txt
    sed -i.bak 's/cmake_policy(SET CMP0045 OLD)/cmake_policy(SET CMP0045 NEW)/' CMakeLists.txt
    sed -i.bak 's/cmake_minimum_required(VERSION 3\.0)/cmake_minimum_required(VERSION 3.10)/' CMakeLists.txt
fi

# Patch op25_audio_wrapper.h for macOS libc++ strict-copy-ctor compat:
# try_emplace triggers std::__tree_node<std::pair<const int, op25_audio>>
# instantiation; libc++ in Tahoe SDK requires op25_audio's copy ctor to be
# const-correct, which it isn't. Switch to piecewise_construct emplace, which
# constructs the value in place without ever requiring a pair copy.
WRAPPER="op25/gr-op25_repeater/lib/op25_audio_wrapper.h"
if [ -f "$WRAPPER" ] && grep -q "try_emplace(msgq_id, options, logger, debug, msgq_id)" "$WRAPPER"; then
    warn "patching $WRAPPER for libc++ compat"
    # Replace the try_emplace one-liner with a piecewise_construct emplace block
    python3 - <<'PYEOF'
import re, sys
path = "op25/gr-op25_repeater/lib/op25_audio_wrapper.h"
with open(path) as f: src = f.read()
old = "auto const [it, created] = audioMap.try_emplace(msgq_id, options, logger, debug, msgq_id);"
new = """auto __r = audioMap.emplace(std::piecewise_construct,
                                std::forward_as_tuple(msgq_id),
                                std::forward_as_tuple(options, logger, debug, msgq_id));
        auto it = __r.first;
        bool created = __r.second;"""
src2 = src.replace(old, new)
if src2 != src:
    with open(path, "w") as f: f.write(src2)
    print("  patched.")
else:
    print("  no match — already patched or different version.")
PYEOF
fi

mkdir -p build && cd build
# Use the brew-installed gnuradio's pkg-config path
export PKG_CONFIG_PATH="$(brew --prefix)/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
export CMAKE_PREFIX_PATH="$(brew --prefix):${CMAKE_PREFIX_PATH:-}"
# Help CMake 4 tolerate any remaining sub-projects declaring cmake_minimum_required < 3.5
export CMAKE_POLICY_VERSION_MINIMUM=3.5
cmake .. -DCMAKE_PYTHON_USE_PYTHON3=ON -DCMAKE_POLICY_VERSION_MINIMUM=3.5 || die "cmake failed"
make -j4 || die "op25 make failed"
sudo make install || warn "op25 install failed (non-fatal if you only need the binaries)"
popd >/dev/null
ok "op25 built"

# ---------- 7. Smoke test ----------
step "op25 smoke test"
RX="$OP25_DIR/op25/gr-op25_repeater/apps/rx.py"
if [ -f "$RX" ]; then
    ok "rx.py found at $RX"
    "$PYTHON_BIN" "$RX" --help 2>&1 | head -10 || warn "rx.py --help failed"
else
    err "rx.py not found at $RX"
fi

# ---------- 8. Summary ----------
step "Summary"
echo "  SDR++          : $([ -d /Applications/SDR++.app ] && echo "installed" || echo "MISSING")"
echo "  SDRplay daemon : $(pgrep -af sdrplay_apiService >/dev/null && echo "running" || echo "NOT running")"
echo "  Soapy + sdrplay: $(SoapySDRUtil --find 2>/dev/null | grep -q sdrplay && echo "yes" || echo "no")"
echo "  GNU Radio      : $(gnuradio-config-info --version 2>/dev/null || echo "missing")"
echo "  op25 source    : $OP25_DIR"
echo "  op25 rx.py     : $([ -f "$RX" ] && echo "$RX" || echo "MISSING")"
echo
echo "Next: launch SDR++ and add the RSPduo source (Module Manager → sdrplay)."
echo "For op25, the canonical invocation is:"
echo "  cd $OP25_DIR/op25/gr-op25_repeater/apps"
echo "  ./rx.py --args 'driver=sdrplay,serial=YOUR_SERIAL' -T trunk.tsv -V -2 -v 1"
