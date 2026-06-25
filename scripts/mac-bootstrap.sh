#!/bin/bash
# mac-bootstrap.sh — provision a Mac mini (Apple Silicon) to run scannerproject
# as a full replacement for the Linux "micro" box.
#
# This is the consolidated installer. The earlier mac-install-*.sh scripts
# install standalone DESKTOP GUI apps (SDRTrunk/SDRangel) for interactive
# listening; THIS script provisions the headless scannerproject SERVER stack:
# native RF decoders + Icecast + the Python UI, under the /opt/scannerproject
# runtime layout that etc/mac/scannerproject.env points at.
#
# Phases:
#   (always)   0 deps · 1 runtime layout · 2 py3.12 venv · 3 icecast · 4 rtl_airband
#   --op25     5 op25 P25 backend (delegates to mac-install-op25.sh; ~long build)
#   --wx       6 acarsdec + dumpvdl2 (+libacars)            (source builds)
#   --all      everything above
#
# Idempotent — safe to re-run. Run as your normal user; it sudo's where needed.
# Hardware (RSPduo + RTL-SDR dongles) is NOT required to provision; it is
# required to actually stream. Hardware-only steps are flagged [needs HW].

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="/opt/scannerproject"
ENV_FILE="${PREFIX}/etc/scannerproject.env"
PY=python3.12

DO_OP25=0; DO_WX=0
for arg in "$@"; do
  case "$arg" in
    --op25) DO_OP25=1 ;;
    --wx)   DO_WX=1 ;;
    --all)  DO_OP25=1; DO_WX=1 ;;
    *) echo "unknown arg: $arg (valid: --op25 --wx --all)"; exit 2 ;;
  esac
done

step() { printf "\n\033[1;36m== %s ==\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[1;31m✗\033[0m %s\n" "$*"; exit 1; }

# ---------- 0. deps ----------------------------------------------------------
step "Architecture + Homebrew"
ARCH=$(uname -m); ok "arch=$ARCH"; sw_vers | sed 's/^/  /'
command -v brew >/dev/null 2>&1 || die "no Homebrew — install from https://brew.sh first"
ok "brew: $(brew --version | head -1)"

step "Homebrew dependencies"
# Core RF/audio + build toolchain. gnuradio/boost pulled only when op25 is built.
BREW_PKGS=(
  icecast ffmpeg sox lame libshout libconfig fftw libusb jansson
  rtl-sdr soapysdr cmake pkg-config git python@3.12
)
[ "$DO_OP25" = "1" ] && BREW_PKGS+=( gnuradio boost swig cppunit log4cpp uhd pybind11 )
for pkg in "${BREW_PKGS[@]}"; do
  if brew list "$pkg" >/dev/null 2>&1; then ok "$pkg"; else
    warn "installing $pkg"; brew install "$pkg" || warn "$pkg install failed (may not be critical)"
  fi
done
command -v "$PY" >/dev/null 2>&1 || die "python3.12 not on PATH after brew install"

# ---------- 1. runtime layout ------------------------------------------------
step "Runtime layout under ${PREFIX}"
if [ ! -d "$PREFIX" ]; then
  warn "creating ${PREFIX} (sudo)"; sudo mkdir -p "$PREFIX" || die "mkdir ${PREFIX} failed"
  sudo chown -R "$(id -un):staff" "$PREFIX" || die "chown ${PREFIX} failed"
fi
mkdir -p \
  "$PREFIX/etc/airband-profiles" "$PREFIX/etc/digital/profiles" "$PREFIX/etc/v3" \
  "$PREFIX/run/op25" "$PREFIX/log" "$REPO/admin/logs"
ok "dirs ready"
# Drop the env file into place (copy from repo so the canonical source is tracked)
cp "$REPO/etc/mac/scannerproject.env" "$ENV_FILE"
ok "env file -> $ENV_FILE"

# ---------- 2. python 3.12 venv (system python is 3.14 = no audioop) ----------
step "Python 3.12 venv at ${PREFIX}/venv"
if [ ! -d "$PREFIX/venv" ]; then "$PY" -m venv "$PREFIX/venv" || die "venv create failed"; fi
# shellcheck disable=SC1091
source "$PREFIX/venv/bin/activate"
python -m pip install --upgrade pip >/dev/null 2>&1 || warn "pip upgrade failed"
# The UI is stdlib http.server; chirp has its own reqs. Install what exists.
[ -f "$REPO/chirp/requirements.txt" ] && pip install -r "$REPO/chirp/requirements.txt" || warn "no chirp reqs / install failed"
pip install requests numpy >/dev/null 2>&1 || warn "base pip deps failed"
ok "venv python: $(python --version 2>&1)  ($(command -v python))"
deactivate

