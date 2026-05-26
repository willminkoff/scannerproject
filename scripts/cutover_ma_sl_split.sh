#!/bin/bash
# Cutover script for the MA/SL split-process architecture.
# Run this ONCE on Micro after the repo + worktree is deployed.
#
# What it does (in order):
#   1. Stop all SDR-consuming services and cycle the sdrplay daemon
#   2. Install the new systemd units (rtl-airband-airband.service +
#      rtl-airband-ground.service)
#   3. Install the new build-service-config.py script
#   4. Insert the ANALOG_GROUND.mp3 mount block into icecast.xml
#   5. Mask the legacy rtl-airband.service so it can't fight with
#      the new units
#   6. Migrate profile files from DT mode to MA/SL mode
#   7. Generate per-service runtime configs
#   8. Validate the resulting configs together
#   9. Reload systemd + restart icecast
#   10. Enable + start the new units in dependency order
#   11. Verify via /api/status that both services are healthy
#
# All destructive steps take backups with the .pre-ma-sl-split-20260526
# suffix.  Rollback procedure documented at the bottom.
#
# See docs/rspduo_ma_sl_split.md for the architectural rationale.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/home/ubuntu/scannerproject}"
WORKTREE_ROOT="${WORKTREE_ROOT:-${REPO_ROOT}}"
BACKUP_SUFFIX="${BACKUP_SUFFIX:-.pre-ma-sl-split-20260526}"
ICECAST_XML="${ICECAST_XML:-/etc/icecast2/icecast.xml}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SCRIPTS_DIR="${SCRIPTS_DIR:-${REPO_ROOT}/scripts}"
PROFILES_DIR="${PROFILES_DIR:-${REPO_ROOT}/profiles}"

