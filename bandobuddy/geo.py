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


def bng_to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    """A British National Grid reference (as UK registers give positions) to GPS latitude and longitude.
    Ordnance Survey's own formulas: the grid back to OSGB36, then a Helmert shift to WGS84. Good to
    about 5 m, which is as good as the registers themselves."""
    # Airy 1830 ellipsoid and the National Grid's projection
    a, b, f0 = 6377563.396, 6356256.909, 0.9996012717
    lat0, lng0, n0, e0 = math.radians(49), math.radians(-2), -100000.0, 400000.0
    e2 = 1 - b * b / (a * a)
    n = (a - b) / (a + b)

    def meridional(lat: float) -> float:
        d, s = lat - lat0, lat + lat0
        return b * f0 * ((1 + n + 1.25 * n ** 2 + 1.25 * n ** 3) * d
                         - (3 * n + 3 * n ** 2 + 21 / 8 * n ** 3) * math.sin(d) * math.cos(s)
                         + (15 / 8 * n ** 2 + 15 / 8 * n ** 3) * math.sin(2 * d) * math.cos(2 * s)
                         - 35 / 24 * n ** 3 * math.sin(3 * d) * math.cos(3 * s))

    lat, m = lat0, 0.0
    while abs(northing - n0 - m) >= 0.00001:
        lat += (northing - n0 - m) / (a * f0)
        m = meridional(lat)
    sin2 = math.sin(lat) ** 2
    nu = a * f0 / math.sqrt(1 - e2 * sin2)
    rho = a * f0 * (1 - e2) / (1 - e2 * sin2) ** 1.5
    eta2 = nu / rho - 1
    t, sec = math.tan(lat), 1 / math.cos(lat)
    de = easting - e0
    lat_osgb = (lat - t / (2 * rho * nu) * de ** 2
                + t / (24 * rho * nu ** 3) * (5 + 3 * t ** 2 + eta2 - 9 * t ** 2 * eta2) * de ** 4
                - t / (720 * rho * nu ** 5) * (61 + 90 * t ** 2 + 45 * t ** 4) * de ** 6)
    lng_osgb = (lng0 + sec / nu * de
                - sec / (6 * nu ** 3) * (nu / rho + 2 * t ** 2) * de ** 3
                + sec / (120 * nu ** 5) * (5 + 28 * t ** 2 + 24 * t ** 4) * de ** 5
                - sec / (5040 * nu ** 7) * (61 + 662 * t ** 2 + 1320 * t ** 4 + 720 * t ** 6) * de ** 7)

    # OSGB36 to WGS84, through earth-centred coordinates
    nu_a = a / math.sqrt(1 - e2 * math.sin(lat_osgb) ** 2)
    x = nu_a * math.cos(lat_osgb) * math.cos(lng_osgb)
    y = nu_a * math.cos(lat_osgb) * math.sin(lng_osgb)
    z = nu_a * (1 - e2) * math.sin(lat_osgb)
    tx, ty, tz, s = 446.448, -125.157, 542.060, -20.4894e-6
    rx, ry, rz = (math.radians(v / 3600) for v in (0.1502, 0.2470, 0.8421))
    x, y, z = (tx + (1 + s) * x - rz * y + ry * z,
               ty + rz * x + (1 + s) * y - rx * z,
               tz - ry * x + rx * y + (1 + s) * z)
    a2, b2 = 6378137.0, 6356752.3142   # GRS80, as WGS84 uses
    e2w = 1 - b2 * b2 / (a2 * a2)
    p = math.hypot(x, y)
    lat_w = math.atan2(z, p * (1 - e2w))
    for _ in range(10):
        nu_w = a2 / math.sqrt(1 - e2w * math.sin(lat_w) ** 2)
        lat_w = math.atan2(z + e2w * nu_w * math.sin(lat_w), p)
    return math.degrees(lat_w), math.degrees(math.atan2(y, x))


def wgs84_to_bng(lat: float, lng: float) -> tuple[float, float]:
    """GPS latitude and longitude to a British National Grid (easting, northing): the reverse of
    bng_to_wgs84, for finding a place in data laid out on the grid."""
    a2, b2 = 6378137.0, 6356752.3142   # GRS80
    e2w = 1 - b2 * b2 / (a2 * a2)
    phi, lam = math.radians(lat), math.radians(lng)
    nu_w = a2 / math.sqrt(1 - e2w * math.sin(phi) ** 2)
    x = nu_w * math.cos(phi) * math.cos(lam)
    y = nu_w * math.cos(phi) * math.sin(lam)
    z = nu_w * (1 - e2w) * math.sin(phi)
    tx, ty, tz, s = -446.448, 125.157, -542.060, 20.4894e-6      # WGS84 to OSGB36
    rx, ry, rz = (math.radians(v / 3600) for v in (-0.1502, -0.2470, -0.8421))
    x, y, z = (tx + (1 + s) * x - rz * y + ry * z,
               ty + rz * x + (1 + s) * y - rx * z,
               tz - ry * x + rx * y + (1 + s) * z)
    a, b, f0 = 6377563.396, 6356256.909, 0.9996012717                # Airy 1830, the grid's projection
    e2 = 1 - b * b / (a * a)
    p = math.hypot(x, y)
    phi = math.atan2(z, p * (1 - e2))
    for _ in range(10):
        nu = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
        phi = math.atan2(z + e2 * nu * math.sin(phi), p)
    lam = math.atan2(y, x)
    lat0, lng0, n0, e0 = math.radians(49), math.radians(-2), -100000.0, 400000.0
    n = (a - b) / (a + b)
    sin2 = math.sin(phi) ** 2
    nu = a * f0 / math.sqrt(1 - e2 * sin2)
    rho = a * f0 * (1 - e2) / (1 - e2 * sin2) ** 1.5
    eta2 = nu / rho - 1
    d, sm = phi - lat0, phi + lat0
    m = b * f0 * ((1 + n + 1.25 * n ** 2 + 1.25 * n ** 3) * d
                  - (3 * n + 3 * n ** 2 + 21 / 8 * n ** 3) * math.sin(d) * math.cos(sm)
                  + (15 / 8 * n ** 2 + 15 / 8 * n ** 3) * math.sin(2 * d) * math.cos(2 * sm)
                  - 35 / 24 * n ** 3 * math.sin(3 * d) * math.cos(3 * sm))
    c, t = math.cos(phi), math.tan(phi)
    i = m + n0
    ii = nu / 2 * math.sin(phi) * c
    iii = nu / 24 * math.sin(phi) * c ** 3 * (5 - t ** 2 + 9 * eta2)
    iiia = nu / 720 * math.sin(phi) * c ** 5 * (61 - 58 * t ** 2 + t ** 4)
    iv = nu * c
    v = nu / 6 * c ** 3 * (nu / rho - t ** 2)
    vi = nu / 120 * c ** 5 * (5 - 18 * t ** 2 + t ** 4 + 14 * eta2 - 58 * t ** 2 * eta2)
    dl = lam - lng0
    northing = i + ii * dl ** 2 + iii * dl ** 4 + iiia * dl ** 6
    easting = e0 + iv * dl + v * dl ** 3 + vi * dl ** 5
    return easting, northing


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


def bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Initial compass bearing from the first point to the second, 0-360 clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass(degrees: float) -> str:
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[round(degrees / 45) % 8]
