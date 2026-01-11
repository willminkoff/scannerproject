# Command Palette UI Mockup

## 3. "Touch Command Palette" - Complete Redesign

This mockup shows a minimalist operator interface built around a **floating command palette** triggered by ⌘K or the floating ⌘ button. The main screen becomes a fullscreen **Live View** optimized for at-a-glance monitoring.

---

## State 1: Default Live View (No Palette Open)

```
┌─────────────────────────────────────────────┐
│  📡 Frequency Spectrum (121.5-137.0 MHz)   │
├─────────────────────────────────────────────┤
│                                             │
│  █████████░░░░░░░░░ ████░░░░ 134.2 +18dB  │
│  ████████░░░░░░░░░░ ░░░░░░░░ 125.8 +16dB  │
│  ███████░░░░░░░░░░░ ███░░░░░ 133.5 +15dB  │
│  ██████░░░░░░░░░░░░ ░░░░░░░░ 121.8 +12dB  │
│  █████░░░░░░░░░░░░░ ██░░░░░░ 132.1 +14dB  │
│  ████░░░░░░░░░░░░░░ ░░░░░░░░ 128.5 +11dB  │
│                    WATERFALL SPECTRUM      │
│              (Live signal visualization)   │
│                                             │
│  ┌─────────────────┬─────────────────────┐ │
│  │  📊 AIRBAND     │   📊 GROUND         │ │
│  │  247 HITS       │   89 HITS           │ │
│  │  ↑ 12/hr        │   ↑ 4/hr            │ │
│  └─────────────────┴─────────────────────┘ │
│                                             │
│  ┌────────┬────────┬────────┬────────────┐ │
│  │  ▶     │  ⚙     │  ★     │   📋      │ │
│  │ PLAY   │ CONFIG │ FAVS   │   LOGS    │ │
│  └────────┴────────┴────────┴────────────┘ │
│                                             │
│  Recent Hits:                               │
│  14:23:18 │ 134.225 MHz ████░ │ 2.3s      │
│  14:22:45 │ 127.850 MHz ███░░ │ 1.8s      │
│                                             │
│                                        ┌─┐ │
│                                        │⌘│ │◄─ Floating button
│                                        └─┘ │   (tap or ⌘K)
└─────────────────────────────────────────────┘
```

---

## State 2: Command Palette Open

```
┌─────────────────────────────────────────────┐
│                                             │
│  ▓▓▓▓▓▓▓▓▓▓ Blur behind ▓▓▓▓▓▓▓▓▓▓▓▓▓     │
│  ▓                                         ▓ │
│  ▓    ┌─────────────────────────────────┐ ▓ │
│  ▓    │ 🔍 Search commands, profiles    │ ▓ │
│  ▓    └─────────────────────────────────┘ ▓ │
│  ▓                                         ▓ │
│  ▓    🎯 Switch to Airband Profile   ⏎   ▓ │
│  ▓       Load airband scanner settings      ▓ │
│  ▓                                         ▓ │
│  ▓    🎯 Switch to Ground Profile    ↓   ▓ │
│  ▓       Load ground scanner settings       ▓ │
│  ▓                                         ▓ │
│  ▓    ⚙  Increase Gain          +        ▓ │
│  ▓       +2dB to current scanner           ▓ │
│  ▓                                         ▓ │
│  ▓    ⚙  Decrease Gain          -        ▓ │
│  ▓       -2dB to current scanner           ▓ │
│  ▓                                         ▓ │
│  ▓    🔇 Open Squelch          Space     ▓ │
│  ▓       Open squelch for 2 seconds        ▓ │
│  ▓                                         ▓ │
│  ▓    🔄 Restart Scanner        R        ▓ │
│  ▓       Restart current scanner service   ▓ │
│  ▓                                         ▓ │
│  ▓    🚫 Clear Avoids           C        ▓ │
│  ▓       Clear all frequency avoidances    ▓ │
│  ▓                                         ▓ │
│  ▓    📊 Show Statistics        ?        ▓ │
│  ▓       Display session statistics        ▓ │
│  ▓    ┌─────────────────────────────────┐ ▓ │
│  ▓▓▓▓▓│  (Click outside or ESC to close)│▓▓ │
│  └─────└─────────────────────────────────┘─────┘
```

---

## State 3: Search Filtering

```
┌─────────────────────────────────────────────┐
│                                             │
│  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ │
│  ▓                                         ▓ │
│  ▓    ┌─────────────────────────────────┐ ▓ │
│  ▓    │ 🔍 "gain"                       │ ▓ │
│  ▓    └─────────────────────────────────┘ ▓ │
│  ▓                                         ▓ │
│  ▓    ⚙  Increase Gain          +        ▓ │
│  ▓       +2dB to current scanner           ▓ │
│  ▓                                         ▓ │
│  ▓    ⚙  Decrease Gain          -        ▓ │
│  ▓       -2dB to current scanner           ▓ │
│  ▓                                         ▓ │
│  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ │
│                                             │
│  (Live view remains blurred in background) │
└─────────────────────────────────────────────┘
```

---

## Key Features

### Live View (Always Visible)
- **Waterfall Visualization**: Real-time ASCII-art spectrum showing signal activity
- **Large Hit Counters**: Prominent displays with trend indicators (↑/↓ per hour)
- **Quick Access Bar**: 4 buttons for most-used actions (Play, Config, Favorites, Logs)
- **Hit Stream**: Latest 2-3 hits with frequency, time, duration at a glance
- **Minimal Text**: Everything is at-a-glance, no scrolling needed

### Command Palette
- **Universal Search**: Type to find profiles, commands, or settings (e.g., "gain", "airband", "restart")
- **Keyboard-First**: Arrow keys to navigate, Enter/Space to execute
- **Keyboard Shortcuts**: Show hints for power users (⌘, +, -, Space, R, C, ?)
- **Fuzzy Matching**: "squelch" finds "Open Squelch" and other related commands
- **Recent Commands**: Most-used actions stay at top

### Interactions
- Press **⌘K** (Cmd+K) or **Ctrl+K** to toggle palette
- Click floating **⌘** button in bottom-right
- **Arrow keys** to navigate command list
- **Enter** to execute
- **Escape** to close
- Click outside to close

---

## Why This Design Works

✅ **Mobile-First**: Fullscreen waterfall perfect for portrait  
✅ **Distraction-Free**: Main view is minimal until you search  
✅ **Power Users**: Keyboard shortcuts, search, recent history  
✅ **Touch-Friendly**: Large buttons, no hover states required  
✅ **Real-Time Data**: Spectrum and hit counts always visible  
✅ **Scalable**: Easy to add commands without cluttering UI  
✅ **Accessibility**: Search makes all controls discoverable  

---

## Implementation Notes

### New Components
- `WaterfallCanvas` or ASCII art renderer for spectrum
- `CommandPalette` component with search & filtering
- `HitStreamWidget` for real-time updates
- Keyboard event handler for global shortcuts

### Reusable from Current UI
- Range slider controls (moved to palette when needed)
- Profile selector logic
- Live hit data feed
- HTTP endpoints for actions

### CSS Framework
- Keep dark theme (existing palette)
- Add glass-morphism blur effects
- Smooth animations for palette open/close
- Touch-optimized button sizing
