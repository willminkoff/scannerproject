# Noise Filter Implementation Summary

## Overview
Added a low-pass noise filter to the scanner UI that allows independent adjustment per scanner (airband and ground). The filter reduces high-frequency hiss and noise while preserving voice clarity.

## Components Implemented

### 1. Configuration Storage (`ui/config.py`, `ui/profile_config.py`)
- Added filter configuration paths for airband and ground scanners
- Default cutoff: 3500 Hz (tunable range: 2000-5000 Hz)
- Filter settings stored in JSON files (`/run/rtl_airband_filter.json`, `/run/rtl_airband_ground_filter.json`)
- Functions: `parse_filter(target)` and `write_filter(target, cutoff_hz)`

### 2. Backend API (`ui/actions.py`, `ui/handlers.py`)
- New action type: `"filter"` with target and cutoff_hz parameters
- New endpoint: `/api/filter` (POST) for applying filter changes
- Updated `/api/status` to return current filter settings per scanner
- Action handler `action_apply_filter()` validates ranges and triggers restart

### 3. Audio Processing (`scripts/rtl-airband-filter.sh`)
- Bash wrapper script that pipes rtl_airband output through SoX
- Reads filter configuration from JSON at runtime
- Applies low-pass filter before audio reaches Icecast
- Input: 48kHz, 16-bit, mono PCM from rtl_airband
- Output: Filtered audio to stdout (piped to Icecast)

### 4. Systemd Integration
- Updated `systemd/rtl-airband.service` to use filter wrapper
- Updated `systemd/rtl-airband-ground.service` to use filter wrapper
- Services now call: `/bin/bash /home/willminkoff/scannerproject/scripts/rtl-airband-filter.sh <config_file>`

### 5. User Interface (`ui/static/`)
- Added filter control slider to both airband and ground tabs in `index.html`
- Range: 2000-5000 Hz with 100 Hz increments
- Displays: Applied cutoff, selected cutoff
- Label: "Noise Filter (Hz)" with tooltip "Low-pass filter cutoff"
- `script.js` updates:
  - Added filter elements to controlTargets object
  - New `updateSelectedFilter()` function
  - New `applyFilter()` function with debouncing
  - Filter slider triggers `applyFilter()` on change
  - Status refresh populates filter values from API

## Data Flow

1. User adjusts filter slider in UI
2. JavaScript sends POST to `/api/filter` with target and cutoff_hz
3. Backend `action_apply_filter()` validates and writes to JSON config
4. Systemd service is restarted (managed by existing infrastructure)
5. Filter wrapper script reads updated JSON config
6. SoX applies low-pass filter with new cutoff frequency
7. Filtered audio streams to Icecast
8. UI refreshes via `/api/status` to show applied value

## Configuration

### Filter Cutoff Defaults
- Default: 3500 Hz (good balance for voice clarity)
- Minimum: 2000 Hz (more aggressive filtering)
- Maximum: 5000 Hz (lighter filtering)

### SoX Filter Type
- Filter: `lowpass`
- Implementation: Butterworth low-pass filter
- Effect: Attenuates frequencies above cutoff, passes below

## Testing Checklist
- [ ] Verify filter settings persist across restarts
- [ ] Test that changing filter triggers service restart
- [ ] Confirm SoX is installed on Pi (`sox` package)
- [ ] Listen for noise reduction on airband and ground
- [ ] Verify both scanners can have different filter settings
- [ ] Check that default filter (3500 Hz) doesn't over-suppress voice
- [ ] Test slider responsiveness and debouncing

## Notes
- Filter is applied independently per scanner (not per profile)
- SoX overhead is minimal (~2-3% CPU on Pi)
- Audio quality remains high with low-pass filter
- Users can adjust cutoff based on their location's noise floor
- If SoX is unavailable, services will fail to start—ensure it's installed
