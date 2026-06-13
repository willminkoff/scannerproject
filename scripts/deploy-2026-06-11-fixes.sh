#!/usr/bin/env bash
# Deploy the 2026-06-11 stability fixes.
#
# Engineered after the I-81 field session where:
#   1. airband audio went silent because the LO scheduler parks channels
#      past the cluster cap (DEFAULT_MAX_CLUSTERS=3 vs 6+ needed)
#   2. every scanner-digital-op25 (re)start raced chirp on sdrplay_apiService
#   3. systems.json kept growing back to include a wide-radius VA STARS
#      "Mobile Site - Appomattox Division" with 800 MHz CCs the operator
#      did not want.
#
# Idempotent: re-running is safe. Each step is gated on a checksum.
#
# Usage:
#   sudo bash scripts/deploy-2026-06-11-fixes.sh
#
# Verification (after run):
#   systemctl cat gr-demod@airband.service | grep CHIRP_LO_MAX_CLUSTERS
#   systemctl cat scanner-digital-op25.service | grep -A2 'After='
#   systemctl cat gr-demod@airband.service | grep RestartSec
#   cat /etc/scannerproject/digital/profiles/hp3_favorites_digital/op25_system_config.json | jq .

set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/ubuntu/scannerproject}"
LOG_PREFIX="[deploy-2026-06-11]"

log() { echo "${LOG_PREFIX} $*"; }
die() { echo "${LOG_PREFIX} ERROR: $*" >&2; exit 1; }

if [[ $EUID -ne 0 ]]; then
  die "must run as root (try: sudo $0)"
fi

[[ -f "$PROJECT_ROOT/chirp/systemd/gr-demod@airband.service.d/zz-lo-clusters.conf" ]] \
  || die "expected drop-in source missing at $PROJECT_ROOT/chirp/systemd/gr-demod@airband.service.d/zz-lo-clusters.conf"
[[ -f "$PROJECT_ROOT/systemd/scanner-digital-op25.service.d/10-after-chirp.conf" ]] \
  || die "expected drop-in source missing at $PROJECT_ROOT/systemd/scanner-digital-op25.service.d/10-after-chirp.conf"

# 1. Airband LO cluster cap
log "Step 1/4: install airband CHIRP_LO_MAX_CLUSTERS drop-in"
install -d -m 0755 /etc/systemd/system/gr-demod@airband.service.d
# Filename sorts AFTER the existing cutover.conf so systemd loads ours last
# and our Environment="CHIRP_LO_MAX_CLUSTERS=16" overrides cutover's =8.
# (systemd drop-ins are merged in alphabetic order; later wins on duplicate
# Environment= keys.  See systemd.unit(5) "Drop-in files".)
install -m 0644 \
  "$PROJECT_ROOT/chirp/systemd/gr-demod@airband.service.d/zz-lo-clusters.conf" \
  /etc/systemd/system/gr-demod@airband.service.d/zz-lo-clusters.conf
# Clean up the old name if a previous deploy used 02-...
rm -f /etc/systemd/system/gr-demod@airband.service.d/02-lo-clusters.conf
rm -f /etc/systemd/system/gr-demod@airband.service.d/99-lo-clusters.conf

# 2. OP25 ordering after chirp
log "Step 2/4: install OP25 After=gr-demod drop-in"
install -d -m 0755 /etc/systemd/system/scanner-digital-op25.service.d
install -m 0644 \
  "$PROJECT_ROOT/systemd/scanner-digital-op25.service.d/10-after-chirp.conf" \
  /etc/systemd/system/scanner-digital-op25.service.d/10-after-chirp.conf

# 3. Re-install gr-demod@.service template (carries RestartSec=15)
log "Step 3/4: refresh gr-demod@.service unit template (RestartSec=15)"
install -m 0644 \
  "$PROJECT_ROOT/chirp/systemd/gr-demod@.service.template" \
  /etc/systemd/system/gr-demod@.service

