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
#   --gr       5 GNU Radio stack via radioconda (conda-forge) + SoapySDRPlay3 build
#   --op25     6 op25 P25 backend — DEPRECATED in SB7 (needs --force-op25 to run)
#   --wx       7 acarsdec + dumpvdl2 (+libacars)            (source builds)
#   --all      --gr + --wx  (op25 stays opt-in behind --force-op25)
#
# GR stack decision (SB7 §4.3, locked 2026-07-04): gnuradio comes from
# radioconda/conda-forge osx-arm64 (prebuilt 3.10.x, gr-soapy in-tree,
# gnuradio-osmosdr, SoapySDR) — NOT Homebrew; brew's gnuradio formula is
# deprecated (Qt5 EOL). Brew stays for leaf tools only (icecast, ffmpeg,
# lame, libshout, sox, rtl-sdr, cmake, pkg-config, ...).
#
# Idempotent — safe to re-run. Run as your normal user; it sudo's where needed.
# Hardware (RSPduo + RTL-SDR dongles) is NOT required to provision; it is
# required to actually stream. Hardware-only steps are flagged [needs HW].

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="/opt/scannerproject"
ENV_FILE="${PREFIX}/etc/scannerproject.env"
PY=python3.12
# radioconda env (--gr). The chirp daemon runs under THIS python (gnuradio is
# not pip-installable; the /opt venv below is plain CPython for the UI).
RC_PREFIX="${PREFIX}/radioconda"
RC_PY="${RC_PREFIX}/bin/python3"

DO_GR=0; DO_OP25=0; FORCE_OP25=0; DO_WX=0
for arg in "$@"; do
  case "$arg" in
    --gr)         DO_GR=1 ;;
    --op25)       DO_OP25=1 ;;
    --force-op25) DO_OP25=1; FORCE_OP25=1 ;;
    --wx)         DO_WX=1 ;;
    --all)        DO_GR=1; DO_WX=1 ;;
    *) echo "unknown arg: $arg (valid: --gr --op25 --force-op25 --wx --all)"; exit 2 ;;
  esac
done

step() { printf "\n\033[1;36m== %s ==\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[1;31m✗\033[0m %s\n" "$*"; exit 1; }

# ---------- 0. deps ----------------------------------------------------------
step "Architecture + Homebrew"
ARCH=$(uname -m); ok "arch=$ARCH"; sw_vers | sed 's/^/  /'
# Homebrew is OPTIONAL, not required (revised 2026-07-05). The M1 provisioning
# session proved the whole stack — GR, SoapySDRPlay3, icecast, libshout,
# python-shout, ffmpeg, lame, cmake, pkg-config — builds via radioconda/
# conda-forge + source builds with ZERO Homebrew installed. HAVE_BREW gates the
# brew-dependency steps below; the icecast/libshout phase always runs (it now
# builds from source against radioconda when brew is absent).
HAVE_BREW=0
if command -v brew >/dev/null 2>&1; then
  HAVE_BREW=1
  ok "brew: $(brew --version | head -1)"
else
  warn "no Homebrew — proceeding brew-free (radioconda + source builds cover the RF/audio stack)"
fi

if [ "$HAVE_BREW" = "1" ]; then
  step "Homebrew dependencies"
  # Core RF/audio + build toolchain — LEAF TOOLS ONLY (SB7 §4.3). gnuradio does
  # NOT come from brew (deprecated formula); it comes from radioconda in phase 5.
  # The brew gnuradio/boost pile is pulled only for the deprecated forced-op25 path.
  # NOTE: rtl_airband (phase 4) still assumes libconfig/fftw/jansson from here —
  # that combo was NOT validated brew-free this session; if you're brew-free and
  # rtl_airband's build fails on those, that's the known gap.
  BREW_PKGS=(
    icecast ffmpeg sox lame libshout libconfig fftw libusb jansson
    rtl-sdr soapysdr cmake pkg-config git python@3.12
  )
  [ "$FORCE_OP25" = "1" ] && BREW_PKGS+=( gnuradio boost swig cppunit log4cpp uhd pybind11 )
  for pkg in "${BREW_PKGS[@]}"; do
    if brew list "$pkg" >/dev/null 2>&1; then ok "$pkg"; else
      warn "installing $pkg"; brew install "$pkg" || warn "$pkg install failed (may not be critical)"
    fi
  done
