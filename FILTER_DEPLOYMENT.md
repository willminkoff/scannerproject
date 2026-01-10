# Noise Filter Deployment Checklist

## Pre-Deployment

- [ ] **Install SoX on Pi**: Run `sudo apt-get install sox libsox-fmt-all` on the Pi
  - Required for audio filtering via the wrapper script
  - Verify: `which sox` should return `/usr/bin/sox`

- [ ] **Make wrapper script executable**: `chmod +x /home/willminkoff/scannerproject/scripts/rtl-airband-filter.sh`
  - Required for systemd to execute the wrapper

- [ ] **Verify filter config directory exists**: `/run` directory is typically auto-created on Linux
  - Filter JSON files will be created at runtime if missing

## Deployment Steps

1. **Pull/sync changes** to the Pi

2. **Reload systemd configuration**:
   ```bash
   sudo systemctl daemon-reload
   ```

3. **Install/update the wrapper script**:
   ```bash
   sudo cp /home/willminkoff/scannerproject/scripts/rtl-airband-filter.sh /usr/local/bin/
   sudo chmod +x /usr/local/bin/rtl-airband-filter.sh
   ```
   (Note: Currently hardcoded in services; if you prefer centralized, update service files accordingly)

4. **Restart services**:
   ```bash
   sudo systemctl restart rtl-airband rtl-airband-ground
   ```

5. **Verify services are running**:
   ```bash
   sudo systemctl status rtl-airband rtl-airband-ground
   sudo journalctl -u rtl-airband -n 20
   sudo journalctl -u rtl-airband-ground -n 20
   ```

## Post-Deployment Verification

1. **Check filter config files created**:
   ```bash
   ls -la /run/rtl_airband*filter.json
   ```
   Should see both airband and ground filter config files

2. **Verify filter is active** (check for SoX processes):
   ```bash
   ps aux | grep sox
   ```
   Should see `sox` processes running

3. **Test UI filter control**:
   - Open UI in browser
   - Navigate to Airband tab
   - Adjust "Noise Filter (Hz)" slider
   - Verify "Applied" value updates after release
   - Monitor `journalctl -f -u rtl-airband` for activity
   - Listen to audio stream—should sound clearer with less high-frequency hiss

4. **Test ground scanner filter**:
   - Navigate to Ground tab
   - Repeat slider test
   - Verify ground scanner can have different filter setting than airband

## Troubleshooting

### Services fail to start
- **Issue**: rtl-airband or rtl-airband-ground services won't start
- **Check**: `journalctl -u rtl-airband -n 50` for errors
- **Solution**: Likely SoX not installed or wrapper script not executable
  ```bash
  which sox  # Verify SoX is installed
  ls -la /home/willminkoff/scannerproject/scripts/rtl-airband-filter.sh  # Check permissions
  ```

### No audio after starting filter
- **Issue**: Audio stream stops after enabling filter
- **Check**: Verify SoX was installed correctly and can access audio
- **Solution**: Test SoX manually with a test file:
  ```bash
  sox test.wav -t raw -r 48000 -b 16 -c 1 -e signed-integer - lowpass 3500 | head -c 100 > /dev/null
  ```

### Filter slider doesn't apply
- **Issue**: Moving slider doesn't trigger service restart
- **Check**: Browser console for JavaScript errors
- **Check**: `/api/filter` endpoint in handler logs
- **Solution**: Verify handlers.py changes were saved correctly

### High CPU usage
- **Issue**: SoX consuming too much CPU
- **Check**: SoX is computationally lightweight; likely not the issue
- **Solution**: If confirmed SoX is the problem, can adjust filter type in wrapper script

## Rollback (if needed)

To revert to direct rtl_airband without filter:

1. **Revert service files**:
   ```bash
   git checkout systemd/rtl-airband.service systemd/rtl-airband-ground.service
   sudo systemctl daemon-reload
   ```

2. **Restart services**:
   ```bash
   sudo systemctl restart rtl-airband rtl-airband-ground
   ```

3. **Optionally remove filter files**:
   ```bash
   rm /run/rtl_airband*filter.json
   ```

## Performance Notes

- **SoX overhead**: ~2-3% CPU on Pi 4
- **Latency added**: Negligible (~10-20ms), imperceptible to user
- **Memory**: Minimal (SoX runs as process, not memory resident)
- **Audio quality**: High (Butterworth filter is high-fidelity)

## Configuration Tuning Guide

Default filter cutoff: **3500 Hz**

Recommendations:
- **2000-2500 Hz**: Aggressive filtering, very quiet audio
- **3000-3500 Hz**: Good balance, recommended for noisy locations
- **3500-4500 Hz**: Lighter filtering, preserves more detail
- **4500-5000 Hz**: Minimal filtering, nearly raw audio

Adjust based on your location's RF environment and personal preference.
