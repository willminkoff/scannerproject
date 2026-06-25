#!/bin/bash
# mac-install-sdrtrunk.sh
# Install SDRTrunk on macOS Apple Silicon (works on Intel too).
# SDRTrunk is the modern open-source P25/DMR/NXDN trunking decoder —
# replaces op25 on Mac. Native macOS support, no build chain hell.
#
# Prereqs (already in place from prior steps):
#   - SDRplay API 3.15.1 installed (you've got this)
#   - SDRplay daemon running (start SDRconnect once OR run sdrplay_apiService)
#   - RSPduo plugged in
#
# Run as your normal user — the script will sudo where needed.

set -u  # error on unset vars; don't set -e — we want graceful continue past benign checks

INSTALL_DIR="${HOME}/SDRTrunk"

step() { printf "\n\033[1;36m== %s ==\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[1;31m✗\033[0m %s\n" "$*"; exit 1; }

# ---------- 0. Architecture + macOS ----------
step "Architecture + macOS"
ARCH=$(uname -m); ok "arch=$ARCH"
sw_vers | sed 's/^/  /'

# ---------- 1. Homebrew ----------
step "Homebrew"
if command -v brew >/dev/null 2>&1; then
    ok "brew at $(command -v brew)"
else
    die "no brew — install Homebrew first (https://brew.sh)"
fi

# ---------- 2. Java 21+ ----------
step "Java 21+"
JAVA_VERSION=""
if command -v java >/dev/null 2>&1; then
    JAVA_VERSION=$(java -version 2>&1 | head -1)
    ok "java: $JAVA_VERSION"
fi
NEED_JAVA=0
if [ -z "$JAVA_VERSION" ]; then
    NEED_JAVA=1
elif ! echo "$JAVA_VERSION" | grep -qE '"(21|22|23|24|25|26)\.'; then
    warn "java present but < 21 — installing openjdk@21"
    NEED_JAVA=1
fi
if [ "$NEED_JAVA" = "1" ]; then
    brew install openjdk@21 || die "brew install openjdk@21 failed"
    JAVA_PREFIX=$(brew --prefix openjdk@21)
    sudo ln -sfn "$JAVA_PREFIX/libexec/openjdk.jdk" /Library/Java/JavaVirtualMachines/openjdk-21.jdk 2>/dev/null || \
        warn "symlink to /Library/Java skipped (java might not be on PATH for the gui launcher)"
    export PATH="$JAVA_PREFIX/bin:$PATH"
    ok "java now: $(java -version 2>&1 | head -1)"
fi

