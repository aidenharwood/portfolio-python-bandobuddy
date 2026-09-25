"""Getting there: rights of way, paths, parking and private access around a place, from OpenStreetMap.

That's the whole path and road network, far too much to keep, so it's asked of the Overpass API
for a few hundred metres around one place, when someone opens it, one request at a time. The web
app caches the answers, which keeps it well inside Overpass's usage policy.
"""
from __future__ import annotations

import math
import threading

import requests

from .config import USER_AGENT
from .geo import bearing_deg, compass, haversine_m

OVERPASS = "https://overpass-api.de/api/interpreter"
PATH_M = 400
PARKING_M = 800
PRIVATE_M = 50
_lock = threading.Lock()

# Legally public routes, in England and Wales (definitive map) and Scotland (core paths).
RIGHTS_OF_WAY = {
    "public_footpath": "Public footpath",
    "public_bridleway": "Public bridleway",
    "restricted_byway": "Restricted byway",
    "byway_open_to_all_traffic": "Byway",
    "core_path": "Core path",
}
PATH_HIGHWAYS = ("footway", "path", "bridleway", "track", "steps", "cycleway")


def query(lat: float, lng: float) -> str:
    return f"""[out:json][timeout:20];
(
  way(around:{PATH_M},{lat},{lng})["highway"~"^({'|'.join(PATH_HIGHWAYS)})$"];
  way(around:{PATH_M},{lat},{lng})["designation"];
  nwr(around:{PARKING_M},{lat},{lng})["amenity"="parking"];
  nwr(around:{PRIVATE_M},{lat},{lng})["access"~"^(private|no)$"];
  nwr(around:{PRIVATE_M},{lat},{lng})["foot"~"^(private|no)$"];
);
out tags geom qt 150;"""


def _nearest_point(lat: float, lng: float, element: dict) -> tuple[float, float] | None:
    """The nearest spot on a way (by projecting onto each segment), or a node's own position."""
    if "lat" in element:
        return element["lat"], element["lon"]
    points = [(g["lat"], g["lon"]) for g in element.get("geometry") or [] if g]
    if not points:
        c = element.get("center")
        return (c["lat"], c["lon"]) if c else None
    if len(points) == 1:
        return points[0]
    k = math.cos(math.radians(lat))  # metres-ish in a small area: stretch longitude by cos(lat)
    best, best_d = points[0], math.inf
    for (a_lat, a_lng), (b_lat, b_lng) in zip(points, points[1:]):
        ax, ay, bx, by = a_lng * k, a_lat, b_lng * k, b_lat
        px, py = lng * k, lat
        dx, dy = bx - ax, by - ay
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        x, y = ax + t * dx, ay + t * dy
        d = (x - px) ** 2 + (y - py) ** 2
        if d < best_d:
            best, best_d = (y, x / k), d
    return best


def _where(lat: float, lng: float, element: dict) -> dict | None:
    point = _nearest_point(lat, lng, element)
    if not point:
        return None
    return {"distance_m": round(haversine_m(lat, lng, *point)),
            "direction": compass(bearing_deg(lat, lng, *point)), "lat": point[0], "lng": point[1]}


def _closed(tags: dict) -> bool:
    return tags.get("access") in ("private", "no") or tags.get("foot") in ("private", "no")


def _private_thing(tags: dict) -> str:
    if tags.get("barrier"):
        return f"a {tags['barrier'].replace('_', ' ')}"
    if tags.get("highway"):
        return f"a {tags['highway'].replace('_', ' ')}"
    if tags.get("landuse") or tags.get("leisure"):
        return f"{(tags.get('landuse') or tags.get('leisure')).replace('_', ' ')} land"
    if tags.get("amenity"):
        return f"a {tags['amenity'].replace('_', ' ')}"
    return "something"


def summarise(lat: float, lng: float, elements: list[dict]) -> dict:
    """Nearest public right of way, nearest other path, nearest public parking, and what's mapped as
    private right next to the place."""
    rights, paths, parking, private = [], [], [], []
    for el in elements:
        tags = el.get("tags") or {}
        where = _where(lat, lng, el)
        if not where:
            continue
        if tags.get("amenity") == "parking":
            if not _closed(tags) and tags.get("access") != "customers":
                parking.append({**where, "name": tags.get("name"), "fee": tags.get("fee") == "yes"})
            continue
        if tags.get("highway") in PATH_HIGHWAYS or tags.get("designation"):
            if _closed(tags):
                if where["distance_m"] <= PRIVATE_M:
                    private.append((where["distance_m"], _private_thing(tags)))
                continue
            kind = RIGHTS_OF_WAY.get(tags.get("designation", ""))
            if kind:
                rights.append({**where, "kind": kind, "ref": tags.get("prow_ref") or tags.get("ref")})
            elif tags.get("highway") in PATH_HIGHWAYS:
                allowed = tags.get("foot") in ("yes", "designated", "permissive")
                label = {"track": "Track", "steps": "Steps", "bridleway": "Bridleway", "cycleway": "Cycle path"}.get(
                    tags["highway"], "Path")
                paths.append({**where, "kind": label, "permissive": tags.get("foot") == "permissive",
                              "foot_allowed": allowed})
            continue
        if _closed(tags) and where["distance_m"] <= PRIVATE_M:
            private.append((where["distance_m"], _private_thing(tags)))

    def nearest(items):
        return min(items, key=lambda i: i["distance_m"]) if items else None

    right = nearest(rights)
    path = nearest(paths)
    if right and path and path["distance_m"] >= right["distance_m"]:
        path = None  # the right of way is at least as close: that's the one to mention
    seen, private_things = set(), []
    for _, thing in sorted(private):
        if thing not in seen:
            seen.add(thing)
            private_things.append(thing)
    return {"right_of_way": right, "path": path, "parking": nearest(parking), "private": private_things[:3]}


def around(lat: float, lng: float, session: requests.Session) -> dict:
    with _lock:  # one question at a time, however many people are looking
        resp = session.post(OVERPASS, data={"data": query(lat, lng)}, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return summarise(lat, lng, resp.json().get("elements") or [])
