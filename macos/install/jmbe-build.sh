#!/bin/bash
# jmbe-build.sh
# Build + install the JMBE voice codec library that SDRTrunk needs for P25/DMR
# voice decode. JMBE is NOT bundled with SDRTrunk (patent reasons) — you compile
# it yourself and point SDRTrunk at the resulting jar.
#
# Refs: https://github.com/DSheirer/jmbe  +  SDRTrunk wiki "JMBE Library".
# Run as your normal user (needs Java 21 + git on PATH).
set -u

step(){ printf "\n\033[1;36m== %s ==\033[0m\n" "$*"; }
ok(){ printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn(){ printf "  \033[1;33m!\033[0m %s\n" "$*"; }
die(){ printf "  \033[1;31m✗\033[0m %s\n" "$*"; exit 1; }

SRC="${HOME}/src/jmbe"
# SDRTrunk looks for the JMBE jar in a user dir you set in its prefs; we stage it here:
OUT_DIR="${HOME}/SDRTrunk/jmbe"

step "Prereqs"
command -v git  >/dev/null || die "git not found"
command -v java >/dev/null || die "java not found (brew install openjdk@21)"
java -version 2>&1 | head -1 | sed 's/^/  /'

step "Clone / update jmbe"
if [ -d "$SRC/.git" ]; then
  git -C "$SRC" pull --ff-only || warn "pull failed; using existing checkout"
else
  git clone https://github.com/DSheirer/jmbe.git "$SRC" || die "clone failed"
fi

step "Build (gradle wrapper)"
cd "$SRC" || die "cd $SRC"
# JMBE ships a gradle wrapper; the 'fatJar'/'jar' task name can vary by version —
# try the common ones, fall back to a plain build.
if ./gradlew tasks --all 2>/dev/null | grep -qi 'fatJar'; then
  ./gradlew fatJar || die "gradlew fatJar failed"
else
  ./gradlew build || die "gradlew build failed"
fi

step "Locate + stage the jar"
JAR=$(find "$SRC" -name 'jmbe-*.jar' -not -name '*sources*' -not -name '*javadoc*' 2>/dev/null | sort | tail -1)
[ -n "$JAR" ] || die "no jmbe-*.jar produced — check the build output / task name"
mkdir -p "$OUT_DIR"
cp "$JAR" "$OUT_DIR/"
ok "staged: $OUT_DIR/$(basename "$JAR")"

step "Done"
echo "  Point SDRTrunk at the JMBE jar:"
echo "    SDRTrunk → View → Preferences → Decoder → JMBE Audio Codec Library"
echo "    set path to: $OUT_DIR/$(basename "$JAR")"
echo "  Then P25/DMR voice will decode. Verify in the Channels view during a call."
