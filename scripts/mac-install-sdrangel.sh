#!/bin/bash
# mac-install-sdrangel.sh
# Install SDRangel on macOS Apple Silicon.
# SDRangel = comprehensive open-source SDR with deep scanner/multi-channel support.
# Native macOS builds available from f4exb/sdrangel releases.
#
# Prereqs already in place:
#   - SDRplay API 3.15.1 universal binary at /Library/SDRplayAPI/3.15.1/
#   - RSPduo plugged in
#   - Homebrew installed

set -u
INSTALL_DIR="/Applications/SDRangel.app"

step() { printf "\n\033[1;36m== %s ==\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[1;31m✗\033[0m %s\n" "$*"; exit 1; }

step "Architecture + macOS"
ARCH=$(uname -m); ok "arch=$ARCH"
sw_vers | sed 's/^/  /'

step "Homebrew"
command -v brew >/dev/null 2>&1 || die "no brew — install Homebrew first"
ok "brew: $(brew --version | head -1)"

# ---------- 1. Find newest release that ships our arch's dmg ----------
# NOTE: f4exb/sdrangel does NOT publish a Homebrew cask, and the *latest*
# release often has no macOS asset at all (CI only builds mac dmgs on some
# tags). So we scan the release list newest-first for the first arm64/x86_64
# .dmg we can find instead of trusting releases/latest.
step "Find newest SDRangel release with a macOS $ARCH .dmg"
if [ "$ARCH" = "arm64" ]; then
    PATTERN='arm64|aarch64'
else
    PATTERN='x86_64|intel'
fi

RELEASES=$(curl -fsSL "https://api.github.com/repos/f4exb/sdrangel/releases?per_page=40")
[ -n "$RELEASES" ] || die "github api fetch failed"

URL=$(echo "$RELEASES" \
    | grep -oE '"browser_download_url":[[:space:]]*"[^"]+\.dmg"' \
    | cut -d'"' -f4 \
    | grep -iE "mac|osx|darwin" \
    | grep -iE "$PATTERN" \
    | head -1)

[ -n "$URL" ] || die "no SDRangel macOS $ARCH .dmg in last 40 releases — check https://github.com/f4exb/sdrangel/releases"
ok "url: $URL"

DMG="${HOME}/Downloads/$(basename "$URL")"
if [ -f "$DMG" ] && [ -s "$DMG" ]; then
    ok "already downloaded: $DMG"
else
    curl -fL -o "$DMG" "$URL" || die "download failed"
    ok "saved: $DMG ($(du -h "$DMG" | cut -f1))"
fi

step "Mount .dmg and copy to /Applications"
# Don't use -quiet here: it suppresses the device/mountpoint table we need to
# parse. Output is tab-separated; the mount point is the last field of the
# /Volumes line. `yes |` auto-accepts any (unlikely) software-license prompt.
MOUNT_POINT=$(yes | hdiutil attach "$DMG" -nobrowse 2>/dev/null \
    | awk -F'\t' '/\/Volumes\// {print $NF}' | tail -1)
[ -n "$MOUNT_POINT" ] || die "dmg mount failed"
ok "mounted at: $MOUNT_POINT"

SRC_APP=$(find "$MOUNT_POINT" -maxdepth 2 -name "SDRangel.app" -type d 2>/dev/null | head -1)
if [ -z "$SRC_APP" ]; then
    warn "SDRangel.app not at expected path — listing dmg contents:"
    ls -la "$MOUNT_POINT" | sed 's/^/    /'
    hdiutil detach "$MOUNT_POINT" -quiet
    die "couldn't find SDRangel.app in dmg"
fi

# /Applications is group-writable by 'admin' on macOS, so no sudo needed for an
# admin user. Fall back to sudo only if a plain copy is denied.
if [ -d "$INSTALL_DIR" ]; then
    warn "removing existing $INSTALL_DIR"
    rm -rf "$INSTALL_DIR" 2>/dev/null || sudo rm -rf "$INSTALL_DIR"
fi
cp -R "$SRC_APP" "$INSTALL_DIR" 2>/dev/null || sudo cp -R "$SRC_APP" "$INSTALL_DIR" || die "copy failed"
ok "copied to $INSTALL_DIR"

hdiutil detach "$MOUNT_POINT" -quiet

# ---------- 3. Strip quarantine ----------
step "Strip macOS quarantine"
xattr -dr com.apple.quarantine "$INSTALL_DIR" 2>/dev/null && ok "quarantine attrs removed"

# ---------- 4. SDRplay lib linkage check ----------
step "SDRplay API lib visibility"
echo "  SDRplay API at /usr/local/lib/libsdrplay_api.so.3.15:"
file /usr/local/lib/libsdrplay_api.so.3.15 2>/dev/null | sed 's/^/    /'
echo "  Universal binary check:"
lipo -info /usr/local/lib/libsdrplay_api.so.3.15 2>/dev/null | sed 's/^/    /' || warn "lipo failed"

# The SDRplay plugin (Contents/Resources/lib/plugins/libinputsdrplayv3.dylib)
# links directly against /usr/local/lib/libsdrplay_api.so.3 (verified via otool
# -L), which already exists as a symlink to the universal 3.15.1 API. The
# Frameworks symlink below is harmless belt-and-suspenders, not the real load
# path — the plugin loads the API straight from /usr/local/lib.
SDRA_FRAMEWORKS="$INSTALL_DIR/Contents/Frameworks"
if [ -d "$SDRA_FRAMEWORKS" ]; then
    if [ ! -e "$SDRA_FRAMEWORKS/libsdrplay_api.so.3" ]; then
        ln -sf /usr/local/lib/libsdrplay_api.so.3 "$SDRA_FRAMEWORKS/libsdrplay_api.so.3" 2>/dev/null \
            || sudo ln -sf /usr/local/lib/libsdrplay_api.so.3 "$SDRA_FRAMEWORKS/libsdrplay_api.so.3"
        ok "symlinked libsdrplay_api.so.3 into SDRangel Frameworks"
    fi
fi

step "Done"
echo "  SDRangel installed at: $INSTALL_DIR"
echo "  Launch: open $INSTALL_DIR  (or click in /Applications)"
echo
echo "Next steps in the GUI:"
echo "  1. Preferences → Devices → SDRplay → ensure RSP-Duo detected"
echo "  2. Add Device → SDRplay RSPduo (pick Single/Dual/Master per use case)"
echo "  3. Add Channel → AM Demod (or NFM Demod) → tune frequency"
echo "  4. For scanning: Channel → Scanner plugin (gives bank-based scan)"
echo "  5. For multi-channel parallel listening: add multiple demod channels on the same device"
