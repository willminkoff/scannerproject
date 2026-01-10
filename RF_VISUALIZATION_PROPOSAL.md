# RF Visualization Integration Proposal

## Overview
This document proposes ways to incorporate RF (Radio Frequency) visualization into the SprontPi Scanner Project, enhancing the user interface with real-time frequency activity visualization.

## Current State
The UI currently displays:
- Last hit frequencies (airband & ground)
- Hit history list (time, frequency, duration)
- Gain/Squelch/Filter sliders
- Profile cards and status indicators

**Missing**: Visual representation of RF activity (spectrum, frequency distribution, activity patterns)

## Proposed RF Visualization Options

### Option 1: Frequency Waterfall/Spectrogram (RECOMMENDED)
**What it shows**: Real-time spectrum activity over time, with frequency on Y-axis and time on X-axis.

**Implementation**:
- Uses `Chart.js` (lightweight, ~200 KB) or `Plotly.js` (more features)
- Backend collects recent hits from `read_icecast_hit_list()` + journalctl
- Frontend renders a scrolling waterfall chart where:
  - Y-axis: Frequency range (118-136 MHz for airband, or VHF/UHF for ground)
  - X-axis: Time (last 30-60 minutes)
  - Color intensity: Hit duration/strength
  - Updates every 2-5 seconds with new activity

**Pros**:
- Shows frequency distribution patterns
- Identifies busy vs. quiet times
- Visually compelling
- Helps identify interference patterns

**Cons**:
- Requires more backend data collection
- Higher CPU/memory usage on browser

**Effort**: Moderate (2-4 hours)

---

### Option 2: Frequency Histogram/Activity Bar Chart
**What it shows**: Which frequencies are most active, ranked by total activity.

**Implementation**:
- Backend aggregates hit data from last N hours
- For each frequency in profile, sum total activity (hits + duration)
- Frontend renders horizontal bar chart: Frequency → Activity Level
- Updates every 30 seconds

**Pros**:
- Simple to implement
- Low resource usage
- Quickly shows which channels are busy
- Real-time updates

**Cons**:
- Less information than waterfall
- No time-series data

**Effort**: Minimal (1-2 hours)

---

### Option 3: Frequency Map with Live Dots
**What it shows**: Interactive frequency range with active frequencies highlighted.

**Implementation**:
- Backend: Create frequency bands for each profile (118-136 MHz split into 100 kHz slots)
- Frontend: Render frequency range as clickable grid/map
- Active frequencies light up with color (green=active, yellow=recent, gray=silent)
- Click a frequency to view its details

**Pros**:
- Intuitive for radio operators
- Shows coverage at a glance
- Interactive

**Cons**:
- Requires more UI real estate
- Less useful for wide frequency ranges

**Effort**: Moderate (2-3 hours)

---

### Option 4: Collapsible Spectrum Waterfall (RECOMMENDED HYBRID)
**What it shows**: Live waterfall spectrum visualization that collapses to a small button, taps to expand.

**Implementation**:
- Backend: Provides frequency activity data (already available from hits)
- Frontend: Canvas-based waterfall that scrolls downward
- UI Pattern: Compact header with "▲" indicator; tap to expand/collapse
- Visualization: Frequency on X-axis, time scrolling downward, color intensity = hit strength/duration

**Pros**:
- Professional spectrum-analyzer-like appearance
- Non-intrusive (collapsed by default)
- Uses existing hit data (no rtl-airband mods needed)
- Similar to user's reference image
- Interactive with smooth animations

**Cons**:
- Approximation of real spectrum (based on hits, not raw FFT)
- Still requires new backend endpoint
- Canvas rendering on Pi (manageable)

**Effort**: Moderate-High (4-6 hours)

---

## Recommended Implementation Path

### Single-Phase: Collapsible Spectrum Waterfall (Optimal)
**Architecture**: Compact collapsible panel with spectrum visualization

1. **Backend Endpoint** (`/api/spectrum-data`)
   - Aggregate hit frequencies into 100 kHz bins
   - Return: time-series of frequency activity
   - Format: `{timestamp, freq_bins: [power_0...power_N]}`

2. **Frontend HTML Structure**
   ```html
   <div class="spectrum-panel">
     <div class="spectrum-header">
       <span>▲ Spectrum</span>  <!-- Tappable expand/collapse -->
     </div>
     <div class="spectrum-content" id="spectrum-canvas-container">
       <canvas id="spectrum-waterfall"></canvas>
     </div>
   </div>
   ```

3. **Canvas-based Waterfall Renderer**
   - New file: `ui/static/spectrum-waterfall.js`
   - Renders frequency vs. time scrolling visualization
   - Updates every 2-5 seconds with new row of data
   - Color gradient: dark (quiet) → bright green (active) → red (busy)

4. **Interaction**
   - Tap header to expand/collapse (CSS transition)
   - Show/hide canvas smoothly
   - Persist last state in localStorage
   - Works on touch and desktop

**Time**: 4-6 hours total  
**Value**: Very High - professional appearance + minimal screen footprint

**Phases**:
1. *Phase 1a (2-3h)*: Backend data aggregation + basic canvas rendering
2. *Phase 1b (1-2h)*: Collapsible UI + animations + styling
3. *Phase 1c (1h)*: Testing, color schemes, performance optimization  