# ---------- 3. Discover latest SDRTrunk release ----------
step "Discover latest SDRTrunk release on GitHub"
LATEST_JSON=$(curl -fsSL https://api.github.com/repos/DSheirer/sdrtrunk/releases/latest 2>/dev/null)
[ -n "$LATEST_JSON" ] || die "github api fetch failed (rate limit? network?)"

# Match the macOS asset. SDRTrunk's asset names look like:
#   sdr-trunk-osx-aarch64-X.Y.Z.zip   (Apple Silicon)
#   sdr-trunk-osx-x86_64-X.Y.Z.zip    (Intel)
if [ "$ARCH" = "arm64" ]; then
    PATTERN='aarch64'
else
    PATTERN='x86_64'
fi

URL=$(echo "$LATEST_JSON" \
    | grep -oE '"browser_download_url":\s*"[^"]+\.zip"' \
    | cut -d'"' -f4 \
    | grep -i 'osx\|macos\|mac' \
    | grep -i "$PATTERN" \
    | head -1)

# Fallback: any mac zip if arch-specific not found
if [ -z "$URL" ]; then
    URL=$(echo "$LATEST_JSON" \
        | grep -oE '"browser_download_url":\s*"[^"]+\.zip"' \
        | cut -d'"' -f4 \
        | grep -iE 'osx|macos|mac' \
        | head -1)
    [ -n "$URL" ] && warn "no $ARCH-specific build; using generic mac zip"
fi

VERSION=$(echo "$LATEST_JSON" | grep -oE '"tag_name":\s*"[^"]+"' | head -1 | cut -d'"' -f4)
[ -n "$URL" ] || die "couldn't find a macOS download URL — check https://github.com/DSheirer/sdrtrunk/releases manually"
ok "version: $VERSION"
ok "url:     $URL"

# ---------- 4. Download ----------
step "Download SDRTrunk"
DEST="${HOME}/Downloads/$(basename "$URL")"
if [ -f "$DEST" ] && [ -s "$DEST" ]; then
    ok "already downloaded: $DEST ($(du -h "$DEST" | cut -f1))"
else
    curl -fL -o "$DEST" "$URL" || die "download failed"
    ok "saved: $DEST ($(du -h "$DEST" | cut -f1))"
fi

# ---------- 5. Unzip ----------
step "Unzip to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
# Wipe a previous install so we don't end up with a mix of versions
rm -rf "$INSTALL_DIR"/*
unzip -q -o "$DEST" -d "$INSTALL_DIR"
# Flatten one level if the zip wraps everything in a single top-level dir
if [ "$(ls "$INSTALL_DIR" | wc -l | tr -d ' ')" = "1" ]; then
    TOP=$(ls "$INSTALL_DIR")
    if [ -d "$INSTALL_DIR/$TOP" ]; then
        ok "flattening top-level dir: $TOP"
        mv "$INSTALL_DIR/$TOP"/* "$INSTALL_DIR/" 2>/dev/null
        mv "$INSTALL_DIR/$TOP"/.* "$INSTALL_DIR/" 2>/dev/null || true
        rmdir "$INSTALL_DIR/$TOP" 2>/dev/null
    fi
fi
ls "$INSTALL_DIR" | sed 's/^/    /'

# ---------- 6. Strip quarantine, fix perms ----------
step "Strip macOS quarantine + ensure exec perms"
xattr -dr com.apple.quarantine "$INSTALL_DIR" 2>/dev/null && ok "quarantine attrs removed"
LAUNCHER=$(find "$INSTALL_DIR" -name 'sdr-trunk' -type f | head -1)
if [ -z "$LAUNCHER" ]; then
    LAUNCHER=$(find "$INSTALL_DIR/bin" -type f -name 'sdr*' 2>/dev/null | head -1)
fi
if [ -n "$LAUNCHER" ]; then
    chmod +x "$LAUNCHER"
    ok "launcher: $LAUNCHER"
else
    warn "no 'sdr-trunk' launcher found — check $INSTALL_DIR/bin manually"
fi

# ---------- 7. Smoke test ----------
step "Smoke test"
if [ -n "$LAUNCHER" ]; then
    # Most SDRTrunk launchers fork the JVM and exit — start it briefly then kill
    timeout 4 "$LAUNCHER" --help 2>&1 | head -10 || warn "--help failed (launcher may not support it)"
fi

# ---------- 8. Summary + next steps ----------
step "Done"
echo "  SDRTrunk installed at: $INSTALL_DIR"
echo "  Launcher:              ${LAUNCHER:-(not found)}"
echo
echo "Next:"
echo "  1. Launch:    $LAUNCHER"
echo "     (or drag $INSTALL_DIR/sdr-trunk.app — if present — to /Applications)"
echo "  2. In the GUI: View → Tuner → add 'SDRplay RSPduo'. Should auto-detect via the"
echo "     installed SDRplay API 3.15.1."
echo "  3. Set master/slave or dual-tuner mode per tuner."
echo "  4. Playlist Editor → add a system. For TN: look up TACN (Tennessee Advanced"
echo "     Communications Network) or local agencies on radioreference.com."
echo "  5. Channels view: live decoded calls + tagged talkgroups."
echo "  6. For Tailscale audio streaming: Playlist → Broadcaster → Icecast HTTP."
echo "     You'll need icecast running somewhere (Mac local: \`brew install icecast\`)."
