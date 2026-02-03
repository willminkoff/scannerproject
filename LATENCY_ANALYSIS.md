# SprontPi Latency Analysis: Antenna to Ear

## Overview
This document analyzes the end-to-end latency from RF signal reception at the SDR antenna through to audio playback on a listener's device.

## Signal Path Components

```
RF Signal (antenna)
    ↓
[1] RTL-SDR USB Dongle (tuning, filtering, demodulation)
    ↓
[2] rtl_airband (demodulation, frame buffering, MP3 encoding)
    ↓
[3] MP3 Stream → Icecast Server (network buffering)
    ↓
[4] Client Network Buffering & HTTP Stream Consumer
    ↓
Audio Playback (speaker/headphones)
```

## Latency Component Breakdown

### [1] RTL-SDR Hardware + USB Pipeline
- **RTL2832U tuner + R820T front-end**: ~1-2 ms (RF to digital conversion)
- **USB bulk transfer buffering**: ~5-10 ms (device → host latency)
- **Driver overhead**: ~2-5 ms
- **Typical hardware latency**: **8-17 ms**

### [2] rtl_airband Processing
**Current Configuration (from `${COMBINED_CONFIG_PATH}`; defaults to `/usr/local/etc/rtl_airband_combined.conf`):**
- **Sample Rate**: 8000 Hz (8 kHz audio)
- **MP3 Bitrate**: 16 kbps
- **Encoder Frame Size**: MP3 frames = 1152 samples @ 8kHz = **144 ms per frame**
- **Mode**: `mode = "scan"` (continuous scanning with frequency switching)

**Processing Latency Estimate**:
- MP3 frame encoding: ~10-20 ms
- Buffering before Icecast output: ~50-100 ms  
- Frequency switching overhead (in scan mode): ~5-15 ms per channel
- **rtl_airband typical latency**: **100-200 ms**

**Key Factor**: At 16 kbps and 8 kHz sample rate, each MP3 frame contains 144 ms of audio but encodes relatively quickly. The main delay is frame accumulation.

### [3] Icecast Server Buffering
**Current Configuration (from `/etc/icecast2/icecast.xml`):**
```xml
<queue-size>524288</queue-size>           <!-- 512 KB buffer -->
<burst-on-connect>1</burst-on-connect>    <!-- Send burst on new connections -->
<burst-size>65536</burst-size>            <!-- 64 KB burst -->
```

**Icecast Latency**:
- Queue buffer holds ~512 KB of data
- At 16 kbps = 2 KB/s → queue holds ~256 seconds of data
- New connection burst sends 64 KB immediately (~32 seconds at 16 kbps)
- Stream ingestion from rtl_airband: ~50-100 ms
- **Icecast server latency**: **50-150 ms**

### [4] Client-Side Network & Playback Buffering
**Factors**:
- HTTP streaming typically buffers 2-5 seconds before playback
- Browser/VLC player buffering: 1-3 seconds typical
- OS audio buffer: 20-100 ms
- Network jitter on local LAN: 5-20 ms
- **Client buffering latency**: **1-5+ seconds**

## Total End-to-End Latency

**Best Case (LAN, optimized client)**:
- RTL-SDR: 10 ms
- rtl_airband: 150 ms  
- Icecast: 75 ms
- Client: 1000 ms (1 second minimum)
- **Total: ~1.2 seconds (1200 ms)**

**Typical Case**:
- RTL-SDR: 15 ms
- rtl_airband: 175 ms
- Icecast: 100 ms
- Client: 2000-3000 ms (depends on stream client)
- **Total: ~2.3-3.3 seconds (2300-3300 ms)**

**Worst Case (congested network, large buffers)**:
- RTL-SDR: 20 ms
- rtl_airband: 200 ms
- Icecast: 150 ms
- Client: 5000+ ms (5+ seconds if client buffers heavily)
- **Total: 5.4+ seconds**

## Observed Latency Markers

From systemd journal timestamps:
```
Jan 10 17:54:05 SprontPi bash: Activity on 467.625 MHz
Jan 10 17:54:10 SprontPi bash: Activity on 467.612 MHz  (5 seconds later)
Jan 10 17:54:21 SprontPi bash: Activity on 462.550 MHz  (11 seconds later)
```

These timestamps represent when **rtl_airband detected activity** (after demodulation and frame assembly). The time you **hear** the audio is **2-5 seconds after** these log entries.

## Optimization Opportunities

### To Reduce Latency:

1. **Lower MP3 Bitrate Further** (currently 16 kbps)
   - Could reduce to 8-12 kbps but may impact audio quality
   - Smaller frames = faster streaming but more noise
   - Estimated savings: 20-50 ms

2. **Reduce Icecast Burst Size**
   - Current: 65536 bytes (65 KB)
   - Could reduce to 8-16 KB for faster initial delivery
   - May cause buffering issues on poor networks
   - Estimated savings: 20-50 ms

3. **Reduce Client Buffer Size**
   - Configuration on player side (VLC, browser, etc.)
   - Browser typically defaults to 2-5 seconds
   - Can tune down to 500ms-1s for LAN
   - Estimated savings: 1000-2000 ms (biggest impact)

4. **Use Lower Sample Rate** (currently 8 kHz)
   - Tower/GMRS don't require high fidelity
   - Already at 8 kHz (telephone quality)
   - Further reduction would significantly impact audio
   - Estimated savings: minimal (already optimized)

5. **Increase Bitrate for Faster Encoding**
   - Current: 16 kbps
   - Increasing to 32 kbps would enable faster frames
   - Would increase network load 2x
   - Estimated savings: 30-100 ms

## Current Configuration Assessment

**Current Settings** (as of Jan 10, 2026):
- ✅ 16 kbps bitrate: **Appropriate for voice/tower comms**
- ✅ 8 kHz sample rate: **Appropriate for band**
- ✅ Icecast queue: **Conservative, ensures reliability**
- ⚠️ Client buffering: **Depends on device/player choice**

**Recommendation**: The current configuration prioritizes **reliability over ultra-low latency**, which is appropriate for a scanner application where occasional 2-3 second delays are acceptable vs. streaming dropouts.

For **interactive use cases**, client-side tuning would have the most impact:
- VLC: Set buffer to 500-1000 ms
- Browser player: Use `<video>` element with minimal buffer
- Mobile apps: Configure stream buffer settings

## Measurement Method

To measure actual latency in your environment:

1. **Enable verbose logging in rtl_airband** (capture exact detection time)
2. **Timestamp when you hear audio** on client device
3. **Calculate delta**: (hear_time - activity_log_time) - (normal processing latency)

Example:
```bash
# Terminal 1: Watch logs
ssh willminkoff@sprontpi.local 'journalctl -u rtl-airband -f'

# Terminal 2: Listen and note when you hear transmission
# vs. when "Activity on X.XXX MHz" appears in Terminal 1

# Measure the delay (typically 1-5 seconds in current setup)
```

## Files Referenced
- Config: `${COMBINED_CONFIG_PATH}` (defaults to `/usr/local/etc/rtl_airband_combined.conf`, bitrate: 16 kbps)
- Icecast: `/etc/icecast2/icecast.xml` (queue-size, burst settings)
- Service: `systemd/rtl-airband.service` (rtl_airband execution)
- Encoder: rtl_airband MP3 encoder (libmp3lame, frame-based)
