"""Small geodesy helpers."""
from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def offset(lat: float, lng: float, north_m: float, east_m: float) -> tuple[float, float]:
    """Shift a point by metres north/east (flat-earth approximation, fine < ~50 km)."""
    dlat = north_m / EARTH_RADIUS_M
    dlng = east_m / (EARTH_RADIUS_M * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lng + math.degrees(dlng)


def grid_boxes(bbox: tuple[float, float, float, float], step: float) -> list[tuple[float, float, float, float]]:
    """Cover (south, west, north, east) with step x step degree boxes."""
    s0, w0, n0, e0 = bbox
    # Count boxes with integers: repeatedly adding a float step drifts and leaves zero-width slivers.
    rows = math.ceil((n0 - s0) / step - 1e-9)
    cols = math.ceil((e0 - w0) / step - 1e-9)
    boxes = []
    for i in range(rows):
        s, n = s0 + i * step, min(s0 + (i + 1) * step, n0)
        for j in range(cols):
            w, e = w0 + j * step, min(w0 + (j + 1) * step, e0)
            boxes.append((round(s, 6), round(w, 6), round(n, 6), round(e, 6)))
    return boxes
