# Deployment Commands - Quick Reference

## SB3 (Micro) — Primary Deploy Target

- Host: `micro` (Dell OptiPlex Micro 7020, via Tailscale)
- Tailscale IP: `100.67.20.40`
- SSH: `ubuntu@100.67.20.40` (Tailscale SSH; previously documented as `root@micro`)
- User/path: `/home/ubuntu/scannerproject`
- Service: `airband-ui`
- Micro has a working git checkout — both `git pull` and rsync are valid deploy paths.
  rsync is recommended for in-flight hot-fixes (don't pollute history). `git pull`
  is the canonical path for landing already-committed changes.

### Deploy to Micro (One-Command)
```bash
rsync -avz -e ssh ui/ root@micro:/home/ubuntu/scannerproject/ui/ --exclude='__pycache__' --exclude='*.pyc' && ssh root@micro "systemctl restart airband-ui && sleep 3 && curl -s http://localhost:5050/api/status | python3 -m json.tool | head -15"
```

### Deploy + Verify (Safer)
```bash
# 1. Sync files
rsync -avz -e ssh ui/ root@micro:/home/ubuntu/scannerproject/ui/ --exclude='__pycache__' --exclude='*.pyc'

# 2. Verify imports
ssh root@micro "cd /home/ubuntu/scannerproject && python3 -c 'from ui.app import main' && echo 'imports ok' || echo 'import error'"

# 3. Restart
ssh root@micro "systemctl restart airband-ui && sleep 3"

# 4. Verify
ssh root@micro "systemctl is-active airband-ui && curl -s http://localhost:5050/api/status | python3 -m json.tool | head -15"
```

### Deploy Scripts Only
```bash
rsync -avz -e ssh scripts/ root@micro:/home/ubuntu/scannerproject/scripts/ --exclude='__pycache__' --exclude='*.pyc'
```

### Pulling commits that untrack runtime files (GOTCHA)

When origin lands a commit that runs `git rm --cached` on a file that Micro
*is currently writing to at runtime* (e.g. `profiles/rtl_airband_*.conf`,
`profiles/managed_analog_controls.json`, `disco/configs/sweep.yaml`), a
naive `git pull` on Micro will either:

1. **Abort** with "Your local changes would be overwritten by merge" if the
   working tree is dirty (very common — the UI rewrites these files on
   every slider tap), or
2. **Silently delete the working-tree file** if you first ran
   `git checkout HEAD -- <file>` to discard the dirt. Pull then sees an
   unmodified tracked-file being untracked and removes it from disk.

Hit case (2) on 2026-05-24 — the cleanup deleted all 27 runtime profile
configs on Micro before the airband-ui could rewrite them. rtl-airband
kept running with cached in-memory state but a service restart would have
failed. Recovery: `git checkout <previous-commit> -- <files>` then
`git reset HEAD -- <files>` to put them back on disk as Untracked.

**Correct sequence** for pulling an untrack-commit cleanly onto Micro:

```bash
ssh ubuntu@100.67.20.40 'cd /home/ubuntu/scannerproject && \
    sudo git rm --cached <same files the commit untracks> 2>/dev/null; \
    sudo git pull --ff-only origin main'
```

The local `git rm --cached` matches the commit's index change, so the pull
is a no-op for those files — working-tree contents stay intact. After the
pull, `git status` will show them as Untracked + ignored, which is the
intended steady state.

**Alternative** (when you forget): `git stash --include-untracked` *before*
pulling preserves the dirt; you can drop the stash afterward since the
files are now gitignored and the runtime will rewrite them anyway.

---

## OP25 multi_rx.py patch (in-place on Micro — NOT in repo)

The OP25 install at `/opt/op25/` is upstream (boatbod/osmocom fork) and is
not tracked in this repo. We carry a single targeted patch in-place.

### Path on Micro
`/opt/op25/op25/gr-op25_repeater/apps/multi_rx.py`

### Why
Every restart_digital() cascade was observed to segfault the `sdrplay_apiServ`
daemon (12/12 on 2026-05-13). Root cause: op25's SIGINT handler calls
`tb.stop()` + `tb.kill()` but never explicitly drops device.src references,
so `~source_impl` (the C++ gr-osmosdr destructor that calls `sdrplay_api_Close`)
only fires via Python GC. With KillSignal=SIGINT + TimeoutStopSec=10s
(now 30s), GC timing was unreliable; handles were abandoned mid-grpc-call,
and the sdrplay daemon segfaulted on its next stop.

### Patch shape
In `rx_main.run()`'s `except (KeyboardInterrupt):` block, drop device.src
refs BEFORE `tb.stop()` so the graph's stop releases the last hold on each
source — destructor runs promptly, sdrplay_api_Close called cleanly.

### Snapshot (pre-patch)
`/opt/op25/op25/gr-op25_repeater/apps/multi_rx.py.pre-sdrplay-close-20260513-152413`

### Diff (effective lines)
```python
# Before:
        except (KeyboardInterrupt):
            self.tb.stop()
            self.tb.kill()
            self.keep_running = False
            sys.stderr.write("Ctrl-C detected\n")

# After:
        except (KeyboardInterrupt):
            # Drop device.src refs before tb.stop() so the graph releases
            # the last hold on the gr-osmosdr source -> ~source_impl runs
            # promptly -> sdrplay_api_Close called -> sdrplay daemon does
            # not segfault on its next restart with abandoned grpc clients.
            try:
                for _dev in getattr(self.tb, "devices", []) or []:
                    if getattr(_dev, "src", None) is not None:
                        _dev.src = None
            except Exception as _e:
                sys.stderr.write("explicit src teardown failed: %s\n" % _e)
            self.tb.stop()
            self.tb.kill()
            self.keep_running = False
            sys.stderr.write("Ctrl-C detected\n")
```

### Re-applying after an op25 reinstall
If `/opt/op25/` is rebuilt (e.g. `make install` from upstream sources),
this patch is wiped. To restore:
```bash
ssh ubuntu@micro 'sudo diff /opt/op25/op25/gr-op25_repeater/apps/multi_rx.py.pre-sdrplay-close-20260513-152413 /opt/op25/op25/gr-op25_repeater/apps/multi_rx.py'
# If diff is empty → patch was wiped. Re-apply the patch shape above.
```

Verify syntax after re-apply:
```bash
ssh ubuntu@micro 'python3 -c "import ast; ast.parse(open(\"/opt/op25/op25/gr-op25_repeater/apps/multi_rx.py\").read())" && echo OK'
```

No service restart needed — patch takes effect on next op25 SIGINT/restart.

---

## SprontPi — Separate Project (Do NOT deploy SB3 here)

- Host: `sprontpi.local` (Raspberry Pi, also on Tailscale)
- SSH: `willminkoff@sprontpi.local`
- Path: `/home/willminkoff/scannerproject`
- Has its own git checkout — uses `git pull` for updates

---

## Legacy Pi Deployment Reference

## Prerequisites
- SSH access to Pi: `willminkoff@sprontpi.local`
- Git on both machines
- rsync installed

---

## Initial Deployment (First Time)

### 1. Create Backup (One-Time)
```bash
ssh willminkoff@sprontpi.local "mkdir -p /home/willminkoff/backups && cp -r /home/willminkoff/scannerproject/ui /home/willminkoff/backups/ui.backup.$(date +%Y%m%d-%H%M%S)"
```

### 2. Deploy Code
```bash
rsync -avz "/Users/willminkoff/Library/Mobile Documents/com~apple~CloudDocs/Documents/scannerproject/ui/" willminkoff@sprontpi.local:/home/willminkoff/scannerproject/ui/ --exclude='__pycache__' --exclude='*.pyc'
```

### 3. Verify Imports
```bash
ssh willminkoff@sprontpi.local "cd /home/willminkoff/scannerproject && python3 -c 'from ui.app import main; print(\"✓ Imports OK\")'"
```

### 4. Restart Service
```bash
ssh willminkoff@sprontpi.local "sudo systemctl restart airband-ui && sleep 2 && systemctl status airband-ui --no-pager"
```

### 5. Test API
```bash
ssh willminkoff@sprontpi.local "curl -s http://localhost:5050/api/status | python3 -m json.tool | head -20"
```

---

## Update Deployment (Push Changes)

### One-Command Deployment + Restart + Test
```bash
rsync -avz "/Users/willminkoff/Library/Mobile Documents/com~apple~CloudDocs/Documents/scannerproject/ui/" willminkoff@sprontpi.local:/home/willminkoff/scannerproject/ui/ --exclude='__pycache__' --exclude='*.pyc' && ssh willminkoff@sprontpi.local "sudo systemctl restart airband-ui && sleep 2 && curl -s http://localhost:5050/api/status | python3 -m json.tool | head -20"
```

### Safer Multi-Step Deployment
```bash
# 1. Deploy
rsync -avz "/Users/willminkoff/Library/Mobile Documents/com~apple~CloudDocs/Documents/scannerproject/ui/" willminkoff@sprontpi.local:/home/willminkoff/scannerproject/ui/ --exclude='__pycache__' --exclude='*.pyc'

# 2. Verify imports
ssh willminkoff@sprontpi.local "cd /home/willminkoff/scannerproject && python3 -c 'from ui.app import main' && echo '✓ Imports OK' || echo '✗ Import error'"

# 3. Restart
ssh willminkoff@sprontpi.local "sudo systemctl restart airband-ui && sleep 2"

# 4. Check status
ssh willminkoff@sprontpi.local "systemctl is-active airband-ui && echo '✓ Service running' || (echo '✗ Service failed' && journalctl -u airband-ui -n 10 --no-pager)"

# 5. Test endpoints
ssh willminkoff@sprontpi.local "curl -s http://localhost:5050/api/status | head -5"
```

### Deploy Icecast Status Page Template
```bash
cd "/Users/willminkoff/Library/Mobile Documents/com~apple~CloudDocs/Documents/scannerproject"
./scripts/deploy-icecast-status-page.sh
```

Optional target override:
```bash
PI_HOST=192.168.86.91 PI_USER=willminkoff ./scripts/deploy-icecast-status-page.sh
```

### Enable Tailscale HTTPS (Tailnet-Private)
Run on the Pi:
```bash
cd /home/willminkoff/scannerproject
chmod +x scripts/enable-tailscale-https.sh scripts/disable-tailscale-https.sh scripts/verify-tailscale-https.sh
./scripts/enable-tailscale-https.sh
```

Verify HTTPS + LAN HTTP:
```bash
cd /home/willminkoff/scannerproject
./scripts/verify-tailscale-https.sh
```

Disable/rollback HTTPS serve mapping:
```bash
cd /home/willminkoff/scannerproject
./scripts/disable-tailscale-https.sh
```

Canonical secure URL pattern:
```text
https://<node>.<tailnet>.ts.net
```

Example for this node:
```text
https://sprontpi.tail508e50.ts.net/sb3
```

If tailnet Serve is not enabled yet, `enable-tailscale-https.sh` prints the exact Tailscale admin link to enable it.

---

## Verification Commands

### Check Service Status
```bash
ssh willminkoff@sprontpi.local "systemctl status airband-ui --no-pager"
```

### View Recent Logs
```bash
ssh willminkoff@sprontpi.local "journalctl -u airband-ui -n 30 --no-pager"
```

### Test All API Endpoints
```bash
ssh willminkoff@sprontpi.local "
echo '=== Testing API Endpoints ===' && \
echo '1. Status:' && curl -s http://localhost:5050/api/status | python3 -c 'import sys, json; d=json.load(sys.stdin); print(f\"  rtl_active={d[\"rtl_active\"]}, ground_active={d[\"ground_active\"]}\")' && \
echo '2. Hits:' && curl -s http://localhost:5050/api/hits | python3 -c 'import sys, json; d=json.load(sys.stdin); print(f\"  Items: {len(d[\"items\"])}\")' && \
echo '3. UI loads:' && curl -s http://localhost:5050/ | grep -q 'SprontPi Radio Control' && echo '  ✓ HTML loads' || echo '  ✗ HTML failed' && \
echo '4. Static CSS:' && curl -s http://localhost:5050/static/style.css | head -1 && \
echo '5. Static JS:' && curl -s http://localhost:5050/static/script.js | head -1
"
```

### Check Disk Space
```bash
ssh willminkoff@sprontpi.local "du -sh /home/willminkoff/scannerproject/ /home/willminkoff/backups/"
```

---

## Rollback (Emergency)

### Quick Rollback (One-Liner)
```bash
ssh willminkoff@sprontpi.local "BACKUP=\$(ls -t /home/willminkoff/backups/ui.backup.* | head -1) && sudo systemctl stop airband-ui && rm -rf /home/willminkoff/scannerproject/ui && cp -r \"\$BACKUP\" /home/willminkoff/scannerproject/ui && sudo systemctl start airband-ui && sleep 2 && systemctl is-active airband-ui && echo '✓ Rollback complete' || echo '✗ Rollback failed'"
```

### Manual Rollback (Step-by-Step)
```bash
# 1. Stop service
ssh willminkoff@sprontpi.local "sudo systemctl stop airband-ui"

# 2. List backups
ssh willminkoff@sprontpi.local "ls -lah /home/willminkoff/backups/ui.backup.*"

# 3. Restore (replace TIMESTAMP with actual backup date)
BACKUP_TS="20260110-102819"
ssh willminkoff@sprontpi.local "rm -rf /home/willminkoff/scannerproject/ui && cp -r /home/willminkoff/backups/ui.backup.${BACKUP_TS} /home/willminkoff/scannerproject/ui"

# 4. Start service
ssh willminkoff@sprontpi.local "sudo systemctl start airband-ui && sleep 2"

# 5. Verify
ssh willminkoff@sprontpi.local "systemctl is-active airband-ui && echo '✓ Rollback successful' || echo '✗ Rollback failed'"
```

---

## Useful Shortcuts (Add to ~/.zshrc or ~/.bashrc)

```bash
# Deploy to Pi
alias deploy-pi='rsync -avz "/Users/willminkoff/Library/Mobile Documents/com~apple~CloudDocs/Documents/scannerproject/ui/" willminkoff@sprontpi.local:/home/willminkoff/scannerproject/ui/ --exclude="__pycache__" --exclude="*.pyc" && ssh willminkoff@sprontpi.local "sudo systemctl restart airband-ui && sleep 2 && curl -s http://localhost:5050/api/status | head -5"'

# Check Pi status
alias status-pi='ssh willminkoff@sprontpi.local "systemctl status airband-ui --no-pager && echo && curl -s http://localhost:5050/api/status | python3 -m json.tool | head -10"'

# View Pi logs
alias logs-pi='ssh willminkoff@sprontpi.local "journalctl -u airband-ui -n 30 --no-pager"'

# Rollback on Pi
alias rollback-pi='ssh willminkoff@sprontpi.local "BACKUP=\$(ls -t /home/willminkoff/backups/ui.backup.* | head -1) && sudo systemctl stop airband-ui && rm -rf /home/willminkoff/scannerproject/ui && cp -r \"\$BACKUP\" /home/willminkoff/scannerproject/ui && sudo systemctl start airband-ui && sleep 2 && systemctl is-active airband-ui && echo \"✓ Rollback complete\""'
```

Usage:
```bash
deploy-pi      # Deploy and restart
status-pi      # Check service status
logs-pi        # View logs
rollback-pi    # Rollback to backup
```

---

## Common Scenarios

### Scenario 1: I Made Code Changes and Want to Deploy
```bash
# From your development machine:
cd ~/Projects/scannerproject
git add .
git commit -m "Your commit message"
git push

# Then deploy to Pi:
deploy-pi
```

### Scenario 2: Service Won't Start
```bash
# Check logs
logs-pi

# If it's an import error, try rollback
rollback-pi

# If rollback works, investigate what changed
git log --oneline | head -5
```

### Scenario 3: API Endpoint Not Responding
```bash
# Check service is running
status-pi

# Check specific endpoint
ssh willminkoff@sprontpi.local "curl -s http://localhost:5050/api/status | python3 -m json.tool"

# If service crashed, check logs
logs-pi
```

### Scenario 4: Regular Maintenance Deploy
```bash
# Make changes locally
nano ui/handlers.py

# Test locally if possible
python3 -c "from ui.handlers import Handler; print('✓ Module loads')"

# Deploy
deploy-pi

# Verify
status-pi
```

---

## Notes

- **rsync**: Only transfers changed files (efficient)
- **--exclude**: Ignores `__pycache__` and `.pyc` files (keeps transfer small)
- **sleep 2**: Gives service time to start before checking status
- **Backup**: Created on first deployment, can be used for emergency rollback
- **Logs**: Check `journalctl -u airband-ui` if anything fails

---

## Copy-Paste Command for Current State

**Fastest way to deploy everything right now:**
```bash
rsync -avz "/Users/willminkoff/Library/Mobile Documents/com~apple~CloudDocs/Documents/scannerproject/ui/" willminkoff@sprontpi.local:/home/willminkoff/scannerproject/ui/ --exclude='__pycache__' --exclude='*.pyc' && ssh willminkoff@sprontpi.local "sudo systemctl restart airband-ui && sleep 2 && curl -s http://localhost:5050/api/status | python3 -m json.tool | head -20"
```

This single command:
1. ✓ Syncs all UI files to Pi
2. ✓ Restarts the service
3. ✓ Tests the API