fi
# python3.12: brew's python@3.12 if present, else radioconda's (installed in
# phase 5 --gr; if neither exists yet PY resolution below will fail loudly at
# the venv-creation step rather than here, since --gr may not have run yet).
if ! command -v "$PY" >/dev/null 2>&1 && [ -x "$RC_PY" ]; then
  PY="$RC_PY"
  ok "python3.12 resolved via radioconda: $PY"
fi

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

# ---------- 3. icecast + libshout + python-shout -----------------------------
# Validated brew-free on the M1, 2026-07-05: icecast 2.4.4 and libshout 2.4.6
# both build from source against radioconda's ogg/vorbis/xml2/xslt/curl (all
# present once phase 5 --gr has run). python-shout then builds against that
# libshout via pip. If brew's icecast/libshout are already present, skip.
step "Icecast (brew if present, else source build against radioconda)"
if command -v icecast >/dev/null 2>&1; then
  ok "icecast already on PATH ($(command -v icecast))"
elif [ -x "$PREFIX/icecast/bin/icecast" ]; then
  ok "icecast already built at $PREFIX/icecast"
else
  if [ ! -x "$RC_PY" ]; then
    warn "no icecast (brew or built) and radioconda not present yet — run with --gr first, or install icecast via brew"
  else
    IC_VER=2.4.4
    IC_SRC="$HOME/icecast-$IC_VER"
    [ -f "$HOME/icecast-$IC_VER.tar.gz" ] || curl -fL "https://downloads.xiph.org/releases/icecast/icecast-$IC_VER.tar.gz" -o "$HOME/icecast-$IC_VER.tar.gz" || die "icecast download failed"
    [ -d "$IC_SRC" ] || ( cd "$HOME" && tar xzf "icecast-$IC_VER.tar.gz" ) || die "icecast untar failed"
    (
      cd "$IC_SRC"
      export PKG_CONFIG_PATH="$RC_PREFIX/lib/pkgconfig"
      export CPPFLAGS="-I$RC_PREFIX/include"
      export LDFLAGS="-L$RC_PREFIX/lib -Wl,-rpath,$RC_PREFIX/lib"
      ./configure --prefix="$PREFIX/icecast" --with-ogg="$RC_PREFIX" --with-vorbis="$RC_PREFIX" \
        --with-xslt-config="$RC_PREFIX/bin/xslt-config" --with-curl-config="$RC_PREFIX/bin/curl-config" \
        --without-openssl
      # icecast 2.4.4's --without-openssl is buggy: it leaves HAVE_OPENSSL
      # defined in config.h (linker then wants -lssl against a broken "no/lib"
      # path). We don't need TLS for localhost mounts, so disable it directly.
      sed -i '' 's|#define HAVE_OPENSSL 1|/* HAVE_OPENSSL disabled for macOS build — no TLS needed for localhost mounts */|' config.h
      make -j"$(sysctl -n hw.ncpu)" && make install
    ) || die "icecast build failed"
    codesign -f -s - "$PREFIX/icecast/bin/icecast" >/dev/null 2>&1 || true
    ok "icecast built -> $PREFIX/icecast"
  fi
fi

step "libshout + python-shout (needed by chirp's IcecastSink)"
if "$RC_PY" -c "import shout" >/dev/null 2>&1; then
  ok "python-shout already importable under radioconda"
