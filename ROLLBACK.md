# Rollback Plan for Ticket 29 Refactoring

## Summary
Ticket 29 refactored the monolithic `airband_ui.py` (1,928 lines) into 11 modular Python modules + static assets. A backup was created on the Pi before deployment. This document provides step-by-step rollback instructions if issues arise.

## Backup Location on Pi
```
/home/willminkoff/backups/ui.backup.YYYYMMDD-HHMMSS/
```

## When to Rollback
Trigger rollback if ANY of these occur:
- Service fails to start: `systemctl status airband-ui` shows failed/inactive
- API endpoints return 500 errors (check: `curl http://localhost:5050/api/status`)
- UI doesn't load or is completely broken
- Hit tracking stops working (no updates in API response)
- Profile switching fails (returns errors in API responses)
- Control changes (gain/squelch) don't apply
- Service crashes repeatedly (check logs: `journalctl -u airband-ui -f`)

## Rollback Steps

### 1. Stop the Service
```bash
sudo systemctl stop airband-ui
```

### 2. List Available Backups
```bash
ls -la /home/willminkoff/backups/ui.backup.*
```

### 3. Identify the Most Recent Backup
The backup format is: `ui.backup.YYYYMMDD-HHMMSS`
The most recent one will have the latest timestamp.

Example:
```bash
/home/willminkoff/backups/ui.backup.20260110-102935/
```

### 4. Restore the Previous Version
```bash
# Set the backup timestamp (replace with actual date/time)
BACKUP_TS="20260110-102935"

# Remove current ui directory
rm -rf /home/willminkoff/scannerproject/ui/

# Restore from backup
cp -r /home/willminkoff/backups/ui.backup.${BACKUP_TS}/ /home/willminkoff/scannerproject/ui/

# Verify files were restored
ls -la /home/willminkoff/scannerproject/ui/
```

### 5. Start the Service
```bash
sudo systemctl start airband-ui
sleep 2
systemctl status airband-ui
```

### 6. Verify Service is Working
```bash
# Check if service is active
systemctl is-active airband-ui

# Test API endpoint
curl -s http://localhost:5050/api/status | head -20

# Check logs for errors
journalctl -u airband-ui -n 20 --no-pager
```

## Complete Automated Rollback Script

Save this as `/home/willminkoff/rollback.sh` on the Pi for quick execution:

```bash
#!/bin/bash
set -e

echo "=== Airband UI Rollback Script ==="
echo ""

# Get the most recent backup
BACKUP=$(ls -t /home/willminkoff/backups/ui.backup.* 2>/dev/null | head -1)

if [ -z "$BACKUP" ]; then
    echo "ERROR: No backups found in /home/willminkoff/backups/"
    exit 1
fi

echo "Found backup: $BACKUP"
echo ""

# Stop the service
echo "Stopping airband-ui service..."
sudo systemctl stop airband-ui

# Restore the backup
echo "Restoring from backup..."
rm -rf /home/willminkoff/scannerproject/ui/
cp -r "$BACKUP" /home/willminkoff/scannerproject/ui/

# Start the service
echo "Starting airband-ui service..."
sudo systemctl start airband-ui
sleep 2

# Verify
echo ""
if systemctl is-active --quiet airband-ui; then
    echo "✓ Service is running"
    echo ""
    echo "Testing API..."
    curl -s http://localhost:5050/api/status | python3 -m json.tool | head -20
    echo ""
    echo "✓ ROLLBACK SUCCESSFUL"
else
    echo "✗ Service failed to start"
    echo "Checking logs:"
    journalctl -u airband-ui -n 30 --no-pager
    exit 1
fi
```

To use:
```bash
chmod +x /home/willminkoff/rollback.sh
/home/willminkoff/rollback.sh
```

## What Was Changed (For Reference)

### Files Created
- `ui/__init__.py` - Package marker
- `ui/config.py` - Centralized configuration (4.4 KB)
- `ui/systemd.py` - Systemd unit control (2.2 KB)
- `ui/icecast.py` - Icecast monitoring (2.0 KB)
- `ui/scanner.py` - Hit tracking (11.2 KB)
- `ui/profile_config.py` - Profile management (12.8 KB)
- `ui/actions.py` - Business logic (5.6 KB)
- `ui/diagnostic.py` - Diagnostics (3.9 KB)
- `ui/server_workers.py` - Worker threads (3.8 KB)
- `ui/handlers.py` - HTTP handlers (8.8 KB)
- `ui/app.py` - Server setup (637 B)
- `ui/static/index.html` - HTML template (5.2 KB)
- `ui/static/style.css` - Styling (5.0 KB)
- `ui/static/script.js` - Client logic (14.3 KB)

### Files Modified
- `ui/airband_ui.py` - Refactored to thin entry point (336 B, was 1,928 lines)

### Backup Date
January 10, 2026, ~10:29 AM CST

## If Rollback Doesn't Fix Issues

1. Check service logs for errors:
   ```bash
   journalctl -u airband-ui -n 50 --no-pager
   ```

2. Verify the symlink and config file are correct:
   ```bash
   ls -la /etc/airband-ui-active.conf
   cat /etc/airband-ui-active.conf
   ```

3. Check if the previous version (from backup) also has the issue - this would indicate a system-level problem, not a code problem.

4. If the backup also fails, the issue is likely:
   - Systemd configuration changed
   - A system dependency is missing (Python version, library, etc.)
   - The rtl-airband or icecast services are failing upstream

5. Check those services:
   ```bash
   systemctl status rtl-airband rtl-airband-ground icecast2
   journalctl -u rtl-airband -n 20 --no-pager
   ```

## Testing the Refactored Version

Before deciding a rollback is necessary, verify these endpoints work:

```bash
# Status endpoint (comprehensive)
curl -s http://localhost:5050/api/status | python3 -m json.tool

# Hit list
curl -s http://localhost:5050/api/hits | python3 -m json.tool

# Apply control (test only, safe to run)
curl -s -X POST http://localhost:5050/api/apply \
  -H 'Content-Type: application/json' \
  -d '{"target":"airband","gain":20,"squelch":5}' | python3 -m json.tool

# Avoid endpoint
curl -s -X POST http://localhost:5050/api/avoid \
  -H 'Content-Type: application/json' \
  -d '{"target":"airband"}' | python3 -m json.tool

# UI loads
curl -s http://localhost:5050/ | grep -q "SprontPi Radio Control" && echo "✓ UI loads"
```

All endpoints should return `{"ok": true}` or valid JSON data (no error messages).

## Summary of Backup Contents

The backup contains the **previous working version** of:
- All Python UI modules (config, handlers, server_workers, etc.)
- All static assets (HTML, CSS, JavaScript)
- The entry point script

Rolling back restores the system to the exact state before the refactoring deployment.

---

**Rollback Plan Last Updated:** January 10, 2026
**Refactoring Commit:** 0b270dc (main branch)
