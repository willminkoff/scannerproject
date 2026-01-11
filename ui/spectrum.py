"""Real-time spectrum data collection and streaming.

This module provides spectrum analysis using rtl_airband stats_filepath
or simulated data for the SB3 UI spectrum widget.
"""
import os
import re
import threading
import time
import json
import random
import math
from typing import Optional, List, Dict, Callable
from collections import deque

# Stats file path (written by rtl_airband every 15 seconds)
STATS_FILE_PATH = os.getenv("RTL_AIRBAND_STATS_PATH", "/run/rtl_airband_stats.txt")

# Prometheus metric patterns
RE_NOISE_FLOOR = re.compile(r'rtl_airband_freq_noise_floor\{freq="([0-9.]+)"\}\s+(-?[0-9.]+)')
RE_ACTIVE_COUNTER = re.compile(r'rtl_airband_freq_active_counter\{freq="([0-9.]+)"\}\s+([0-9]+)')

# Spectrum configuration
SPECTRUM_CONFIG = {
    "airband": {
        "freq_start": 118.0,  # MHz
        "freq_end": 137.0,    # MHz
    },
    "ground": {
        "freq_start": 150.0,
        "freq_end": 175.0,
    },
}

# Global state
_spectrum_data: Dict[str, List[Dict]] = {}
_spectrum_history: Dict[str, deque] = {}
_spectrum_lock = threading.Lock()
_spectrum_thread: Optional[threading.Thread] = None
_spectrum_running = False
_last_stats_mtime = 0.0
_last_active_counters: Dict[str, int] = {}


