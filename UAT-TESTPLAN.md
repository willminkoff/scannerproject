# Ticket 29 UAT - Refactored UI Module Testing Plan

## Overview
Test the refactored modular airband_ui.py in a controlled UAT environment. The refactoring splits the 1,928-line monolithic file into 11 functional modules + static assets.

**UAT Environment**: Pi at `sprontpi.local:5050`
**Test Duration**: ~30-45 minutes for full suite
**Pass Criteria**: All tests marked ✓ PASS

---

## Pre-UAT Setup

### 1. Verify Deployment Status
```bash
# On development machine:
ssh willminkoff@sprontpi.local "systemctl status airband-ui --no-pager"
```

**Expected Output**: Service should be `active (running)`

### 2. Clear Browser Cache (Important!)
- Open http://sprontpi.local:5050
- Hard refresh: `Cmd+Shift+R` (Mac) or `Ctrl+Shift+R` (Windows/Linux)
- Check Network tab in DevTools to ensure CSS/JS are fresh (not cached)

### 3. Check Service Logs (Baseline)
```bash
ssh willminkoff@sprontpi.local "journalctl -u airband-ui -n 5 --no-pager"
```

**Expected**: No error messages, recent timestamp

---

## Test Suite

### Phase 1: UI Load & Asset Tests (5 minutes)

#### 1.1 - UI Loads at Correct URL
- [ ] Open `http://sprontpi.local:5050` in browser
- [ ] Page loads without errors
- [ ] Title shows "SprontPi Radio Control"
- [ ] **Status**: PASS / FAIL

#### 1.2 - Static CSS Loads
- [ ] DevTools Network tab: Check `style.css`
- [ ] Status code should be **200**
- [ ] Content-Type: `text/css`
- [ ] File size > 0 bytes
- [ ] **Status**: PASS / FAIL

#### 1.3 - Static JavaScript Loads
- [ ] DevTools Network tab: Check `script.js`
- [ ] Status code should be **200**
- [ ] Content-Type: `application/javascript`
- [ ] File size > 0 bytes
- [ ] No console errors on page load
- [ ] **Status**: PASS / FAIL

#### 1.4 - Styling Applied Correctly
- [ ] Dark theme visible (dark background, light text)
- [ ] Hit pills display with color indicators (green/red dots)
- [ ] Profile cards render in 2-column grid
- [ ] Buttons have proper styling and are clickable
- [ ] **Status**: PASS / FAIL

---

### Phase 2: API Endpoint Tests (10 minutes)

#### 2.1 - /api/status Endpoint
```bash
curl -s http://sprontpi.local:5050/api/status | python3 -m json.tool
```

**Verify JSON Response**:
- [ ] `rtl_active` is boolean (true/false)
- [ ] `ground_active` is boolean
- [ ] `icecast_active` is boolean
- [ ] `profile_airband` exists and is string
- [ ] `profile_ground` exists and is string
- [ ] `profiles_airband` is array with objects
- [ ] `profiles_ground` is array with objects
- [ ] HTTP Status: **200**
- [ ] **Status**: PASS / FAIL

#### 2.2 - /api/hits Endpoint
```bash
curl -s http://sprontpi.local:5050/api/hits | python3 -m json.tool
```

**Verify JSON Response**:
- [ ] `items` is array
- [ ] Each item has: `time`, `freq`, `duration` fields
- [ ] Frequencies are formatted as "###.####" (e.g., "126.0500")
- [ ] Times are formatted as "HH:MM:SS"
- [ ] Durations are numeric
- [ ] HTTP Status: **200**
- [ ] **Status**: PASS / FAIL

#### 2.3 - /api/status Reflects Current State
- [ ] Check which profile is currently active (airband)
- [ ] Verify `profile_airband` in /api/status matches active profile
- [ ] Check if any frequencies are currently being scanned (last hit time should be recent)
- [ ] **Status**: PASS / FAIL

---

### Phase 3: UI Functionality Tests (15 minutes)

#### 3.1 - Last Hit Display
- [ ] Main screen shows "Airband Hits" pill with frequency
- [ ] Frequency displays in "###.####" format (e.g., "126.0500")
- [ ] When new traffic occurs, frequency updates within 5 seconds
- [ ] **Status**: PASS / FAIL

#### 3.2 - Hit List Navigation
- [ ] Click on "Airband Hits" pill
- [ ] Page switches to hit list view
- [ ] Shows "Last 20 Hits" with time, frequency, duration
- [ ] Hits are sorted newest first
- [ ] Click back arrow returns to main view
- [ ] **Status**: PASS / FAIL

#### 3.3 - Ground Scanner Display
- [ ] Main view has two tabs: "Airband" and "Ground"
- [ ] Click "Ground" tab
- [ ] Ground last hit pill displays (may show "No hits" if no activity)
- [ ] Same hit list functionality works for ground
- [ ] **Status**: PASS / FAIL

