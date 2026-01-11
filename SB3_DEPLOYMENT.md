# SB3 UI Deployment Checklist

## What's Ready
- ✅ `mockup_sb3.html` - Full UI with frequency monitor
- ✅ `spectrum.py` - Parses rtl_airband stats file
- ✅ `handlers.py` - SSE endpoint at `/api/stream`, serves `/sb3`
- ✅ Horizontal scroll for 30+ frequencies

## On the Pi

### 1. Deploy the updated config (with stats_filepath)

```bash
# Copy updated config to Pi
scp rtl_airband_combined.conf pi@scanner:/usr/local/etc/rtl_airband_combined.conf
scp profiles/rtl_airband_airband.conf pi@scanner:/usr/local/etc/airband-profiles/
```

Or if you're using the combined config:
```bash
# The key line to ensure is in your config:
stats_filepath = "/run/rtl_airband_stats.txt";
```

### 2. Copy the UI files

```bash
# Copy entire ui/ folder
scp -r ui/ pi@scanner:~/scannerproject/ui/
```

### 3. Restart rtl_airband

```bash
sudo systemctl restart rtl-airband
```

### 4. Verify stats file is being written

```bash
# Should show Prometheus metrics every 15 seconds
cat /run/rtl_airband_stats.txt

# Watch it update
watch -n 5 cat /run/rtl_airband_stats.txt
```

Expected format:
```
# HELP rtl_airband_freq_noise_floor Current noise floor in dB
rtl_airband_freq_noise_floor{freq="118.400000"} -42.5
rtl_airband_freq_noise_floor{freq="118.600000"} -41.2
...
# HELP rtl_airband_freq_active_counter Number of times frequency was active
rtl_airband_freq_active_counter{freq="118.400000"} 127
```

### 5. Start the UI server

```bash
cd ~/scannerproject
python3 -m ui.app
```

Or via systemd:
```bash
sudo systemctl restart airband-ui
```

### 6. Access the SB3 UI

Open in browser:
```
http://<pi-ip>:5050/sb3
```

## Testing Without Hardware

On your Mac (with no stats file), the UI auto-detects simulation mode:

```bash
cd ~/Library/Mobile\ Documents/com~apple~CloudDocs/Documents/scannerproject
python3 -m ui.app
```

Then open: http://localhost:5050/sb3

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `UI_PORT` | 5050 | Server port |
| `RTL_AIRBAND_STATS_PATH` | `/run/rtl_airband_stats.txt` | Stats file location |

## Troubleshooting

**Stats file not found / empty**
- Check rtl_airband is running: `systemctl status rtl-airband`
- Check config has `stats_filepath` in correct location (inside `channels()` block)
- Check permissions: `ls -la /run/rtl_airband*`

**No data in spectrum widget**
- Open browser console (F12) - check for SSE connection errors
- Test SSE directly: `curl http://localhost:5050/api/stream`

**Frequencies don't match config**
- Update `CONFIGURED_FREQS` array in mockup_sb3.html to match your profile
- Or wait for dynamic frequency loading (future feature)
