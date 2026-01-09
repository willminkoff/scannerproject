#!/usr/bin/env python3
"""
Icecast Metadata Updater for SprontPi
Updates Icecast metadata with current frequency/mode from the UI
"""

import requests
import json
import time
import sys
import logging
import re
from urllib.parse import quote

# Configuration
UI_API_URL = "http://127.0.0.1:5050/api/status"
ICECAST_STATUS_URL = "http://127.0.0.1:8000/status-json.xsl"
ICECAST_HOST = "127.0.0.1"
ICECAST_PORT = 8000
ICECAST_MOUNT = "GND.mp3"
ICECAST_ADMIN_PASSWORD = "hackme"  # Admin password from icecast.xml
UPDATE_INTERVAL = 5  # seconds - only update every 5 seconds to avoid feedback

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("icecast-metadata-updater")


def get_icecast_current_frequency():
    """Extract frequency from Icecast stream metadata"""
    try:
        response = requests.get(ICECAST_STATUS_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        # Find the GND.mp3 source
        sources = data.get("icestats", {}).get("source", [])
        if not isinstance(sources, list):
            sources = [sources] if sources else []
        
        for source in sources:
            if source.get("mount") == f"/{ICECAST_MOUNT}":
                title = source.get("title", "")
                if title:
                    return title
        
        return None
    except Exception as e:
        logger.error(f"Failed to fetch Icecast status: {e}")
        return None


def get_current_status():
    """Fetch current status from Icecast - this is now the primary source"""
    freq = get_icecast_current_frequency()
    if freq:
        return {"frequency": freq}
    # Fall back to UI API if Icecast is unavailable
    try:
        response = requests.get(UI_API_URL, timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch UI status: {e}")
        return None


def format_metadata(status):
    """Format status into Icecast metadata strings"""
    if not status:
        return None, None
    
    try:
        # If we have a frequency directly from Icecast, rtl_airband already has it
        # so we just pass it through
        if "frequency" in status:
            frequency = status.get("frequency", "")
            if frequency:
                return frequency, "SprontPi Scanner"
        
        # Fall back to UI API response format (has last_hit_airband and last_hit_ground)
        airband_freq = status.get("last_hit_airband", "Unknown")
        ground_freq = status.get("last_hit_ground", "Unknown")
        
        # Prefer whichever has a valid frequency (not "Unknown")
        if airband_freq != "Unknown":
            frequency = airband_freq
            profile = status.get("profile_airband", "AIRBAND").upper()
        elif ground_freq != "Unknown":
            frequency = ground_freq
            profile = status.get("profile_ground", "GROUND").upper()
        else:
            frequency = "Scanning"
            profile = "SCANNER"
        
        # Format: "Frequency - Profile"
        title = f"{frequency} - {profile}"
        artist = "SprontPi Scanner"
        
        return title, artist
    except (KeyError, TypeError) as e:
        logger.error(f"Error formatting metadata: {e}")
        return None, None


def update_icecast_metadata(title, artist):
    """Update Icecast metadata via admin API"""
    try:
        # Icecast admin API endpoint
        url = f"http://{ICECAST_HOST}:{ICECAST_PORT}/admin/metadata"
        
        params = {
            "mount": f"/{ICECAST_MOUNT}",
            "mode": "updinfo",
            "song": f"{title} - {artist}"
        }
        
        auth = ("admin", ICECAST_ADMIN_PASSWORD)
        
        response = requests.get(url, params=params, auth=auth, timeout=5)
        
        if response.status_code == 200:
            logger.info(f"Metadata updated: {title} - {artist}")
            return True
        else:
            logger.warning(f"Icecast returned status {response.status_code}")
            return False
            
    except requests.RequestException as e:
        logger.error(f"Failed to update Icecast metadata: {e}")
        return False


def main():
    """Main loop"""
    logger.info("Starting Icecast Metadata Updater")
    logger.info(f"UI API: {UI_API_URL}")
    logger.info(f"Icecast: {ICECAST_HOST}:{ICECAST_PORT}/{ICECAST_MOUNT}")
    
    loop_count = 0
    while True:
        try:
            loop_count += 1
            logger.debug(f"Loop iteration {loop_count} starting")
            
            # Fetch current status
            status = get_current_status()
            
            if status:
                # Format metadata
                title, artist = format_metadata(status)
                
                # Always update (don't check if changed)
                if title:
                    update_icecast_metadata(title, artist)
            else:
                logger.debug("No status received from API")
            
            logger.debug(f"Sleeping for {UPDATE_INTERVAL} seconds")
            time.sleep(UPDATE_INTERVAL)
            
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            sys.exit(0)
        except Exception as e:
            logger.error(f"Unexpected error in loop iteration {loop_count}: {e}", exc_info=True)
            time.sleep(UPDATE_INTERVAL)


if __name__ == "__main__":
    main()
