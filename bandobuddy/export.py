"""Export sites as CSV (Google My Maps, spreadsheets), KML (Google Earth, Organic Maps) or GPX (any GPS app)."""
from __future__ import annotations

import csv
import io
from html import escape


def links(site: dict) -> dict[str, str]:
    lat, lng = site["lat"], site["lng"]
    out = {
        "openstreetmap": f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lng:.6f}#map=18/{lat:.6f}/{lng:.6f}",
        "streetview": f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat:.6f},{lng:.6f}",
        "mapillary": f"https://www.mapillary.com/app/?lat={lat:.6f}&lng={lng:.6f}&z=18",
    }
    detail = site.get("detail") or {}
    osm = detail.get("osm") or []
    if osm:
        out["osm_element"] = f"https://www.openstreetmap.org/{osm[0]['osm_id']}"
    wd = detail.get("wikidata") or []
    wiki = next((w["wikipedia_url"] for w in wd if w.get("wikipedia_url")), None)
    if wiki:
        out["wikipedia"] = wiki
    if wd:
        out["wikidata"] = wd[0]["url"]
    return out


def to_csv(sites: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "score", "tier", "category", "latitude", "longitude", "sources", "reasons",
                "openstreetmap", "wikipedia", "added"])
    for s in sites:
        lk = links(s)
        w.writerow([s["name"], s["score"], s["tier"], s["category"], f"{s['lat']:.6f}", f"{s['lng']:.6f}",
                    s["sources"], " | ".join(s["reasons"]), lk.get("osm_element", lk["openstreetmap"]),
                    lk.get("wikipedia", ""), s.get("added") or ""])
    return buf.getvalue()


def to_kml(sites: list[dict], title: str = "bandobuddy") -> str:
    marks = []
    for s in sites:
        desc = "<br>".join(escape(r) for r in s["reasons"])
        marks.append(
            f"<Placemark><name>{escape(s['name'])} [{s['score']}]</name>"
            f"<description><![CDATA[{desc}]]></description>"
            f"<Point><coordinates>{s['lng']:.6f},{s['lat']:.6f},0</coordinates></Point></Placemark>"
        )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            f"<name>{escape(title)}</name>" + "".join(marks) + "</Document></kml>\n")


def to_gpx(sites: list[dict], title: str = "bandobuddy") -> str:
    pts = []
    for s in sites:
        pts.append(
            f'<wpt lat="{s["lat"]:.6f}" lon="{s["lng"]:.6f}"><name>{escape(s["name"])} [{s["score"]}]</name>'
            f"<desc>{escape(' | '.join(s['reasons']))}</desc><type>{escape(s['category'])}</type></wpt>"
        )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="bandobuddy" xmlns="http://www.topografix.com/GPX/1/1">'
            f"<metadata><name>{escape(title)}</name></metadata>" + "".join(pts) + "</gpx>\n")


FORMATS = {
    "csv": (to_csv, "text/csv; charset=utf-8"),
    "kml": (to_kml, "application/vnd.google-earth.kml+xml"),
    "gpx": (to_gpx, "application/gpx+xml"),
}