# ---------- 3. icecast --------------------------------------------------------
step "Icecast config"
ICE_SRC="$REPO/etc/icecast2/icecast.xml"
if [ -f "$ICE_SRC" ]; then
  cp "$ICE_SRC" "$PREFIX/etc/icecast.xml"
  ok "icecast.xml -> $PREFIX/etc/icecast.xml (review <paths>/<logdir> for macOS)"
else
  warn "no repo icecast.xml at $ICE_SRC — generate one or copy brew's sample"
fi

# ---------- 4. rtl_airband (build native) ------------------------------------
step "RTLSDR-Airband (build from source, -DPLATFORM=native)"
RA_DIR="$HOME/RTLSDR-Airband"
if command -v rtl_airband >/dev/null 2>&1; then
  ok "rtl_airband already on PATH ($(command -v rtl_airband))"
else
  [ -d "$RA_DIR" ] || git clone https://github.com/charlie-foxtrot/RTLSDR-Airband.git "$RA_DIR" || die "clone failed"
  ( cd "$RA_DIR" && mkdir -p build && cd build \
      && cmake .. -DPLATFORM=native -DCMAKE_BUILD_TYPE=Release \
      && make -j"$(sysctl -n hw.ncpu)" \
      && sudo make install ) || die "rtl_airband build failed"
  ok "rtl_airband built: $(command -v rtl_airband || echo "check $RA_DIR/build/src")"
fi

# ---------- 5. op25 (optional) -----------------------------------------------
if [ "$DO_OP25" = "1" ]; then
  step "op25 P25 backend (delegating to mac-install-op25.sh)"
  if [ -x "$REPO/scripts/mac-install-op25.sh" ]; then
    "$REPO/scripts/mac-install-op25.sh" || warn "op25 install reported errors — review output"
  else
    warn "scripts/mac-install-op25.sh missing/not executable"
  fi
fi

# ---------- 6. WX decoders (optional) ----------------------------------------
if [ "$DO_WX" = "1" ]; then
  step "libacars + acarsdec + dumpvdl2 (source builds)"
  build_cmake() { # $1=name $2=git
    local name="$1" repo="$2" dir="$HOME/$1"
    command -v "$name" >/dev/null 2>&1 && { ok "$name already installed"; return; }
    [ -d "$dir" ] || git clone "$repo" "$dir" || { warn "$name clone failed"; return; }
    ( cd "$dir" && mkdir -p build && cd build && cmake .. && make -j4 && sudo make install ) \
      || warn "$name build failed"
  }
  build_cmake libacars https://github.com/szpajder/libacars.git   # dep for both
  build_cmake acarsdec  https://github.com/TLeconte/acarsdec.git
  build_cmake dumpvdl2  https://github.com/szpajder/dumpvdl2.git
  warn "radiosonde_auto_rx not auto-built — see projecthorus/radiosonde_auto_rx (SoapySDR backend)"
fi

# ---------- summary ----------------------------------------------------------
step "Summary"
echo "  prefix        : $PREFIX"
echo "  env file      : $ENV_FILE"
echo "  venv python   : $PREFIX/venv/bin/python"
echo "  icecast       : $(command -v icecast >/dev/null && echo yes || echo MISSING)"
echo "  rtl_airband   : $(command -v rtl_airband >/dev/null && echo yes || echo 'check build dir')"
echo "  op25          : $([ "$DO_OP25" = 1 ] && echo "attempted" || echo "skipped (--op25)")"
echo "  wx decoders   : $([ "$DO_WX" = 1 ] && echo "attempted" || echo "skipped (--wx)")"
echo
echo "Next:"
echo "  1. [needs HW] plug in the RSPduo + RTL-SDR dongles; confirm: SoapySDRUtil --find"
echo "  2. Edit $PREFIX/etc/icecast.xml <paths>/<logdir> for macOS, then run icecast -c it."
echo "  3. Generate the combined airband config into $PREFIX/etc (build-combined-config.py)."
echo "  4. Proof-of-life: source the env, launch icecast + rtl_airband by hand, listen to /ANALOG.mp3."
echo "  5. Then: the ui/systemd.py service-control backend + launchd plists (see docs/mac-mini-port.md)."
