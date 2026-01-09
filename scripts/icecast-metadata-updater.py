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
from urllib.parse import quote

# Configuration
UI_API_URL = "http://127.0.0.1:5050/api/status"
ICECAST_HOST = "127.0.0.1"
ICECAST_PORT = 8000
ICECAST_MOUNT = "GND.mp3"
ICECAST_ADMIN_PASSWORD = "hackme"  # Admin password from icecast.xml
UPDATE_INTERVAL = 2  # seconds

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("icecast-metadata-updater")


def get_current_status():
    """Fetch current status from UI API"""
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
        # Use static metadata that doesn't create feedback loop
        # Just show scanner status, not actual frequencies
        artist = "SprontPi Scanner"
        title = "Live Ground/Airband Mix"
        
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
    
    last_metadata = None
    
    while True:
        try:
            # Fetch current status
            status = get_current_status()
            
            if status:
                # Format metadata
                title, artist = format_metadata(status)
                
                # Only update if changed
                current_metadata = (title, artist)
                if current_metadata != last_metadata and title:
                    if update_icecast_metadata(title, artist):
                        last_metadata = current_metadata
            
            time.sleep(UPDATE_INTERVAL)
            
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            sys.exit(0)
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            time.sleep(UPDATE_INTERVAL)


if __name__ == "__main__":
    main()