def parse_stats_file(filepath: str = STATS_FILE_PATH) -> Dict[str, Dict]:
    """Parse rtl_airband stats file in Prometheus format.
    
    Returns dict of {freq: {noise_floor: dB, active_counter: int, active_delta: int}}
    """
    global _last_active_counters
    
    result = {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    
    # Parse noise floor values
    for match in RE_NOISE_FLOOR.finditer(content):
        freq = match.group(1)
        noise = float(match.group(2))
        if freq not in result:
            result[freq] = {}
        result[freq]['noise_floor'] = noise
        result[freq]['freq'] = float(freq)
    
    # Parse active counters
    for match in RE_ACTIVE_COUNTER.finditer(content):
        freq = match.group(1)
        count = int(match.group(2))
        if freq not in result:
            result[freq] = {'freq': float(freq)}
        result[freq]['active_counter'] = count
        
        # Calculate delta since last read
        prev = _last_active_counters.get(freq, count)
        result[freq]['active_delta'] = count - prev
        _last_active_counters[freq] = count
    
    return result


def get_spectrum_bins(band: str = "airband") -> List[Dict]:
    """Get current spectrum data for a band.
    
    Returns list of {freq: MHz, power: dB, active: bool} dicts.
    """
    with _spectrum_lock:
        if band in _spectrum_data:
            return list(_spectrum_data[band])
    return []


def get_spectrum_history(band: str = "airband", depth: int = 50) -> List[List[float]]:
    """Get spectrum history for waterfall display.
    
    Returns list of power arrays, most recent first.
    """
    with _spectrum_lock:
        if band in _spectrum_history:
            history = list(_spectrum_history[band])
            history.reverse()
            return history[:depth]
    return []


def _check_stats_file() -> bool:
    """Check if stats file has been updated."""
    global _last_stats_mtime
    try:
        mtime = os.path.getmtime(STATS_FILE_PATH)
        if mtime > _last_stats_mtime:
            _last_stats_mtime = mtime
            return True
    except FileNotFoundError:
        pass
    return False


def _run_stats_monitor() -> None:
    """Monitor rtl_airband stats file for updates."""
    global _spectrum_running
    
    while _spectrum_running:
        if _check_stats_file():
            stats = parse_stats_file()
            if stats:
                _update_spectrum_from_stats(stats)
        time.sleep(1)  # Check every second


def _update_spectrum_from_stats(stats: Dict[str, Dict]) -> None:
    """Update spectrum data from parsed stats."""
    # Convert to bin list sorted by frequency
    bins = []
    for freq_str, data in stats.items():
        bins.append({
            'freq': data.get('freq', float(freq_str)),
            'power': data.get('noise_floor', -95),
            'active': data.get('active_delta', 0) > 0,
            'count': data.get('active_counter', 0),
        })
    
    bins.sort(key=lambda x: x['freq'])
    
    with _spectrum_lock:
        _spectrum_data['airband'] = bins
        
        # Update history for waterfall
        if 'airband' not in _spectrum_history:
            _spectrum_history['airband'] = deque(maxlen=100)
        _spectrum_history['airband'].append([b['power'] for b in bins])


def _run_simulated_spectrum(band: str) -> None:
    """Generate simulated spectrum data for development/demo."""
    global _spectrum_running
    
    config = SPECTRUM_CONFIG.get(band, SPECTRUM_CONFIG["airband"])
    
    # Simulated frequencies matching your airband profile
    sim_freqs = [118.4, 118.6, 119.35, 119.45, 121.9, 124.75, 126.05, 
                 127.175, 128.825, 129.95, 130.125, 130.725, 131.375, 131.45, 135.1]
    
    sim_labels = ["BNA Dep East", "BNA Tower", "BNA Dep West", "BNA App", "BNA Gnd", 
                  "BNA Final", "BNA Clnc Del", "BNA App 127.175", "United Ops", 
                  "Ramp 129.95", "SWA Ops", "Frontier Ops", "Ramp 131.37", "Delta Ops", "BNA ATIS"]
    
    noise_floor = -95
    active_counters = {f: 0 for f in sim_freqs}
    
    while _spectrum_running:
        bins = []
        
        for i, freq in enumerate(sim_freqs):
            # Base noise floor with variation
            power = noise_floor + random.gauss(0, 2)
            active = False
            
            # Simulate random activity (some freqs more active)
            activity_chance = 0.15 if freq in [118.6, 121.9, 127.175] else 0.05
            if random.random() < activity_chance:
                power = noise_floor + random.uniform(15, 35)
                active = True
                active_counters[freq] += 1
            
            bins.append({
                'freq': freq,
                'power': round(power, 1),
                'active': active,
                'count': active_counters[freq],
                'label': sim_labels[i] if i < len(sim_labels) else '',
            })
        
        with _spectrum_lock:
            _spectrum_data[band] = bins
            
            if band not in _spectrum_history:
                _spectrum_history[band] = deque(maxlen=100)
            _spectrum_history[band].append([b['power'] for b in bins])
        
        time.sleep(1)  # Update every second (simulates 15s stats but faster for demo)


def start_spectrum(band: str = "airband", device_serial: Optional[str] = None, simulate: bool = False) -> None:
    """Start spectrum collection for a band.
    
    Args:
        band: Band name (airband, ground)
        device_serial: RTL-SDR device serial (unused, for API compat)
        simulate: If True, use simulated data
    """
    global _spectrum_running, _spectrum_thread
    
    if _spectrum_running:
        return
    
    _spectrum_running = True
    
    # Check if stats file exists
    stats_available = os.path.exists(STATS_FILE_PATH)
    
    if simulate or not stats_available:
        _spectrum_thread = threading.Thread(
            target=_run_simulated_spectrum,
            args=(band,),
            daemon=True
        )
    else:
        _spectrum_thread = threading.Thread(
            target=_run_stats_monitor,
            daemon=True
        )
    
    _spectrum_thread.start()


def stop_spectrum() -> None:
    """Stop spectrum collection."""
    global _spectrum_running
    _spectrum_running = False


def spectrum_to_json(band: str = "airband") -> str:
    """Get current spectrum as JSON string."""
    bins = get_spectrum_bins(band)
    return json.dumps({
        "band": band,
        "timestamp": time.time(),
        "bins": bins,
        "source": "stats_file" if os.path.exists(STATS_FILE_PATH) else "simulation"
    })


# For testing
if __name__ == "__main__":
    print(f"Looking for stats at: {STATS_FILE_PATH}")
    
    # Try to read real stats first
    stats = parse_stats_file()
    if stats:
        print(f"Found {len(stats)} frequencies in stats file:")
        for freq, data in sorted(stats.items(), key=lambda x: float(x[0])):
            print(f"  {freq} MHz: noise={data.get('noise_floor', 'N/A')} dB, count={data.get('active_counter', 0)}")
    else:
        print("No stats file found. Waiting for real stats...")
        # start_spectrum("airband", simulate=False)
        
        try:
            while True:
                time.sleep(2)
                bins = get_spectrum_bins("airband")
                if bins:
                    active = [b for b in bins if b.get('active')]
                    print(f"Bins: {len(bins)}, Active: {len(active)}")
        except KeyboardInterrupt:
            stop_spectrum()
