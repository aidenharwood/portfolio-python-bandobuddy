"""Export sites as CSV (Google My Maps, spreadsheets), KML (Google Earth, Organic Maps) or GPX (any GPS app)."""
from __future__ import annotations

import csv
import io
from html import escape

from .config import CATEGORIES, OTHER_CATEGORY
from .importer import SOURCE as IMPORTED
from .opendata import DATASETS

_LABELS = {k: label for k, label, _ in CATEGORIES} | {OTHER_CATEGORY[0]: OTHER_CATEGORY[1]}


def category_label(key: str) -> str:
    return _LABELS.get(key, key)


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
    registers = [{"label": _register_label(e["source"]), "url": e["url"]}
                 for e in (detail.get("open") or []) if e.get("url")]
    if registers:
        out["registers"] = registers
    return out


def _register_label(source: str) -> str:
    if source in DATASETS:
        return DATASETS[source].label
    return "Where you got it" if source == IMPORTED else source


def to_csv(sites: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "category", "condition", "evidence", "latitude", "longitude", "sources", "reasons",
                "openstreetmap", "wikipedia", "added", "also_known_as", "entrances"])
    for s in sites:
        lk = links(s)
        entrances = "; ".join(f"{_entrance_label(e)} ({e['lat']:.6f}, {e['lng']:.6f})" for e in s.get("entrances") or [])
        w.writerow([s["name"], category_label(s["category"]), s["condition"], s["strength"], f"{s['lat']:.6f}",
                    f"{s['lng']:.6f}", s["sources"], " | ".join(s["reasons"]),
                    lk.get("osm_element", lk["openstreetmap"]), lk.get("wikipedia", ""), s.get("added") or "",
                    "; ".join(s.get("aliases") or []), entrances])
    return buf.getvalue()


def _entrance_label(e: dict) -> str:
    return e["name"] or e["kind"]


def _ways_in(s: dict):
    """Each entrance as a point of its own, named so a GPS list reads "Gripwood Quarry: Air shaft"."""
    for e in s.get("entrances") or []:
        label = e["name"] if e["name"] and s["name"].lower() in e["name"].lower() else f"{s['name']}: {_entrance_label(e)}"
        yield label, e


def to_kml(sites: list[dict], title: str = "bandobuddy") -> str:
    marks = []
    for s in sites:
        desc = "<br>".join(escape(r) for r in s["reasons"])
        marks.append(
            f"<Placemark><name>{escape(s['name'])} ({escape(s['condition'])})</name>"
            f"<description><![CDATA[{desc}]]></description>"
            f"<Point><coordinates>{s['lng']:.6f},{s['lat']:.6f},0</coordinates></Point></Placemark>"
        )
        for label, e in _ways_in(s):
            marks.append(f"<Placemark><name>{escape(label)}</name><description>{escape(e['kind'])}</description>"
                         f"<Point><coordinates>{e['lng']:.6f},{e['lat']:.6f},0</coordinates></Point></Placemark>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            f"<name>{escape(title)}</name>" + "".join(marks) + "</Document></kml>\n")


def to_gpx(sites: list[dict], title: str = "bandobuddy") -> str:
    pts = []
    for s in sites:
        pts.append(
            f'<wpt lat="{s["lat"]:.6f}" lon="{s["lng"]:.6f}"><name>{escape(s["name"])}</name>'
            f"<desc>{escape(s['condition'] + ': ' + ' | '.join(s['reasons']))}</desc>"
            f"<type>{escape(category_label(s['category']))}</type></wpt>"
        )
        for label, e in _ways_in(s):
            pts.append(f'<wpt lat="{e["lat"]:.6f}" lon="{e["lng"]:.6f}"><name>{escape(label)}</name>'
                       f"<desc>{escape(e['kind'])} of {escape(s['name'])}</desc><type>Entrance</type></wpt>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="bandobuddy" xmlns="http://www.topografix.com/GPX/1/1">'
            f"<metadata><name>{escape(title)}</name></metadata>" + "".join(pts) + "</gpx>\n")


FORMATS = {
    "csv": (to_csv, "text/csv; charset=utf-8"),
    "kml": (to_kml, "application/vnd.google-earth.kml+xml"),
    "gpx": (to_gpx, "application/gpx+xml"),
}