elif [ -x "$RC_PY" ]; then
  LS_VER=2.4.6
  LS_SRC="$HOME/libshout-$LS_VER"
  [ -f "$HOME/libshout-$LS_VER.tar.gz" ] || curl -fL "https://downloads.xiph.org/releases/libshout/libshout-$LS_VER.tar.gz" -o "$HOME/libshout-$LS_VER.tar.gz" || die "libshout download failed"
  [ -d "$LS_SRC" ] || ( cd "$HOME" && tar xzf "libshout-$LS_VER.tar.gz" ) || die "libshout untar failed"
  (
    cd "$LS_SRC"
    export PKG_CONFIG_PATH="$RC_PREFIX/lib/pkgconfig"
    export CPPFLAGS="-I$RC_PREFIX/include"
    export LDFLAGS="-L$RC_PREFIX/lib -Wl,-rpath,$RC_PREFIX/lib"
    ./configure --prefix="$RC_PREFIX" --with-ogg="$RC_PREFIX" --with-vorbis="$RC_PREFIX" --with-theora="$RC_PREFIX"
    make -j"$(sysctl -n hw.ncpu)" && make install
  ) || die "libshout build failed"
  # make install's shout.pc sometimes lands in the source tree instead of
  # $RC_PREFIX/lib/pkgconfig (observed on this build) — copy it explicitly.
  [ -f "$RC_PREFIX/lib/pkgconfig/shout.pc" ] || cp "$LS_SRC/shout.pc" "$RC_PREFIX/lib/pkgconfig/shout.pc" 2>/dev/null || true
  # python-shout's setup.py shells out to a bare `pkg-config` — it must be on
  # PATH (radioconda's pkg-config isn't, by default).
  PATH="$RC_PREFIX/bin:$PATH" PKG_CONFIG_PATH="$RC_PREFIX/lib/pkgconfig" \
    CPPFLAGS="-I$RC_PREFIX/include" LDFLAGS="-L$RC_PREFIX/lib -Wl,-rpath,$RC_PREFIX/lib" \
    "$RC_PREFIX/bin/pip" install --no-cache-dir python-shout || die "python-shout install failed"
  "$RC_PY" -c "import shout" >/dev/null 2>&1 && ok "python-shout installed + importable" \
    || die "python-shout installed but import still fails"
else
  warn "radioconda not present — run with --gr first (chirp's icecast sink needs python-shout under $RC_PY)"
fi

step "Icecast config"
mkdir -p "$PREFIX/etc"
if [ ! -f "$PREFIX/etc/icecast.xml" ]; then
  IC_BASEDIR="$PREFIX/icecast/share/icecast"
  [ -d "$IC_BASEDIR" ] || IC_BASEDIR="$(brew --prefix icecast 2>/dev/null)/share/icecast" || IC_BASEDIR=""
  cat > "$PREFIX/etc/icecast.xml" <<XML
<icecast>
  <limits><clients>50</clients><sources>12</sources><queue-size>524288</queue-size></limits>
  <authentication>
    <source-password>CHANGE_ME_source_password</source-password>
    <admin-user>admin</admin-user>
    <admin-password>CHANGE_ME_admin_password</admin-password>
  </authentication>
  <hostname>localhost</hostname>
  <listen-socket><port>8000</port><bind-address>127.0.0.1</bind-address></listen-socket>
  <paths>
    <basedir>${IC_BASEDIR}</basedir>
    <logdir>${PREFIX}/log</logdir>
    <webroot>${IC_BASEDIR}/web</webroot>
    <adminroot>${IC_BASEDIR}/admin</adminroot>
    <pidfile>${PREFIX}/run/icecast.pid</pidfile>
  </paths>
  <logging><loglevel>3</loglevel><logsize>10000</logsize></logging>
  <fileserve>1</fileserve>
</icecast>
XML
  warn "icecast.xml written with PLACEHOLDER passwords — edit $PREFIX/etc/icecast.xml before going live"
else
  ok "icecast.xml already present at $PREFIX/etc/icecast.xml (not overwritten)"
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

