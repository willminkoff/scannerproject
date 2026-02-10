"""Icecast stream monitoring and metadata."""
import json
import urllib.request
from urllib.parse import urlparse

try:
    from .config import ICECAST_STATUS_URL, ICECAST_MOUNT_PATH, UNITS
    from .systemd import unit_active
except ImportError:
    from ui.config import ICECAST_STATUS_URL, ICECAST_MOUNT_PATH, UNITS
    from ui.systemd import unit_active


def icecast_up() -> bool:
    """Check if Icecast is running."""
    return unit_active(UNITS["icecast"])


def fetch_local_icecast_status() -> str:
    """Fetch Icecast status JSON from localhost."""
    try:
        with urllib.request.urlopen(ICECAST_STATUS_URL, timeout=5) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return f"ERROR: {e}"


def _normalize_icecast_title(value) -> str:
    """Normalize an Icecast title value to string."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{value}".strip()
    if isinstance(value, str):
        return value.strip()
    return ""


def extract_icecast_title(status_text: str) -> str:
    """Extract the current stream title from Icecast status JSON."""
    try:
        data = json.loads(status_text)
    except json.JSONDecodeError:
        return ""
    sources = data.get("icestats", {}).get("source")
    if not sources:
        return ""
    if not isinstance(sources, list):
        sources = [sources]
    for source in sources:
        listenurl = (source.get("listenurl") or "")
        mount = (source.get("mount") or "")
        if listenurl.endswith(ICECAST_MOUNT_PATH) or mount == ICECAST_MOUNT_PATH:
            for key in ("title", "streamtitle", "yp_currently_playing"):
                value = _normalize_icecast_title(source.get(key))
                if value:
                    return value
    return ""


def read_last_hit_from_icecast() -> str:
    """Read the last hit frequency from Icecast stream title."""
    try:
        with urllib.request.urlopen(ICECAST_STATUS_URL, timeout=1.5) as resp:
            status_text = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""
    return extract_icecast_title(status_text)


def parse_icecast_sources(status_text: str) -> list:
    """Parse Icecast status JSON into a list of sources."""
    try:
        data = json.loads(status_text)
    except json.JSONDecodeError:
        return []
    sources = data.get("icestats", {}).get("source")
    if not sources:
        return []
    if not isinstance(sources, list):
        sources = [sources]
    results = []
    for source in sources:
        listenurl = (source.get("listenurl") or "").strip()
        mount = (source.get("mount") or "").strip()
        if not mount and listenurl:
            try:
                mount = urlparse(listenurl).path or ""
            except Exception:
                mount = ""
        listeners = source.get("listeners")
        try:
            listeners = int(listeners)
        except Exception:
            listeners = 0
        results.append({
            "mount": mount,
            "listenurl": listenurl,
            "listeners": listeners,
        })
    return results


def list_icecast_mounts(status_text: str) -> list:
    """Return a list of mounts from Icecast status JSON."""
    mounts = []
    for source in parse_icecast_sources(status_text):
        mount = source.get("mount") or ""
        if mount:
            mounts.append(mount)
    return mounts
