# Pi Deployment Commands - Quick Reference

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