# ---------- 5. GNU Radio stack via radioconda (--gr) -------------------------
# SB7 §4.3 (locked 2026-07-04): gnuradio 3.10.x prebuilt for osx-arm64 from
# conda-forge via the radioconda distribution, installed to $RC_PREFIX. The
# chirp daemon runs under $RC_PY (see mac-spike-chirp-soak.sh). Only
# SoapySDRPlay3 is self-built — against the OFFICIAL SDRplay 3.15 API
# (system .pkg from sdrplay.com; not on conda/brew) — and installed INTO the
# radioconda prefix so conda's SoapySDR (same ABI) picks the module up without
# SOAPY_SDR_PLUGIN_PATH games.
if [ "$DO_GR" = "1" ]; then
  step "GNU Radio stack — radioconda at ${RC_PREFIX} (conda-forge, NOT brew)"

  # The import set chirp needs (chirp/daemon.py + chirp/dsp/source_sdr.py):
  # gnuradio, gr-soapy (gnuradio.soapy), gnuradio-osmosdr (osmosdr), SoapySDR.
  gr_verify() {
    "$RC_PY" - <<'PYEOF'
import sys
checks = (
    ("gnuradio",         "from gnuradio import gr; print('   gnuradio', gr.version())"),
    ("gr-soapy",         "from gnuradio import soapy"),
    ("gnuradio-osmosdr", "import osmosdr"),
    ("soapysdr",         "import SoapySDR; print('   SoapySDR ABI', SoapySDR.getABIVersion())"),
)
failed = []
for label, stmt in checks:
    try:
        exec(stmt)
    except Exception as e:  # noqa: BLE001
        failed.append(f"{label}: {type(e).__name__}: {e}")
for f in failed:
    print("  MISSING " + f)
sys.exit(1 if failed else 0)
PYEOF
  }

  if [ -x "$RC_PY" ] && gr_verify; then
    ok "radioconda present + all GR imports verified ($RC_PY)"
  else
    if [ ! -x "$RC_PY" ]; then
      warn "radioconda not found at $RC_PREFIX — fetching installer"
      case "$ARCH" in
        arm64)  RC_ASSET="MacOSX-arm64.sh" ;;
        x86_64) RC_ASSET="MacOSX-x86_64.sh" ;;   # Intel cold-spare path
        *) die "unsupported arch for radioconda: $ARCH" ;;
      esac
      RC_INSTALLER="$HOME/Downloads/radioconda-$RC_ASSET"
      if [ ! -f "$RC_INSTALLER" ]; then
        RC_URL=""
        for slug in radioconda/radioconda ryanvolz/radioconda; do
          RC_URL=$(curl -fsSL "https://api.github.com/repos/${slug}/releases/latest" 2>/dev/null \
                     | grep -o "\"browser_download_url\": *\"[^\"]*${RC_ASSET}\"" \
                     | head -1 | sed 's/.*"\(https[^"]*\)".*/\1/')
          [ -n "$RC_URL" ] && break
        done
        [ -n "$RC_URL" ] || die "could not resolve the latest radioconda ${RC_ASSET} (offline? API rate-limited?) — download it from https://github.com/radioconda/radioconda/releases to $RC_INSTALLER and re-run --gr"
        warn "downloading $RC_URL"
        curl -fL "$RC_URL" -o "$RC_INSTALLER" || die "radioconda installer download failed"
      fi
      # -b batch (no prompts), -p prefix. Constructor refuses an existing dir,
      # so this only runs when $RC_PREFIX is absent — which is the idempotency
      # story: present+verified re-runs skip this whole branch.
      bash "$RC_INSTALLER" -b -p "$RC_PREFIX" || die "radioconda install failed"
      ok "radioconda installed at $RC_PREFIX"
    fi
    # macOS 26's dyld hard-rejects any Mach-O carrying duplicate LC_RPATH load
    # commands. radioconda's install-time relocation leaves many libs
    # (libgfortran, libquadmath, libopenblas, ...) with @loader_path repeated
    # and/or a @loader_path/ trailing-slash variant, so numpy — and the whole
    # GNU Radio stack — fail to import. Dedupe every dylib/.so's rpaths and
    # re-sign ad-hoc (modifying a dylib invalidates its signature; arm64 needs
    # a valid one to load). Idempotent. DYLD_* env vars do NOT work around this.
    # Discovered provisioning the M1 on 2026-07-05.
    warn "normalising radioconda dylib LC_RPATHs for macOS dyld (dedupe + re-sign)"
    /usr/bin/python3 - "$RC_PREFIX/lib" <<'PYFIX'
import os, subprocess, sys
root = sys.argv[1]
def rpaths(p):
    out = subprocess.run(["otool", "-l", p], capture_output=True, text=True).stdout
    r, L = [], out.splitlines()
    for i, l in enumerate(L):
        if "cmd LC_RPATH" in l:
            for j in range(i, min(i + 4, len(L))):
                s = L[j].strip()
                if s.startswith("path "):
                    r.append(s[5:].rsplit(" (offset", 1)[0]); break
    return r
fixed = 0
for dp, _, fs in os.walk(root):
    for fn in fs:
        if not (fn.endswith(".dylib") or fn.endswith(".so")):
            continue
        p = os.path.join(dp, fn)
        if os.path.islink(p):
            continue
        try:
            rps = rpaths(p)
        except Exception:
            continue
        seen, keep = set(), []
        for r in rps:
            k = r.rstrip("/")
            if k not in seen:
                seen.add(k); keep.append(k)
        if len(keep) == len(rps) and all(r == r.rstrip("/") for r in rps):
            continue
        g = 0
        while g < 60:
            cur = rpaths(p)
            if not cur:
                break
            subprocess.run(["install_name_tool", "-delete_rpath", cur[0], p], capture_output=True)
            g += 1
        for k in keep:
            subprocess.run(["install_name_tool", "-add_rpath", k, p], capture_output=True)
        subprocess.run(["codesign", "-f", "-s", "-", p], capture_output=True)
        fixed += 1