---

## Technical Architecture

### Backend Changes (Python)

**New endpoint**: `GET /api/frequency-activity`
```json
{
  "airband": {
    "frequency_histogram": [
      {"freq": "118.100", "hits": 15, "total_duration": 245, "last_hit": "2026-01-10T14:32:10"},
      {"freq": "121.500", "hits": 8, "total_duration": 89, "last_hit": "2026-01-10T14:31:45"}
    ],
    "time_series": [
      {"timestamp": 1704900600, "active_freqs": ["118.100", "121.500"]},
      {"timestamp": 1704900660, "active_freqs": ["118.100"]}
    ]
  },
  "ground": {
    "frequency_histogram": [...],
    "time_series": [...]
  }
}
```

**Implementation location**: `ui/scanner.py`
- Add `aggregate_frequency_activity(unit, hours=1)` function
- Group hits by frequency
- Calculate stats (hit count, duration, last hit time)

---

### Frontend Changes (JavaScript)

**New visualization module**: `ui/static/visualization.js`
```javascript
class FrequencyVisualizer {
  constructor(containerId, target) { /* ... */ }
  update(data) { /* fetch and render */ }
  setTimeWindow(minutes) { /* 15/30/60 */ }
}
```

**Integration points**:
- Load visualization.js in index.html
- Add chart container divs
- Call `visualizer.update()` every 30 seconds
- Switch between airband/ground charts on tab change

---

## Data Sources

| Source | Data | Availability | Pros | Cons |
|--------|------|--------------|------|------|
| Icecast hit log | Time, Frequency, Duration | Realtime | Fresh data | Limited history |
| Journalctl | Time, Frequency (parsed) | On-demand | Full history | Slower, needs parsing |
| Last-hit files | Last frequency | Realtime | Fast | Only latest |
| Icecast metadata | Title string | Realtime | Real-time | Format parsing |

**Recommended**: Combine Icecast hit log + journalctl for optimal balance.

---

## Dependencies

**No external libraries required** - Pure Canvas API for waterfall rendering.

Optional: CSS3 for smooth animations (already available).

---

## Mockup: Collapsible Spectrum Waterfall

**Collapsed State** (default, minimal footprint):
```
┌─────────────────────────────────────┐
│ ▲ Spectrum                          │  ← Tap to expand
└─────────────────────────────────────┘
```

**Expanded State** (slides down to reveal):
```
┌─────────────────────────────────────┐
│ ▼ Spectrum                          │  ← Tap to collapse
├─────────────────────────────────────┤
│ 136 MHz ░░░░░░░░░░░░░░░░░░░░░░░░░░│
│ 130 MHz ░░░░░██░░░░░░░░░░███░░░░░░│
│ 125 MHz ░░░░░░░░░░░██████░░░░░░░░░│ ← Time
│ 120 MHz ░░░██░░░░█████░░░░░░░░░░░░│   scrolls
│ 118 MHz ░░░░░░░░░░░░░░░░░░░░░░░░░░│   downward
│         └─────────────────────────→│
│         ← 60min ago    Now ←        │
│                                     │
├─────────────────────────────────────┤
│ [↺ Reset] [← Hour] [Zoom: 60min]   │
└─────────────────────────────────────┘
```

**Color Scheme** (RTL-SDR style):
- Black background
- Dark gray: Silent/no activity
- Green: Light activity
- Yellow: Medium activity  
- Red: High activity/busy
- Grid overlay for reference

---

## Implementation Checklist

### Phase 1a: Backend (2-3 hours)
- [ ] Add `aggregate_spectrum_data()` to `ui/scanner.py`
  - Group hits into 100 kHz frequency bins
  - Track power/strength per bin per time interval
  - Return last 60 minutes of data
- [ ] Create `/api/spectrum-data` endpoint in `ui/handlers.py`
  - Return JSON with time-series spectrum data
  - Include airband and ground separately
- [ ] Test endpoint with curl/Postman

### Phase 1b: Frontend - HTML/CSS (1-2 hours)
- [ ] Add spectrum panel HTML to `ui/static/index.html`
  - Collapsible header with expand/collapse icon
  - Canvas container
- [ ] Add CSS to `ui/static/style.css`
  - Smooth collapse/expand transition
  - Dark theme styling
  - Responsive sizing
- [ ] Test expand/collapse interaction

### Phase 1c: Frontend - Canvas Renderer (1-2 hours)
- [ ] Create `ui/static/spectrum-waterfall.js`
  - Canvas drawing functions
  - Color mapping (quiet→green→red)
  - Frequency binning visualization
  - Time scrolling animation
- [ ] Integrate into `ui/static/script.js`
  - Fetch `/api/spectrum-data` every 2-5 seconds
  - Render new row on canvas
  - Handle tab switching (airband/ground)
- [ ] Test rendering, performance, color scheme

### Phase 1d: Polish (30-60 min)
- [ ] Add reset/zoom controls
- [ ] Persist collapsed/expanded state
- [ ] Fine-tune colors and grid
- [ ] Mobile/touch testing
- [ ] Performance optimization (canvas size, update frequency)

---

## Ready to Start?

Just confirm and I'll begin implementation with Phase 1a (backend endpoint).
