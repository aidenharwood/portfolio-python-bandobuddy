"""Load places from a file you already have.

Some good records aren't open data: the Defence of Britain archive (ROC posts, pillboxes and the
rest of the 20th-century military landscape) is a one-off download, and your own notes are your own.
Anything imported here stays in your copy of the database - it is never fetched or published by
bandobuddy - so the licence on it stays between you and whoever compiled it.

Reads CSV, GPX, GeoJSON, KML and KMZ. Positions can be any of the usual column names.
"""
from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .opendata import judge_record, sounds_gone

SOURCE = "imported"
DEFAULT_WEIGHT = 20                     # your own records: shown by default, below hard OSM evidence
GONE_WEIGHT = 5                         # ...unless your notes say it's gone: kept, but a weak lead
NAME_KEYS = ("name", "title", "site", "site_name", "label", "description")
LAT_KEYS = ("lat", "latitude", "y", "ycoord", "northing_lat")
LNG_KEYS = ("lng", "lon", "long", "longitude", "x", "xcoord")
KIND_KEYS = ("type", "site_type", "category", "class", "monument_type", "kind")
NOTE_KEYS = ("description", "notes", "note", "comment", "summary", "condition")
URL_KEYS = ("url", "link", "website", "href")
ALIAS_KEYS = ("alt_name", "alt_names", "aliases", "alias", "aka", "also_known_as", "other_names", "alternative_names")


class BadFile(ValueError):
    """The file couldn't be read as places."""


def _pick(row: dict, keys) -> str:
    for key in keys:
        for actual, value in row.items():
            if (actual or "").strip().lower().replace(" ", "_") == key and str(value or "").strip():
                return str(value).strip()
    return ""


def _coords(row: dict) -> tuple[float, float] | None:
    try:
        return float(_pick(row, LAT_KEYS)), float(_pick(row, LNG_KEYS))
    except ValueError:
        return None


def _place(name: str, lat: float, lng: float, kind: str = "", note: str = "", url: str = "",
           aliases: str = "") -> dict:
    return {"name": name.strip(), "lat": lat, "lng": lng, "kind": kind.strip(), "note": note.strip(),
            "url": url.strip(), "aliases": [a.strip() for a in re.split(r"[;|]", aliases or "") if a.strip()]}


def read_csv(text: str) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(text)))
    places = []
    for row in rows:
        point = _coords(row)
        if not point:
            continue
        places.append(_place(_pick(row, NAME_KEYS), *point, _pick(row, KIND_KEYS), _pick(row, NOTE_KEYS),
                             _pick(row, URL_KEYS), _pick(row, ALIAS_KEYS)))
    return places


def _tag(element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()


def _find(element, name: str) -> str:
    for child in element.iter():
        if _tag(child) == name and (child.text or "").strip():
            return child.text.strip()
    return ""


def read_gpx(text: str) -> list[dict]:
    root = ET.fromstring(text)
    places = []
    for point in root.iter():
        if _tag(point) != "wpt":
            continue
        try:
            lat, lng = float(point.get("lat")), float(point.get("lon"))
        except (TypeError, ValueError):
            continue
        places.append(_place(_find(point, "name"), lat, lng, _find(point, "type"), _find(point, "desc"),
                             _find(point, "link")))
    return places


def read_kml(text: str) -> list[dict]:
    root = ET.fromstring(text)
    places = []
    for mark in root.iter():
        if _tag(mark) != "placemark":
            continue
        raw = _find(mark, "coordinates")
        parts = raw.replace("\n", " ").split()[0].split(",") if raw else []
        if len(parts) < 2:
            continue
        try:
            lng, lat = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        places.append(_place(_find(mark, "name"), lat, lng, note=_find(mark, "description")))
    return places


def read_kmz(data: bytes) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        kml = next((n for n in archive.namelist() if n.lower().endswith(".kml")), None)
        if not kml:
            raise BadFile("That .kmz has no .kml inside it")
        return read_kml(archive.read(kml).decode("utf-8", "replace"))


def read_geojson(text: str) -> list[dict]:
    data = json.loads(text)
    places = []
    for feature in data.get("features", data if isinstance(data, list) else []):
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates")
        if geometry.get("type") != "Point" or not coords:
            continue
        props = feature.get("properties") or {}
        places.append(_place(_pick(props, NAME_KEYS), float(coords[1]), float(coords[0]),
                             _pick(props, KIND_KEYS), _pick(props, NOTE_KEYS), _pick(props, URL_KEYS),
                             _pick(props, ALIAS_KEYS)))
    return places


def read_file(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    try:
        if suffix == ".kmz":
            return read_kmz(path.read_bytes())
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if suffix == ".csv" or suffix == ".tsv":
            return read_csv(text)
        if suffix == ".gpx":
            return read_gpx(text)
        if suffix in (".kml", ".xml"):
            return read_kml(text)
        if suffix in (".geojson", ".json"):
            return read_geojson(text)
    except (ET.ParseError, json.JSONDecodeError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise BadFile(f"Couldn't read {path.name}: {exc}")
    raise BadFile(f"Don't know how to read {suffix or 'that file'}; use CSV, GPX, GeoJSON, KML or KMZ")


_PUNCT = re.compile(r"[^a-z0-9]+")


def _ref(label: str, place: dict) -> str:
    """Keyed on name and position, so importing the same file again updates rather than duplicates."""
    name = _PUNCT.sub("-", place["name"].lower()).strip("-")[:60]
    return f"{label}:{name}:{place['lat']:.5f},{place['lng']:.5f}"


def to_items(places: list[dict], label: str) -> list[dict]:
    items = []
    for place in places:
        described = " ".join(x for x in (place["kind"], place["note"], place["name"]) if x)
        verdict = judge_record(described)
        weight, judged = verdict if verdict else (DEFAULT_WEIGHT, "place")
        kind = place["kind"] or judged   # what you called it says more than our guess ("cave entrance")
        if sounds_gone(described):
            weight = GONE_WEIGHT
        detail = place["kind"] or place["note"][:120]
        items.append({
            "dataset": SOURCE,
            "ref": _ref(label, place),
            "name": place["name"] or f"Unnamed {kind}",
            "lat": place["lat"],
            "lng": place["lng"],
            "kind": kind,
            "evidence": f"From your import \"{label}\"" + (f": {detail}" if detail else ""),
            "weight": weight,
            "url": place["url"] or None,
            "aliases": place["aliases"],
        })
    return items


def load(store, path: Path, label: str | None = None) -> int:
    """Read a file into the database as your own places. Returns how many were loaded."""
    from .store import now_iso

    places = read_file(path)
    if not places:
        raise BadFile(f"No places with a position found in {path.name}")
    items = to_items(places, label or _PUNCT.sub("-", path.stem.lower()).strip("-") or "import")
    store.upsert_od(items, now_iso())
    return len(items)