print("  rpath-normalised %d libs" % fixed)
PYFIX
    ok "radioconda dylib rpaths normalised for dyld"
    if ! gr_verify; then
      warn "GR imports incomplete — installing from conda-forge (idempotent)"
      RC_CONDA="$RC_PREFIX/bin/mamba"
      [ -x "$RC_CONDA" ] || RC_CONDA="$RC_PREFIX/bin/conda"
      [ -x "$RC_CONDA" ] || die "no mamba/conda inside $RC_PREFIX — broken radioconda install?"
      "$RC_CONDA" install -y -n base -c conda-forge gnuradio gnuradio-osmosdr soapysdr \
        || die "conda install of the GR stack failed"
      gr_verify || die "GR imports STILL failing after conda install — see MISSING lines above"
    fi
    ok "GR stack verified under $RC_PY"
  fi

  step "SoapySDRPlay3 (RSPduo driver, self-built against the official SDRplay API)"
  SDRPLAY_LIB="/usr/local/lib/libsdrplay_api.dylib"
  SDRPLAY_HDR="/usr/local/include/sdrplay_api.h"
  if [ ! -f "$SDRPLAY_LIB" ] || [ ! -f "$SDRPLAY_HDR" ]; then
    warn "official SDRplay API NOT found ($SDRPLAY_LIB + $SDRPLAY_HDR)"
    warn "SKIPPING SoapySDRPlay3 build. Install the SDRplay API 3.15 .pkg from"
    warn "https://www.sdrplay.com/api/ (SDRconnect bundles it too), then re-run --gr."
  else
    API_VER=$(sed -n 's/.*SDRPLAY_API_VERSION[^0-9]*\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' "$SDRPLAY_HDR" | head -1)
    if [ "$API_VER" = "3.15" ]; then
      ok "SDRplay API $API_VER present"
    else
      warn "SDRplay API header reports '${API_VER:-unknown}' (SB7 §4.3 locked 3.15) — building anyway; re-verify the soak gate after any API change"
    fi
    if ls "$RC_PREFIX"/lib/SoapySDR/modules*/libsdrPlaySupport.so >/dev/null 2>&1; then
      ok "SoapySDRPlay3 module already installed in $RC_PREFIX"
    else
      SP3_DIR="$HOME/SoapySDRPlay3"
      [ -d "$SP3_DIR" ] || git clone https://github.com/pothosware/SoapySDRPlay3.git "$SP3_DIR" || die "SoapySDRPlay3 clone failed"
      # CMAKE_PREFIX_PATH pins the build to radioconda's SoapySDR (NOT brew's —
      # both are installed; the module must match the ABI of the SoapySDR that
      # loads it). Install prefix = $RC_PREFIX, user-owned, so no sudo.
      ( cd "$SP3_DIR" && mkdir -p build && cd build \
          && cmake .. -DCMAKE_PREFIX_PATH="$RC_PREFIX" \
                      -DCMAKE_INSTALL_PREFIX="$RC_PREFIX" \
                      -DCMAKE_BUILD_TYPE=Release \
          && make -j"$(sysctl -n hw.ncpu)" \
          && make install ) || die "SoapySDRPlay3 build/install failed"
      # The built module links @rpath/libsdrplay_api.so.3 and
      # @rpath/libSoapySDR.0.8.dylib but ships with NO usable LC_RPATH, so dyld
      # can't resolve either → the driver loads MISSING. Add rpaths to the
      # SDRplay API (/usr/local/lib) and to radioconda's lib (@loader_path/../..
      # from the module dir), then re-sign ad-hoc. (M1 provisioning 2026-07-05.)
      MOD=$(ls "$RC_PREFIX"/lib/SoapySDR/modules*/libsdrPlaySupport.so 2>/dev/null | head -1)
      if [ -n "$MOD" ]; then
        install_name_tool -add_rpath /usr/local/lib "$MOD" 2>/dev/null || true
        install_name_tool -add_rpath @loader_path/../.. "$MOD" 2>/dev/null || true
        codesign -f -s - "$MOD" >/dev/null 2>&1 || true
      fi
      ok "SoapySDRPlay3 built + installed into $RC_PREFIX (rpaths patched)"
    fi
    # radioconda's SoapySDR-rtlsdr module wants librtlsdr.2.dylib but the
    # rtl-sdr package installs only librtlsdr.2.0.1.dylib — add the symlink.
    if [ -f "$RC_PREFIX/lib/librtlsdr.2.0.1.dylib" ] && [ ! -e "$RC_PREFIX/lib/librtlsdr.2.dylib" ]; then
      ( cd "$RC_PREFIX/lib" && ln -s librtlsdr.2.0.1.dylib librtlsdr.2.dylib )
      ok "linked librtlsdr.2.dylib -> librtlsdr.2.0.1.dylib"
    fi
    # radioconda's SoapySDR has a compiled-in module path that does NOT survive
    # relocation: the driver reads MISSING unless SOAPY_SDR_PLUGIN_PATH points at
    # the modules dir. scannerproject.env exports it for every chirp/GR process.
    SPP="$RC_PREFIX/lib/SoapySDR/modules0.8"
    if SOAPY_SDR_PLUGIN_PATH="$SPP" "$RC_PREFIX/bin/SoapySDRUtil" --check=sdrplay 2>/dev/null | grep -qi "PRESENT"; then
      ok "SoapySDR loads the sdrplay driver (needs SOAPY_SDR_PLUGIN_PATH=$SPP)"
    else
      warn "SoapySDRUtil --check=sdrplay not PRESENT even with SOAPY_SDR_PLUGIN_PATH — check build output"
    fi
  fi