log()  { printf '\n=== %s ===\n' "$*"; }
warn() { printf '\nWARN: %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

require_root_or_sudo() {
    if [[ "$EUID" -ne 0 ]] && ! command -v sudo >/dev/null; then
        die "Must run as root or have sudo available"
    fi
}

backup_then_install() {
    local src="$1"
    local dst="$2"
    if [[ -f "$dst" ]] && [[ ! -f "${dst}${BACKUP_SUFFIX}" ]]; then
        sudo cp -a "$dst" "${dst}${BACKUP_SUFFIX}"
        printf '  backed up %s -> %s\n' "$dst" "${dst}${BACKUP_SUFFIX}"
    fi
    sudo install -m 644 -o root -g root "$src" "$dst"
    printf '  installed %s\n' "$dst"
}


require_root_or_sudo

log "Step 1: stop all SDR consumers + cycle sdrplay daemon"
sudo systemctl stop rtl-airband.service scanner-digital-op25.service 2>/dev/null || true
sudo systemctl reset-failed rtl-airband.service scanner-digital-op25.service 2>/dev/null || true
sudo pkill -9 -f /usr/local/bin/rtl_airband 2>/dev/null || true
sudo pkill -9 -f multi_rx.py 2>/dev/null || true
sleep 3
sudo systemctl restart sdrplay.service
sleep 4

log "Step 2: install new systemd unit files"
backup_then_install \
    "${WORKTREE_ROOT}/etc/systemd/system/rtl-airband-airband.service" \
    "${SYSTEMD_DIR}/rtl-airband-airband.service"
backup_then_install \
    "${WORKTREE_ROOT}/etc/systemd/system/rtl-airband-ground.service" \
    "${SYSTEMD_DIR}/rtl-airband-ground.service"

log "Step 3: ensure new scripts are present + executable"
# When the worktree IS the deployed repo (the typical case when the
# code arrived via ``git pull`` on Micro), source path == dest path
# and ``install`` refuses to copy a file onto itself.  Just chmod the
# files in place.  When worktree != repo (developer staging from
# elsewhere), copy them.
for s in build-service-config.py migrate_dt_to_ma_sl_profiles.py; do
    src="${WORKTREE_ROOT}/scripts/${s}"
    dst="${SCRIPTS_DIR}/${s}"
    if [[ ! -f "$src" ]]; then
        die "missing source script: $src"
    fi
    if [[ "$(readlink -f "$src")" != "$(readlink -f "$dst")" ]]; then
        sudo install -m 755 -o ubuntu -g ubuntu "$src" "$dst"
        printf '  copied %s -> %s\n' "$src" "$dst"
    else
        sudo chmod 755 "$dst"
        sudo chown ubuntu:ubuntu "$dst"
        printf '  in-place: %s (worktree == repo)\n' "$dst"
    fi
done

log "Step 4: add ANALOG_GROUND.mp3 mount block to icecast.xml"
if grep -q "ANALOG_GROUND.mp3" "$ICECAST_XML"; then
    printf '  /ANALOG_GROUND.mp3 mount already present — skipping\n'
else
    if [[ ! -f "${ICECAST_XML}${BACKUP_SUFFIX}" ]]; then
        sudo cp -a "$ICECAST_XML" "${ICECAST_XML}${BACKUP_SUFFIX}"
        printf '  backed up %s\n' "$ICECAST_XML"
    fi
    # Insert the new <mount> block immediately after the existing
    # /ANALOG.mp3 mount's closing </mount> tag.  Use Python for robust
    # XML-aware editing rather than sed (icecast.xml has nested tags).
    sudo python3 - "$ICECAST_XML" \
                   "${WORKTREE_ROOT}/etc/icecast2/analog-ground-mount-snippet.xml" <<'PYEOF'
import re, sys
xml_path = sys.argv[1]
snippet_path = sys.argv[2]
xml = open(xml_path).read()
snippet_raw = open(snippet_path).read()
# Strip XML comments from the snippet BEFORE searching for the
# mount block — otherwise a comment that mentions ``<mount>`` (a
# previous version did, helpfully) gets matched as if it WERE the
# block to insert.
snippet_no_comments = re.sub(r"<!--.*?-->", "", snippet_raw, flags=re.S)
m = re.search(r"(<mount>\s*<mount-name>.*?</mount>)", snippet_no_comments, re.S)
if not m:
    sys.exit("snippet file missing <mount><mount-name>...</mount> block")
mount_block = m.group(1)
pattern = re.compile(
    r"(<mount>\s*<mount-name>/ANALOG\.mp3</mount-name>.*?</mount>)",
    re.S,
)
m = pattern.search(xml)
if not m:
    sys.exit("could not find /ANALOG.mp3 mount block in icecast.xml")
insertion_point = m.end()
new_xml = (
    xml[:insertion_point]
    + "\n\n  "
    + mount_block.strip()
    + xml[insertion_point:]
)
open(xml_path, "w").write(new_xml)
print(f"  inserted ANALOG_GROUND.mp3 mount block into {xml_path}")
PYEOF
fi

log "Step 5: retire legacy rtl-airband.service"
# Originally tried ``systemctl mask`` here, but on this systemd build
# mask refuses to replace an admin-installed unit file even with
# --force.  Cleanest fix: rename the file out from under systemd so
# the unit name resolves to "not-found" and nothing — neither the
# auto-restart chain nor a manual ``systemctl start rtl-airband`` —
# can bring it up.  Rollback step renames it back into place.
LEGACY_UNIT=/etc/systemd/system/rtl-airband.service
RETIRED_UNIT="${LEGACY_UNIT}.retired-ma-sl-split-20260526"
if [[ -f "$LEGACY_UNIT" && ! -L "$LEGACY_UNIT" ]]; then
    sudo systemctl disable rtl-airband.service 2>/dev/null || true
    sudo mv "$LEGACY_UNIT" "$RETIRED_UNIT"
    printf '  renamed legacy unit -> %s\n' "$RETIRED_UNIT"
    sudo systemctl daemon-reload
elif [[ -L "$LEGACY_UNIT" ]]; then
    printf '  legacy unit already masked (/dev/null symlink) — leaving as-is\n'
else
    printf '  legacy unit not present — nothing to retire\n'
fi

log "Step 6: migrate profile files DT -> MA/SL"
sudo -u ubuntu python3 "${SCRIPTS_DIR}/migrate_dt_to_ma_sl_profiles.py" \
    --profiles-dir "${PROFILES_DIR}" \
    --backup-suffix "${BACKUP_SUFFIX}"

log "Step 7: generate per-service runtime configs"
# The build script reads CONFIG_SYMLINK / GROUND_CONFIG_PATH / etc.
# from the environment — set in /etc/airband-ui.conf which our
# systemd units source via EnvironmentFile=.  Source the file here
# too so cutover-time config generation sees the same paths the
# services will use at runtime.  Use ``set -a; source`` so bash
# parses quoted values (e.g. LIBACARS_BRIDGE_CMD="...spaces...")
# correctly.
AIRBAND_UI_ENV=/etc/airband-ui.conf
# Run as root (not the ubuntu user) because the build script writes
# the runtime config to /run/, which is root-only.  This mirrors how
# the systemd units invoke build-service-config.py at startup —
# they don't drop privileges either.
if [[ -f "$AIRBAND_UI_ENV" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$AIRBAND_UI_ENV"
    set +a
    sudo -E python3 "${SCRIPTS_DIR}/build-service-config.py" --service airband
    sudo -E python3 "${SCRIPTS_DIR}/build-service-config.py" --service ground
else
    warn "no $AIRBAND_UI_ENV — running build-service-config.py with script defaults"
    sudo python3 "${SCRIPTS_DIR}/build-service-config.py" --service airband
    sudo python3 "${SCRIPTS_DIR}/build-service-config.py" --service ground
fi

log "Step 8: validate the new configs together"
sudo -u ubuntu python3 - <<'PYEOF'
import json, sys
sys.path.insert(0, "/home/ubuntu/scannerproject")
from ui.config_validator import validate_dual_service_configs
with open("/run/rtl_airband_airband_runtime.conf") as f: airband_text = f.read()
with open("/run/rtl_airband_ground_runtime.conf") as f: ground_text = f.read()
result = validate_dual_service_configs(airband_text, ground_text)
if not result["ok"]:
    print("VALIDATION FAILED:")
    for issue in result["issues"]:
        print(f"  - {issue['code']}: {issue.get('detail', '')}")
    sys.exit(1)
print("validator: OK — both per-service configs pass and the MA/SL invariants hold")
PYEOF

log "Step 9: reload systemd + restart icecast"
sudo systemctl daemon-reload
sudo systemctl restart icecast2.service
sleep 3

log "Step 10: enable + start the new units in dependency order"
sudo systemctl enable rtl-airband-airband.service rtl-airband-ground.service
sudo systemctl start rtl-airband-airband.service
printf '  rtl-airband-airband.service started (Master, Tuner 1)\n'
printf '  waiting 12s for Master to come up cleanly...\n'
sleep 12
sudo systemctl start rtl-airband-ground.service
printf '  rtl-airband-ground.service started (Slave, Tuner 2)\n'
sleep 8
sudo systemctl start scanner-digital-op25.service
printf '  scanner-digital-op25.service started\n'
sleep 8

log "Step 11: verify via /api/status"
sudo systemctl restart airband-ui.service
sleep 4
python3 - <<'PYEOF'
import json, urllib.request
with urllib.request.urlopen("http://localhost:5050/api/status", timeout=5) as r:
    d = json.load(r)
keys = [
    "rtl_airband_service_active",
    "rtl_airband_service_sample_flow_ok",
    "rtl_airband_service_stats_age_sec",
    "rtl_ground_service_active",
    "rtl_ground_service_sample_flow_ok",
    "rtl_ground_service_stats_age_sec",
    "icecast_mount_analog_alive",
    "icecast_mount_analog_ground_alive",
    "icecast_mount_digital_alive",
]
print()
print("  CUTOVER HEALTH CHECK")
print("  --------------------")
for k in keys:
    v = d.get(k)
    print(f"  {k}: {v}")
print()
all_green = (
    d.get("rtl_airband_service_active")
    and d.get("rtl_ground_service_active")
    and d.get("icecast_mount_analog_alive")
    and d.get("icecast_mount_analog_ground_alive")
    and d.get("icecast_mount_digital_alive")
)
if all_green:
    print("  ✅  ALL GREEN — cutover successful")
else:
    print("  ⚠️   one or more services not healthy yet — give it 30s and re-check.")
    print("      if still not green, see the rollback steps at the bottom of this script.")
PYEOF

cat <<'EOF'


# =============================================================================
# Rollback (if needed)
# =============================================================================
#
# If the cutover doesn't stabilize and you need to revert to the legacy
# single-service DT mode:
#
#     sudo systemctl stop rtl-airband-airband.service rtl-airband-ground.service
#     sudo systemctl disable rtl-airband-airband.service rtl-airband-ground.service
#     sudo systemctl unmask rtl-airband.service
#
#     # Restore migrated profile files
#     for f in /home/ubuntu/scannerproject/profiles/*.pre-ma-sl-split-20260526; do
#         sudo cp "$f" "${f%.pre-ma-sl-split-20260526}"
#     done
#
#     # Restore icecast.xml (removes the ANALOG_GROUND.mp3 mount block)
#     sudo cp /etc/icecast2/icecast.xml.pre-ma-sl-split-20260526 /etc/icecast2/icecast.xml
#     sudo systemctl restart icecast2.service
#
#     # Start legacy single-service rtl-airband
#     sudo systemctl restart rtl-airband.service scanner-digital-op25.service
#
# After rollback, the system is in the same state as before the cutover.
# Investigate the failure, fix in the worktree, and re-run this script.
EOF