#### 3.4 - Profile Selection (Airband)
- [ ] Under "Airband" tab, see profile cards in 2-column grid
- [ ] Profiles visible: KBNA, Nashville Centers, Tower, etc.
- [ ] Currently selected profile has visual highlight (darker/different style)
- [ ] Click a different profile (e.g., "Nashville Centers")
- [ ] Profile becomes selected (visual highlight changes)
- [ ] Service does NOT restart (should be instant)
- [ ] **Status**: PASS / FAIL

#### 3.5 - Profile Selection (Ground)
- [ ] Switch to "Ground" tab
- [ ] See ground profiles
- [ ] Click a different ground profile
- [ ] Profile selection updates
- [ ] **Status**: PASS / FAIL

#### 3.6 - Gain Control
- [ ] Find "Gain" slider
- [ ] Current gain value displays (e.g., "20")
- [ ] Drag slider to a different position
- [ ] Value updates in real-time
- [ ] After 2 seconds of inactivity, control is applied to scanner
- [ ] Service remains running (no restart needed)
- [ ] **Status**: PASS / FAIL

#### 3.7 - Squelch Control
- [ ] Find "Squelch" slider (shows 0-10 range)
- [ ] Current squelch value displays
- [ ] Drag slider left/right
- [ ] Value updates in real-time
- [ ] After 2 seconds of inactivity, control is applied
- [ ] Service remains running
- [ ] **Status**: PASS / FAIL

#### 3.8 - Refresh Button
- [ ] Locate refresh button (circular arrow icon)
- [ ] Click refresh button
- [ ] UI syncs without restarting service
- [ ] Gain/squelch sliders snap to current values from scanner
- [ ] Hit pills update if new traffic occurred
- [ ] **Status**: PASS / FAIL

---

### Phase 4: Control Application Tests (8 minutes)

#### 4.1 - Gain Application via API
```bash
curl -s -X POST http://sprontpi.local:5050/api/apply \
  -H 'Content-Type: application/json' \
  -d '{"target":"airband","gain":25,"squelch":5}' | python3 -m json.tool
```

**Verify**:
- [ ] Response: `{"ok": true, "changed": true}` or `{"ok": true, "changed": false}`
- [ ] Service continues running
- [ ] No errors in logs
- [ ] **Status**: PASS / FAIL

#### 4.2 - Squelch Application via API
```bash
curl -s -X POST http://sprontpi.local:5050/api/apply \
  -H 'Content-Type: application/json' \
  -d '{"target":"airband","gain":20,"squelch":8}' | python3 -m json.tool
```

**Verify**:
- [ ] Response indicates success
- [ ] Service stable
- [ ] No errors in logs
- [ ] **Status**: PASS / FAIL

#### 4.3 - Ground Control Application
```bash
curl -s -X POST http://sprontpi.local:5050/api/apply \
  -H 'Content-Type: application/json' \
  -d '{"target":"ground","gain":22,"squelch":6}' | python3 -m json.tool
```

**Verify**:
- [ ] Response indicates success (or no change if already set)
- [ ] Service stable
- [ ] Ground scanner continues operating
- [ ] **Status**: PASS / FAIL

---

### Phase 5: Avoids Functionality (5 minutes)

#### 5.1 - Add Avoid
```bash
curl -s -X POST http://sprontpi.local:5050/api/avoid \
  -H 'Content-Type: application/json' \
  -d '{"target":"airband"}' | python3 -m json.tool
```

**Verify**:
- [ ] Response contains current frequency (e.g., `{"ok": true, "freq": "126.0500"}`)
- [ ] Service remains running
- [ ] **Status**: PASS / FAIL

#### 5.2 - Avoid Summary Display
- [ ] Check UI for "Avoids" section
- [ ] Should show count of avoided frequencies
- [ ] Should show sample of frequencies (e.g., "3 avoided: 126.05, 128.15, ...")
- [ ] **Status**: PASS / FAIL

#### 5.3 - Clear Avoids
```bash
curl -s -X POST http://sprontpi.local:5050/api/avoid-clear \
  -H 'Content-Type: application/json' \
  -d '{"target":"airband"}' | python3 -m json.tool
```

**Verify**:
- [ ] Response: `{"ok": true}`
- [ ] Avoids count goes back to 0
- [ ] Service stable
- [ ] **Status**: PASS / FAIL

---

### Phase 6: Error Handling & Edge Cases (5 minutes)

#### 6.1 - Invalid Gain Value
```bash
curl -s -X POST http://sprontpi.local:5050/api/apply \
  -H 'Content-Type: application/json' \
  -d '{"target":"airband","gain":999,"squelch":5}'
```

**Expected**: Error response or value clamped to valid range
- [ ] **Status**: PASS / FAIL