fi

# ---------- 6. op25 (DEPRECATED in SB7 — gated behind --force-op25) ----------
# SB7 §4.1b: op25 has no viable macOS path; the digital engine is SDRTrunk.
# --op25 alone refuses with a pointer; --force-op25 runs the legacy installer.
if [ "$DO_OP25" = "1" ]; then
  step "op25 P25 backend (DEPRECATED)"
  if [ "$FORCE_OP25" != "1" ]; then
    warn "op25 is DEPRECATED on macOS (SB7 §4.1b — digital = SDRTrunk, see mac-install-sdrtrunk.sh)."
    warn "Refusing to install. If you really need the legacy path, re-run with --force-op25."
  elif [ -x "$REPO/scripts/mac-install-op25.sh" ]; then
    warn "op25 FORCED (--force-op25) — legacy path, unsupported going forward"
    "$REPO/scripts/mac-install-op25.sh" || warn "op25 install reported errors — review output"
  else
    warn "scripts/mac-install-op25.sh missing/not executable"
  fi
fi

# ---------- 7. WX decoders (optional) ----------------------------------------
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
echo "  gr stack      : $([ "$DO_GR" = 1 ] && { [ -x "$RC_PY" ] && echo "radioconda at $RC_PREFIX" || echo "FAILED — no $RC_PY"; } || echo "skipped (--gr)")"
echo "  sdrplay soapy : $(ls "$RC_PREFIX"/lib/SoapySDR/modules*/libsdrPlaySupport.so >/dev/null 2>&1 && echo yes || echo "not installed (needs SDRplay 3.15 API + --gr)")"
echo "  op25          : $([ "$FORCE_OP25" = 1 ] && echo "attempted (FORCED — deprecated)" || echo "skipped (deprecated; digital = SDRTrunk)")"
echo "  wx decoders   : $([ "$DO_WX" = 1 ] && echo "attempted" || echo "skipped (--wx)")"
echo
echo "Next:"
echo "  1. [needs HW] plug in the RSPduo + RTL-SDR dongles; confirm: $RC_PREFIX/bin/SoapySDRUtil --find"
echo "  2. Edit $PREFIX/etc/icecast.xml <paths>/<logdir> for macOS, then run icecast -c it."
echo "  3. Appliance conventions (pmset/FileVault/auto-login/Tailscale): scripts/mac-appliance-setup.sh"
echo "  4. Generate the combined airband config into $PREFIX/etc (build-combined-config.py)."
echo "  5. Proof-of-life: source the env, launch icecast + rtl_airband by hand, listen to /ANALOG.mp3."
echo "  6. SB7.1 go/no-go gate: scripts/mac-spike-chirp-soak.sh --dry-run, then the 48 h soak [needs HW]."
echo "  7. Then: the ui/systemd.py service-control backend + launchd plists (see docs/mac-mini-port.md)."
