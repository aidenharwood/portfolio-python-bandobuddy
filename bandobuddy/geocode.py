"""Place search via Nominatim (OpenStreetMap's geocoder). Free; be gentle - one search at a time."""
from __future__ import annotations

import threading
import time

import requests

from .config import USER_AGENT

NOMINATIM = "https://nominatim.openstreetmap.org/search"
_lock = threading.Lock()
_last = 0.0


def search(q: str, session: requests.Session) -> dict | None:
    """Best UK match for a town, address, postcode or landmark."""
    global _last
    with _lock:  # Nominatim's policy: at most one request per second
        wait = 1.0 - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.time()
        resp = session.get(NOMINATIM, params={"q": q, "format": "jsonv2", "limit": 1, "countrycodes": "gb"},
                           headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    hits = resp.json()
    if not hits:
        return None
    h = hits[0]
    s, n, w, e = (float(x) for x in h.get("boundingbox") or [h["lat"], h["lat"], h["lon"], h["lon"]])
    return {"name": h.get("display_name", q), "lat": float(h["lat"]), "lng": float(h["lon"]), "bbox": [w, s, e, n]}
