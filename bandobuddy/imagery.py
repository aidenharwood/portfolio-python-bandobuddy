"""Street-level photos from Panoramax - the open (CC-BY-SA), community-run alternative to Street View.

No key needed. Coverage in the UK is patchy but growing; the UI also links out to other viewers.
"""
from __future__ import annotations

import math

import requests

from .config import USER_AGENT
from .geo import haversine_m

PANORAMAX_SEARCH = "https://api.panoramax.xyz/api/search"


def _bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def nearby_photos(lat: float, lng: float, session: requests.Session, radius_m: float = 80, limit: int = 6) -> list[dict]:
    """Photos taken near a site, those pointing at it first, then the closest."""
    dlat = radius_m / 111_320
    dlng = radius_m / (111_320 * math.cos(math.radians(lat)))
    resp = session.get(
        PANORAMAX_SEARCH,
        params={"bbox": f"{lng - dlng:.6f},{lat - dlat:.6f},{lng + dlng:.6f},{lat + dlat:.6f}", "limit": 60},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    resp.raise_for_status()
    photos = []
    for f in resp.json().get("features", []):
        plng, plat = f["geometry"]["coordinates"][:2]
        props = f.get("properties", {})
        assets = f.get("assets", {})
        distance = haversine_m(plat, plng, lat, lng)
        azimuth = props.get("view:azimuth")
        facing = None
        if azimuth is not None and distance > 5:
            diff = abs((_bearing(plat, plng, lat, lng) - azimuth + 180) % 360 - 180)
            facing = diff <= 60
        photos.append({
            "id": f["id"],
            "thumb": (assets.get("thumb") or {}).get("href"),
            "image": (assets.get("sd") or assets.get("hd") or {}).get("href"),
            "taken": (props.get("datetime") or "")[:10],
            "distance_m": round(distance),
            "facing": facing,
            "producer": props.get("geovisio:producer") or "",
            "license": props.get("license") or "",
        })
    photos.sort(key=lambda p: (p["facing"] is False, p["distance_m"]))
    return [p for p in photos if p["thumb"]][:limit]