#### 6.2 - Invalid Target
```bash
curl -s -X POST http://sprontpi.local:5050/api/apply \
  -H 'Content-Type: application/json' \
  -d '{"target":"invalid","gain":20,"squelch":5}'
```

**Expected**: Error response with message
- [ ] **Status**: PASS / FAIL

#### 6.3 - Missing Required Fields
```bash
curl -s -X POST http://sprontpi.local:5050/api/apply \
  -H 'Content-Type: application/json' \
  -d '{"target":"airband"}'
```

**Expected**: Error response
- [ ] **Status**: PASS / FAIL

#### 6.4 - Service Stability Check
```bash
ssh willminkoff@sprontpi.local "systemctl is-active airband-ui"
```

**Expected**: `active`
- [ ] **Status**: PASS / FAIL

#### 6.5 - No Errors in Logs
```bash
ssh willminkoff@sprontpi.local "journalctl -u airband-ui -n 50 --no-pager | grep -i error"
```

**Expected**: No matches (or only pre-test errors)
- [ ] **Status**: PASS / FAIL

---

### Phase 7: Performance Tests (5 minutes)

#### 7.1 - Page Load Time
- [ ] Open DevTools Performance tab
- [ ] Refresh page (hard refresh)
- [ ] Page should fully load in < 3 seconds
- [ ] Record page load time: _______ seconds
- [ ] **Status**: PASS / FAIL

#### 7.2 - API Response Time
```bash
time curl -s http://sprontpi.local:5050/api/status > /dev/null
```

**Expected**: Response time < 500ms
- [ ] Record time: _______ ms
- [ ] **Status**: PASS / FAIL

#### 7.3 - Slider Responsiveness
- [ ] Move gain slider smoothly left and right
- [ ] UI should respond without lag
- [ ] Should feel smooth, not jerky
- [ ] **Status**: PASS / FAIL

#### 7.4 - Control Application Latency
- [ ] Apply gain change
- [ ] Measure time until change is applied to scanner
- [ ] Should be < 5 seconds (includes debounce time)
- [ ] Record latency: _______ seconds
- [ ] **Status**: PASS / FAIL

---

## Test Results Summary

### Metrics
```
Total Tests: _____ / 38
Passed: _____ 
Failed: _____
Pass Rate: _____%
```

### Failed Tests (if any)
List any failed tests and details:
1. 
2. 
3. 

### Issues Found
```
Issue #1: [Description]
Severity: Critical / High / Medium / Low
Reproduction Steps: ...
Expected: ...
Actual: ...

Issue #2: [Description]
...
```

### Performance Metrics
- Page Load Time: _______ seconds
- API Response Time: _______ ms
- Control Application Latency: _______ seconds
- Browser: ____________
- Network Condition: Good / Fair / Poor

---

## Sign-Off

### UAT Approval
- **Tested By**: ___________________
- **Date**: ___________________
- **Result**: ☐ PASS  ☐ FAIL  ☐ PASS WITH ISSUES
- **Comments**: ________________________________

### Ready for Production
- [ ] All critical tests pass
- [ ] No blocking issues found
- [ ] Performance acceptable
- [ ] Code is stable

---

## Rollback Instructions (If Needed)

If any critical issues are found, rollback to previous version:

```bash
# On the Pi:
ssh willminkoff@sprontpi.local "BACKUP=\$(ls -t /home/willminkoff/backups/ui.backup.* | head -1) && \
sudo systemctl stop airband-ui && \
rm -rf /home/willminkoff/scannerproject/ui && \
cp -r \"\$BACKUP\" /home/willminkoff/scannerproject/ui && \
sudo systemctl start airband-ui && sleep 2 && \
systemctl is-active airband-ui && echo '✓ Rollback complete'"
```

See **ROLLBACK.md** for detailed rollback procedures.

---

## Quick Test Commands (Copy-Paste)

```bash
# Test all endpoints in sequence
echo "=== Testing API ===" && \
curl -s http://sprontpi.local:5050/api/status | head -5 && echo && \
curl -s http://sprontpi.local:5050/api/hits | head -5 && echo && \
echo "=== Testing UI Load ===" && \
curl -s http://sprontpi.local:5050/ | grep -q "SprontPi" && echo "✓ HTML loads" && \
echo "=== All tests complete ==="
```

---

## Notes

- **Browser**: Test in Chrome/Firefox/Safari to verify cross-browser compatibility
- **Network**: Test on both WiFi and wired connection
- **Cache**: Always hard refresh (Cmd+Shift+R) when testing CSS/JS changes
- **Logs**: Check logs frequently for any errors: `journalctl -u airband-ui -f`
- **Service**: If service crashes, it should auto-restart (systemd restart policy)

---

**UAT Test Plan Created**: January 10, 2026
**Reference Ticket**: #29 - Split airband_ui.py into modules and static assets
**Reference Commit**: 8d1f6ff
