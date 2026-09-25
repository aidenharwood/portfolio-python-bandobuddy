"""OpenStreetMap: find places mappers have tagged as abandoned, disused or ruined - plus old mines,
bunkers, pillboxes, caves and dead railway tunnels - in a local .osm.pbf extract (see extract.py).

Reading a local file means no rate limits, full coverage, and free-text checks on every
name/description/note, which public Overpass servers can't afford to do for a whole country.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Callable

from .geo import haversine_m

# Lifecycle prefixes on things that are just lines on the ground (old trackbeds,
# removed power lines, etc.) - not somewhere to explore.
LINEAR_VALUES = {
    "rail", "tram", "light_rail", "narrow_gauge", "subway", "monorail", "funicular",
    "line", "minor_line", "cable", "road", "track", "path", "footway", "service",
    "unclassified", "residential", "canal", "ditch", "drain", "pipeline",
    "motorway", "trunk", "primary", "secondary", "tertiary", "motorway_link", "trunk_link", "primary_link",
    "secondary_link", "tertiary_link", "living_street", "pedestrian", "bridleway", "cycleway", "steps",
}

# Evidence weights, fed into the score in scoring.py.
STRONG = 35
MEDIUM = 25
CAVE = 15
WEAK = 10

DEAD_WORDS_RX = "derelict|abandoned|disused|boarded up|ruined"
_DEAD_WORDS = re.compile(rf"\b({DEAD_WORDS_RX})\b", re.I)
MINE_TAGS = {("man_made", "adit"), ("man_made", "mineshaft"), ("historic", "mine"), ("historic", "mine_shaft"),
             ("historic", "mine_adit"), ("historic", "quarry")}

# Cheap first check while streaming millions of tagged objects: key -> values worth a closer look.
_INTERESTING = {
    "abandoned": {"yes"},
    "disused": {"yes"},
    "building": {"ruins", "abandoned", "bunker"},
    "ruins": {"yes"},
    "historic": {"ruins", "mine", "mine_shaft", "mine_adit", "quarry", "bunker", "pillbox", "railway_station"},
    "landuse": {"brownfield"},
    "man_made": {"adit", "mineshaft"},
    "natural": {"cave_entrance"},
    "military": {"bunker"},
    "railway": {"abandoned", "disused"},
}
_TEXT_KEYS = ("name", "description", "note")


HERITAGE_OPERATORS = re.compile(r"english heritage|national trust|cadw|historic (environment )?scotland", re.I)
# A closed shop, bank or pub mapped as a single point is usually a unit in a busy street,
# not a building standing empty.
POINT_UNIT_KEYS = ("disused:shop", "disused:amenity", "disused:office", "disused:craft", "disused:healthcare")
POINT_UNIT = 15


# In use as somewhere to visit: whatever it was, it's open, staffed and looked after now.
ATTRACTIONS = {"museum": "Museum", "gallery": "Gallery", "attraction": "Visitor attraction",
               "theme_park": "Theme park", "zoo": "Zoo", "aquarium": "Aquarium"}


def in_use_as(tags: dict[str, str]) -> str | None:
    """What a place is in use as today, if that's a visitor attraction ("Museum"), else None."""
    if tags.get("tourism") in ATTRACTIONS:
        return ATTRACTIONS[tags["tourism"]]
    if tags.get("railway") in ("station", "halt") and (tags.get("usage") == "tourism"
                                                       or tags.get("railway:preserved") == "yes"):
        return "Heritage railway"
    if HERITAGE_OPERATORS.search(tags.get("operator", "")):
        return "Heritage site"
    return None


def is_heritage_site(tags: dict[str, str]) -> bool:
    """Ruins that are a visitor attraction (castles, abbeys) rather than somewhere to explore."""
    return (tags.get("historic") in ("castle", "abbey", "monastery", "archaeological_site")
            or tags.get("tourism") in ("attraction", "museum")
            or bool(tags.get("opening_hours") or tags.get("fee"))
            or bool(HERITAGE_OPERATORS.search(tags.get("operator", ""))))


def classify(tags: dict[str, str], element: str | None = None) -> tuple[str, int] | None:
    """Return (evidence description, weight) or None if this element is just noise.

    `element` is "node", "way" or "relation" when known; it only changes a few weights.
    """
    verdict = _classify(tags)
    if not verdict:
        return None
    evidence, weight = verdict
    if weight == STRONG and ("building=" in evidence or "ruins" in evidence) and is_heritage_site(tags):
        return f"{evidence} (heritage site open to visitors)", WEAK
    if element == "node" and weight == MEDIUM and evidence.startswith(tuple(f"OSM: {k}" for k in POINT_UNIT_KEYS)):
        return f"{evidence} (a single shop unit)", POINT_UNIT
    return evidence, weight