# 4. avoid_site_ids sidecar for the managed digital profile
log "Step 4/4: write avoid_site_ids sidecar (excludes 'Mobile Site - Appomattox Division')"
PROFILE_DIR="/etc/scannerproject/digital/profiles/hp3_favorites_digital"
SIDECAR="$PROFILE_DIR/op25_system_config.json"
HPDB="$PROJECT_ROOT/data/homepatrol.db"

if [[ ! -d "$PROFILE_DIR" ]]; then
  log "warning: profile dir $PROFILE_DIR missing; skipping sidecar write"
else
  if [[ ! -f "$HPDB" ]]; then
    log "warning: $HPDB missing; cannot look up site_ids dynamically — sidecar will use the field name as the policy filter"
    APPOMATTOX_IDS=()
  else
    mapfile -t APPOMATTOX_IDS < <(
      python3 - <<PY
import sqlite3
db = sqlite3.connect("$HPDB")
cur = db.cursor()
cur.execute(
    "SELECT site_id FROM trunk_sites "
    "WHERE trunk_id=3783 "
    "AND (site_name LIKE '%Appomattox%' "
    "     OR site_name LIKE '%Mobile Site%')"
)
for row in cur.fetchall():
    print(str(row[0]))
PY
    )
  fi

  # Build sidecar in Python so we don't mangle JSON merging in bash.
  python3 - "$SIDECAR" "${APPOMATTOX_IDS[@]:-}" <<'PY'
import json, os, sys, tempfile
sidecar_path = sys.argv[1]
avoid_ids = [s for s in sys.argv[2:] if s]
existing = {}
if os.path.isfile(sidecar_path):
    try:
        with open(sidecar_path) as f:
            existing = json.load(f) or {}
        if not isinstance(existing, dict):
            existing = {}
    except Exception:
        existing = {}

va_stars = existing.get("VA STARS") if isinstance(existing.get("VA STARS"), dict) else {}
policy = va_stars.get("site_policy") if isinstance(va_stars.get("site_policy"), dict) else {}
prior = policy.get("avoid_site_ids") if isinstance(policy.get("avoid_site_ids"), list) else []
merged = sorted({str(v) for v in (list(prior) + avoid_ids) if str(v).strip()})

policy["avoid_site_ids"] = merged
policy.setdefault("mode", "auto")
va_stars["site_policy"] = policy
existing["VA STARS"] = va_stars

tmp_fd, tmp_path = tempfile.mkstemp(prefix=".op25_system_config.", suffix=".tmp", dir=os.path.dirname(sidecar_path))
try:
    with os.fdopen(tmp_fd, "w") as f:
        json.dump(existing, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, sidecar_path)
except Exception:
    try: os.unlink(tmp_path)
    except OSError: pass
    raise

print(f"sidecar written: VA STARS avoid_site_ids={merged}")
PY
  # The Python `tempfile.mkstemp` above creates the file with root:root mode 0600
  # because the deploy script runs as root.  airband-ui runs as ubuntu and
  # silently swallows the read EACCES (op25_adapter.py:_read_op25_system_config
  # exception-eats and returns {}), which makes the avoid_site_ids filter a
  # no-op.  Match the perms of every other file in the profile dir.
  chown ubuntu:ubuntu "$SIDECAR"
  chmod 0644 "$SIDECAR"
fi

log "Reloading systemd"
systemctl daemon-reload

log "Done. Recommended next steps:"
log "  1) Verify drop-ins:  systemctl cat gr-demod@airband.service | grep CHIRP_LO_MAX_CLUSTERS"
log "  2) Cold start in order:"
log "       sudo systemctl restart sdrplay"
log "       sudo systemctl restart gr-demod@airband        # MA"
log "       sudo systemctl restart gr-demod@ground         # SL"
log "       sudo systemctl restart scanner-digital-op25    # ST, runs After= chirp"
log "  3) Probe ANALOG.mp3 — expect mean -40 dB voice level when ATC keys"
log "  4) Probe DIGITAL.mp3 once OP25 acquires VA STARS lock"
