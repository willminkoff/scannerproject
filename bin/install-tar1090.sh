#!/usr/bin/env bash
# Install tar1090's web UI (the map adsb.lol runs) for SB3's ADS-B panel.
#
# SB3 serves the static files at /adsb/ and feeds them adsb.lol's live
# aircraft through /adsb/data/*.json (sb3/ui/routes.py), so no decoder or
# web server from tar1090's own installer is involved -- only html/ plus the
# aircraft database that its icons and type/operator lookups read.
#
# Usage: bin/install-tar1090.sh [dest]   (default ~/vendor/tar1090)
set -euo pipefail

DEST="${1:-$HOME/vendor/tar1090}"
TAR1090_REPO="https://github.com/wiedehopf/tar1090.git"
DB_REPO="https://github.com/wiedehopf/tar1090-db.git"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

git clone -q --depth 1 "$TAR1090_REPO" "$work/tar1090"
git clone -q --depth 1 "$DB_REPO" "$work/tar1090-db"

stage="$work/html"
cp -R "$work/tar1090/html" "$stage"
# index.html reads the database from "db2" (databaseFolder).
cp -R "$work/tar1090-db/db" "$stage/db2"
{
    echo "tar1090 $(git -C "$work/tar1090" rev-parse --short HEAD) $(cat "$work/tar1090/version")"
    echo "tar1090-db $(git -C "$work/tar1090-db" rev-parse --short HEAD) $(cat "$work/tar1090-db/version")"
} > "$stage/SB3_VERSIONS"

mkdir -p "$DEST"
rm -rf "$DEST/html.old"
[[ -d "$DEST/html" ]] && mv "$DEST/html" "$DEST/html.old"
mv "$stage" "$DEST/html"
rm -rf "$DEST/html.old"
cat "$DEST/html/SB3_VERSIONS"