# What a place *is*, most telling first. A lifecycle prefix only means something on one of these:
# "disused:amenity=pub" is a shut pub, but "disused:website" is only a lapsed website.
FEATURE_KEYS = ("amenity", "shop", "railway", "aeroway", "military", "healthcare", "office", "craft", "tourism",
                "leisure", "club", "industrial", "man_made", "power", "public_transport", "emergency", "historic",
                "landuse", "building", "highway", "waterway")


def _classify(tags: dict[str, str]) -> tuple[str, int] | None:
    tunnel = tags.get("tunnel") not in (None, "no")
    meaningful = {}
    for base in FEATURE_KEYS:
        for state in ("abandoned", "disused"):
            v = tags.get(f"{state}:{base}")
            if v is None or (base in ("railway", "highway", "power", "waterway", "man_made")
                             and v in LINEAR_VALUES and not tunnel):
                continue
            meaningful[f"{state}:{base}"] = v

    if tags.get("building") in ("ruins", "abandoned") or tags.get("ruins") == "yes":
        return f"OSM: building={tags.get('building', 'ruins')}", STRONG
    if tags.get("abandoned") == "yes":
        return "OSM: abandoned=yes", STRONG
    for k, v in meaningful.items():
        if k.startswith("abandoned:"):
            what = "abandoned railway tunnel" if tunnel and v in LINEAR_VALUES else f"{k}={v}"
            return f"OSM: {what}", STRONG
    if tunnel and tags.get("railway") in ("abandoned", "disused"):
        return f"OSM: {tags['railway']} railway tunnel", STRONG
    for key in _TEXT_KEYS:
        m = _DEAD_WORDS.search(tags.get(key) or "")
        if m:
            return f"OSM: {key} says '{m.group(1).lower()}'", MEDIUM
    if tags.get("disused") == "yes":
        return "OSM: disused=yes", MEDIUM
    for k, v in meaningful.items():
        what = "disused railway tunnel" if tunnel and v in LINEAR_VALUES else f"{k}={v}"
        return f"OSM: {what}", MEDIUM
    for k, v in MINE_TAGS:
        if tags.get(k) == v:
            return f"OSM: old mine or quarry ({k}={v})", MEDIUM
    if tags.get("military") == "bunker" or tags.get("building") == "bunker" or tags.get("historic") in ("bunker", "pillbox"):
        kind = tags.get("bunker_type") or tags.get("historic") or "bunker"
        return f"OSM: military bunker ({kind})", MEDIUM
    if tags.get("historic") == "railway_station":
        return "OSM: former railway station (historic=railway_station)", MEDIUM
    if tags.get("natural") == "cave_entrance":
        return "OSM: cave entrance", CAVE
    # Usually castles, abbeys and other heritage sites open to visitors, not bandos.
    if tags.get("historic") == "ruins":
        return "OSM: historic=ruins (often a heritage site)", WEAK
    if tags.get("landuse") == "brownfield":
        return "OSM: landuse=brownfield", WEAK
    return None


def best_name(tags: dict[str, str]) -> str | None:
    for key in ("name", "abandoned:name", "disused:name", "old_name", "was:name", "official_name"):
        if tags.get(key):
            return tags[key]
    return None


def describe_kind(tags: dict[str, str]) -> str:
    if tags.get("natural") == "cave_entrance":
        return "cave entrance"
    for key in ("building", "amenity", "shop", "tourism", "leisure", "industrial",
                "man_made", "railway", "military", "historic", "landuse"):
        for prefix in ("", "abandoned:", "disused:"):
            v = tags.get(prefix + key)
            if v and v not in ("yes", "ruins"):
                return v.replace("_", " ")
    return "structure"


def is_candidate(tags) -> bool:
    """Fast pre-check on an osmium TagList (or any iterable of objects with .k/.v)."""
    for tag in tags:
        k = tag.k
        if k.startswith(("abandoned", "disused")):
            base = k.split(":", 1)[1] if ":" in k else ""
            if base in FEATURE_KEYS or (not base and tag.v == "yes"):
                return True
            continue
        values = _INTERESTING.get(k)
        if values is not None and tag.v in values:
            return True
        if (k == "tourism" and tag.v in ATTRACTIONS) or (k == "usage" and tag.v == "tourism") \
                or (k == "operator" and HERITAGE_OPERATORS.search(tag.v)):
            return True  # not a lead itself, but it tells us a lead nearby is open to the public
        if k in _TEXT_KEYS and _DEAD_WORDS.search(tag.v):
            return True
    return False


class Cancelled(Exception):
    pass


def extract_candidates(
    pbf: Path,
    progress: Callable[[str, int, int | None], None] | None = None,
    cancel: threading.Event | None = None,
    on_nodes: Callable[[list[dict]], None] | None = None,
) -> list[dict]:
    """Every OSM element in the extract that classify() accepts, as {osm_id, lat, lng, tags}.

    Three passes, so no giant node-location index is needed:
      1. tagged objects: keep matching nodes (they carry coordinates), ways and relations
      2. member ways of matching relations: their node lists
      3. the nodes those ways use: their coordinates, to place each way/relation at its bbox centre
    """
    import osmium  # optional heavy dependency; only needed when building the database
    from osmium.filter import EmptyTagFilter, IdFilter

    report = progress or (lambda stage, done, total: None)
    cancel = cancel or threading.Event()
    path = str(pbf)

    nodes: list[dict] = []
    ways: dict[int, tuple[dict, list[int]]] = {}
    rels: dict[int, tuple[dict, list[int]]] = {}
    seen = 0
    for obj in osmium.FileProcessor(path).with_filter(EmptyTagFilter()):
        seen += 1
        if seen % 250_000 == 0:
            if cancel.is_set():
                raise Cancelled()
            report("Reading OSM tags", seen, None)
        if not is_candidate(obj.tags):
            continue
        tags = {t.k: t.v for t in obj.tags}
        if classify(tags) is None and in_use_as(tags) is None:
            continue
        kind = obj.type_str()
        if kind == "n":
            if obj.location.valid():
                nodes.append({"osm_id": f"node/{obj.id}", "lat": obj.location.lat, "lng": obj.location.lon,
                              "extent_m": 0, "tags": tags})
        elif kind == "w":
            ways[obj.id] = (tags, [n.ref for n in obj.nodes])
        else:
            rels[obj.id] = (tags, [m.ref for m in obj.members if m.type == "w"])
    report("Reading OSM tags", seen, seen)
    if on_nodes and nodes:
        on_nodes(nodes)  # points are ready now; outlines need two more passes

    member_ways: dict[int, list[int]] = {}
    wanted_ways = {ref for _, members in rels.values() for ref in members} - set(ways)
    if wanted_ways:
        report("Reading relation outlines", 0, len(wanted_ways))
        for w in osmium.FileProcessor(path, osmium.osm.WAY).with_filter(IdFilter(wanted_ways)):
            member_ways[w.id] = [n.ref for n in w.nodes]
        if cancel.is_set():
            raise Cancelled()
    for wid, (_, refs) in ways.items():
        member_ways.setdefault(wid, refs)

    wanted_nodes = {ref for refs in member_ways.values() for ref in refs}
    coords: dict[int, tuple[float, float]] = {}
    if wanted_nodes:
        report("Placing outlines", 0, len(wanted_nodes))
        for n in osmium.FileProcessor(path, osmium.osm.NODE).with_filter(IdFilter(wanted_nodes)):
            if n.location.valid():
                coords[n.id] = (n.location.lat, n.location.lon)
        if cancel.is_set():
            raise Cancelled()

    def place(refs: list[int]) -> tuple[float, float, int] | None:
        """Centre of the outline's box, and half its diagonal: how far the outline reaches."""
        pts = [coords[r] for r in refs if r in coords]
        if not pts:
            return None
        lats, lngs = [p[0] for p in pts], [p[1] for p in pts]
        reach = haversine_m(min(lats), min(lngs), max(lats), max(lngs)) / 2
        return (min(lats) + max(lats)) / 2, (min(lngs) + max(lngs)) / 2, round(reach)

    out = list(nodes)
    for wid, (tags, refs) in ways.items():
        c = place(refs)
        if c:
            out.append({"osm_id": f"way/{wid}", "lat": c[0], "lng": c[1], "extent_m": c[2], "tags": tags})
    for rid, (tags, members) in rels.items():
        c = place([r for wid in members for r in member_ways.get(wid, [])])
        if c:
            out.append({"osm_id": f"relation/{rid}", "lat": c[0], "lng": c[1], "extent_m": c[2], "tags": tags})
    report("Placing outlines", len(wanted_nodes), len(wanted_nodes))
    return out
