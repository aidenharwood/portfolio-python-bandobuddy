"""Turn raw OSM and Wikidata items into merged, scored sites.

Rebuilt from scratch after each update step (a few seconds for the whole UK), which keeps the
merging rules in one place and means tweaking the scoring never needs a re-crawl.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher

from .geo import haversine_m
from .osm import alt_names, best_name, classify, describe_kind, entrance_kind, in_use_as
from .config import WEAK_BELOW
from .scoring import category_for, condition_for, score_site, strength_for
from .store import Store
from .wikidata import evaluate, wikipedia_title

TWIN_M = 40      # OSM elements this close (and not differently named) are one site
MATCH_M = 80     # Wikidata items this close and similarly named, or within SAME_SPOT_M, join a site
SAME_SPOT_M = 25
LOOSE_M = 60     # a weak lead this close to a site is the same place, whatever each calls it
COMPLEX_M = 120  # parts of one quarry or colliery that a register recorded separately
ATTRACTION_SPOT_M = 15    # a museum mapped as a single point speaks for its own building...
ATTRACTION_POINT_M = 40   # ...or a neighbour this close that shares its name; on a high street, 40 m is five shops
ATTRACTION_EDGE_M = 20    # slack around an outline, for a colliery point just inside the museum's fence
ATTRACTION_MAX_M = 1500   # an "attraction" reaching further (a country park) says nothing about one building
ATTRACTION_CELL = 0.01    # ~1 km lookups: wider than the biggest reach we believe
ENTRANCE_M = 400          # a named way in this close, sharing a name with a place, is one of its entrances
ENTRANCE_UNNAMED_M = 80   # an unnamed shaft or cave entrance only joins what it's right beside
ENTRANCE_CELL = 0.01
CELL_DEG = 0.002  # grid cell for neighbour lookups; bigger than MATCH_M in both directions


class Grid:
    def __init__(self):
        self.cells: dict[tuple[int, int], list[dict]] = defaultdict(list)

    @staticmethod
    def _cell(lat: float, lng: float) -> tuple[int, int]:
        return math.floor(lat / CELL_DEG), math.floor(lng / CELL_DEG)

    def add(self, site: dict) -> None:
        self.cells[self._cell(site["lat"], site["lng"])].append(site)

    def near(self, lat: float, lng: float) -> list[dict]:
        ci, cj = self._cell(lat, lng)
        return [s for di in (-1, 0, 1) for dj in (-1, 0, 1) for s in self.cells.get((ci + di, cj + dj), ())]


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _strongest(site: dict) -> int:
    return max((m["weight"] for m in site["osm"] + site["wikidata"] + site["open"]), default=0)


def _members(site: dict) -> list[dict]:
    return site["osm"] + site["wikidata"] + site["open"]


def _names(item: dict) -> list[str]:
    return [n for n in [item.get("name"), *(item.get("aliases") or [])] if n and not n.startswith("Unnamed ")]


def _site_names(site: dict) -> set[str]:
    """Every name a site goes by: what it's shown as, what each source calls it, and their other names."""
    names = set(_names(site))
    for m in _members(site):
        names.update(_names(m))
    return names


def _best_match(item: dict, candidates: list[dict]) -> dict | None:
    best, best_score = None, 0.0
    mine = _names(item)
    for c in candidates:
        d = haversine_m(item["lat"], item["lng"], c["lat"], c["lng"])
        if d > MATCH_M:
            continue
        sim = max((_similar(a, b) for a in mine for b in _site_names(c)), default=0.0)
        # A brownfield plot or heritage listing sitting on a ruin is that ruin, not a second place.
        loose = d <= LOOSE_M and min(item["weight"], _strongest(c)) < WEAK_BELOW
        if d <= SAME_SPOT_M or sim >= 0.6 or loose:
            s = sim + (1 - d / MATCH_M)
            if s > best_score:
                best, best_score = c, s
    return best


# Words that name a kind of thing, or a part of one, rather than a particular place.
_KINDS = {"quarry", "quarries", "mine", "mines", "mill", "mills", "colliery", "collieries", "works", "pit", "pits",
          "shaft", "shafts", "adit", "adits", "level", "levels", "kiln", "kilns", "ironworks", "brickworks"}
_PARTS = {"track", "trackway", "incline", "tramway", "ropeway", "base", "engine", "house", "building", "buildings",
          "site", "post", "battery", "winding", "wheelpit", "tip", "tips", "reservoir", "leat", "office", "store",
          "disused", "former", "old", "north", "south", "east", "west", "upper", "lower", "the", "of", "and", "y"}
_NUMBERED = re.compile(r"\s+(?:[ivx]+|\d+)$", re.I)


def _words(part: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", part.lower()) if not re.fullmatch(r"[ivx]+|\d+", w)]


def _complex_parts(name: str) -> set[str]:
    """Parts of a name that name a whole complex: a distinctive word and a kind of place
    ("Manod Granite Quarries"), not a village ("Llandaff") or a bare kind ("Disused Quarry")."""
    out = set()
    for part in name.split(","):
        words = _words(part)
        if any(w in _KINDS for w in words) and any(w not in _KINDS and w not in _PARTS for w in words):
            out.add(" ".join(words))
    return out


def _numbered(name: str) -> tuple[str, str] | None:
    """"Quarry II, Bwlch y Bi" -> ("quarry", "bwlch y bi"): one of several numbered parts."""
    parts = [p.strip() for p in name.split(",")]
    if len(parts) != 2 or not _NUMBERED.search(parts[0]):
        return None
    return " ".join(_words(parts[0])), " ".join(_words(parts[1]))


def _same_complex(item: dict, candidates: list[dict]) -> tuple[dict, str] | None:
    """The site this record is one more part of, and the name for the whole."""
    mine, numbered = _complex_parts(item["name"]), _numbered(item["name"])
    if not mine and not numbered:
        return None
    best, closest = None, COMPLEX_M
    for c in candidates:
        d = haversine_m(item["lat"], item["lng"], c["lat"], c["lng"])
        if d > closest:
            continue
        shared = mine & _complex_parts(c["name"])
        if shared:
            best, closest = (c, next(iter(shared))), d
        elif numbered and _numbered(c["name"]) == numbered:
            best, closest = (c, None), d
    if not best:
        return None
    site, shared = best
    if shared:  # name it after the complex, as the register spelled it
        whole = next(p.strip() for p in item["name"].split(",") if " ".join(_words(p)) == shared)
        return site, _NUMBERED.sub("", whole)
    feature, place = [p.strip() for p in item["name"].split(",")]
    return site, f"{_NUMBERED.sub('', feature)}, {place}"


class Attractions:
    """Places in use as somewhere to visit, and how far each reaches."""

    def __init__(self):
        self.cells: dict[tuple[int, int], list[dict]] = defaultdict(list)

    def add(self, lat: float, lng: float, reach_m: float | None, kind: str, name: str | None, source: str) -> None:
        """`reach_m` is how far an outline reaches; None for something mapped as a point."""
        self.cells[(math.floor(lat / ATTRACTION_CELL), math.floor(lng / ATTRACTION_CELL))].append(
            {"lat": lat, "lng": lng, "reach": reach_m, "kind": kind, "name": name, "source": source})

    def covering(self, lat: float, lng: float, name: str) -> dict | None:
        ci, cj = math.floor(lat / ATTRACTION_CELL), math.floor(lng / ATTRACTION_CELL)
        best, closest = None, math.inf
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for a in self.cells.get((ci + di, cj + dj), ()):
                    d = haversine_m(lat, lng, a["lat"], a["lng"])
                    if a["reach"] is not None:
                        inside = d <= a["reach"]
                    else:
                        inside = d <= ATTRACTION_SPOT_M or (d <= ATTRACTION_POINT_M and _share_a_name(name, a["name"]))
                    if inside and d < closest:
                        best, closest = a, d
        return best


_PLAIN_WORDS = {"the", "and", "old", "museum", "gallery", "centre", "center", "heritage", "house", "hall", "visitor",
                "attraction", "trust", "national", "english", "unnamed", "building", "structure", "church", "chapel"}


def _share_a_name(a: str | None, b: str | None) -> bool:
    """"Wilton Royal Carpet Factory" and "Wilton Royal Carpet Factory Museum" share a name; "The Bear" and
    "Chippenham Museum" don't."""
    def words(s):
        return {w for w in re.findall(r"[a-z']+", (s or "").lower()) if len(w) > 3 and w not in _PLAIN_WORDS}
    return bool(words(a) & words(b))


_ENTRANCE_WORDS = [
    (re.compile(r"air ?shaft|ventilation shaft", re.I), "Air shaft"),
    (re.compile(r"cave entrance|\bentrance\b|\bportal\b", re.I), "Entrance"),
    (re.compile(r"\badit\b", re.I), "Adit"),
    (re.compile(r"\bshaft\b|mineshaft", re.I), "Shaft"),
]
# Too common to tie a way in to a place: "Gripwood" does, "Quarry", "Lane" and "Hill" don't.
_PLAIN_PLACE_WORDS = {
    "quarry", "quarries", "mine", "mines", "cave", "caves", "cavern", "entrance", "shaft", "shafts", "adit", "adits",
    "main", "subway", "tunnel", "portal", "level", "north", "south", "east", "west", "upper", "lower", "great",
    "little", "road", "lane", "hill", "wood", "farm", "house", "street", "park", "common", "down", "downs",
    "bridge", "close", "vale", "valley", "field", "fields", "works", "colliery", "stone", "slate", "lime",
}


def _entrance_from_text(text: str) -> str | None:
    for rx, kind in _ENTRANCE_WORDS:
        if rx.search(text or ""):
            return kind
    return None


def _distinctive(names) -> set[str]:
    return {w for n in names for w in re.findall(r"[a-z']+", n.lower()) if len(w) > 3 and w not in _PLAIN_PLACE_WORDS}


def _only_entrances(site: dict) -> bool:
    members = _members(site)
    return bool(members) and all(m.get("entrance") for m in members)


def _strongest_member(site: dict) -> int:
    return max((m["weight"] for m in _members(site)), default=0)


def _absorb(parent: dict, child: dict) -> None:
    for key in ("osm", "wikidata", "open"):
        parent[key].extend(child[key])
    if parent["name"].startswith("Unnamed ") and not child["name"].startswith("Unnamed "):
        parent["name"] = child["name"]


def _common_start(names: list[str]) -> str:
    """"Gripwood Quarry Main Entrance" and "Gripwood Quarry Entrance 1" -> "Gripwood Quarry"."""
    split = [n.split() for n in names]
    common = []
    for words in zip(*split):
        if len({w.lower() for w in words}) != 1:
            break
        common.append(words[0])
    return " ".join(common)


def _join_entrances(sites: list[dict]) -> list[dict]:
    """Shafts, adits and cave entrances that belong to a place nearby join it, so a cave system or a mine
    is one place with several ways in, not a scatter of pins. A way in joins a place within 400 m that
    shares a distinctive name with it (or one of its other names), or one within 80 m if it has no name."""
    index: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for s in sites:
        index[(math.floor(s["lat"] / ENTRANCE_CELL), math.floor(s["lng"] / ENTRANCE_CELL))].append(s)
    gone: set[int] = set()

    def near(site: dict) -> list[dict]:
        ci, cj = math.floor(site["lat"] / ENTRANCE_CELL), math.floor(site["lng"] / ENTRANCE_CELL)
        return [s for di in (-1, 0, 1) for dj in (-1, 0, 1) for s in index.get((ci + di, cj + dj), ())
                if s is not site and id(s) not in gone]

    def partner(entrance: dict, want_parent: bool) -> dict | None:
        words = _distinctive(_site_names(entrance))
        best, best_key = None, None
        for s in near(entrance):
            if _only_entrances(s) == want_parent:
                continue
            d = haversine_m(entrance["lat"], entrance["lng"], s["lat"], s["lng"])
            shared = len(words & _distinctive(_site_names(s))) if words else 0
            joined = (d <= ENTRANCE_M and shared) if words else d <= ENTRANCE_UNNAMED_M
            # Sharing a village name ("Minera") is weaker than sharing the mine's ("Taylor's Shaft Lead Mine").
            key = (-shared, d)
            if joined and (best_key is None or key < best_key):
                best, best_key = s, key
        return best

    ways_in = sorted((s for s in sites if _only_entrances(s)), key=lambda s: -_strongest_member(s))
    # First, ways into a place that's more than a hole: the quarry, the mine, the cave.
    for entrance in ways_in:
        parent = partner(entrance, want_parent=True)
        if parent:
            _absorb(parent, entrance)
            gone.add(id(entrance))
    # Then entrances with no such place: those that share a name become one, named for what they share.
    for entrance in ways_in:
        if id(entrance) in gone:
            continue
        other = partner(entrance, want_parent=False)
        if other and id(other) not in gone:
            _absorb(other, entrance)
            gone.add(id(entrance))
            shared = _common_start([m["name"] for m in _members(other)
                                    if m["name"] and not m["name"].startswith("Unnamed ")])
            if _distinctive([shared]):
                other["name"] = shared
    return [s for s in sites if id(s) not in gone]


def _entrances(site: dict) -> list[dict]:
    """The ways in, where there's more than one, or where the place is more than its one way in."""
    ways_in: list[dict] = []
    for m in _members(site):
        if not m.get("entrance") or m.get("lat") is None:
            continue
        if any(haversine_m(w["lat"], w["lng"], m["lat"], m["lng"]) < 8 for w in ways_in):
            continue  # the same hole, from two sources
        named = bool(m["name"]) and not m["name"].startswith("Unnamed ")
        ways_in.append({"name": m["name"] if named else "", "kind": m["entrance"], "lat": m["lat"], "lng": m["lng"]})
    if len(ways_in) == 1 and _only_entrances(site):
        return []
    return sorted(ways_in, key=lambda w: (w["name"] == "", w["name"], w["kind"]))


def _agreed_name(site: dict) -> str:
    """When sources disagree, the name most of them use: one record called "Bethel Quarry" that also
    goes by "Gripwood Quarry", and another called "Gripwood Quarry", make it Gripwood Quarry. A name
    worked out for a whole complex or cave system is only replaced when two or more sources outvote it."""
    votes: Counter = Counter()
    spelled: dict[str, str] = {}
    for m in _members(site):
        if m.get("entrance"):
            continue
        for name in _names(m):
            votes[name.lower()] += 1
            spelled.setdefault(name.lower(), name)
    if not votes:
        return site["name"]
    best, count = votes.most_common(1)[0]
    if count >= 2 and count > votes.get(site["name"].lower(), 0):
        return spelled[best]
    # A brownfield entry is a plot with an address ("22-23 High Street", "First Floor"); any other
    # source's name for the place ("The Guildhall, High Street") reads better.
    shown_by = [m for m in _members(site) if m.get("name") == site["name"]]
    if shown_by and all(m["source"] == "brownfield" for m in shown_by):
        better = [m for m in _members(site) if m["source"] != "brownfield" and not m.get("entrance")
                  and m.get("name") and not m["name"].startswith("Unnamed ")]
        if better:
            return max(better, key=lambda m: m["weight"])["name"]
    return site["name"]


def _aliases(site: dict) -> list[str]:
    """Other names the place goes by: each source's own name for it and the alternatives they list,
    but not the names of its entrances."""
    shown = site["name"].lower()
    per_source = Counter(m["source"] for m in _members(site))
    out: dict[str, str] = {}
    for m in _members(site):
        # A source's own name counts when it's that source's only record here (several records from one
        # register are parts of a complex, and brownfield entries are plots with addresses, not names), or
        # when the record says outright that it's this place ("Bethel Quarry", also known as Gripwood Quarry).
        says_so = any((alias or "").lower() == shown for alias in m.get("aliases") or [])
        sole = per_source[m["source"]] == 1 and m["source"] != "brownfield"
        own = [m["name"]] if not m.get("entrance") and (sole or says_so) else []
        for name in own + list(m.get("aliases") or []):
            low = (name or "").lower()
            if name and not name.startswith("Unnamed ") and low not in shown and shown not in low:
                out.setdefault(low, name)
    return list(out.values())[:6]


def build_sites(store: Store) -> int:
    """Rebuild the sites table from the raw items. Returns how many sites there are."""
    grid = Grid()
    sites: list[dict] = []
    baselines: dict[str, str | None] = {}

    def baseline(source: str) -> str | None:
        if source not in baselines:
            baselines[source] = store.get_setting(f"baseline_{source}") or None
        return baselines[source]

    # OSM: strongest evidence first, so a cluster is keyed by (and named after) its best element.
    osm_items = []
    attractions = Attractions()
    for item in store.active_osm():
        use = in_use_as(item["tags"])
        extent = item.get("extent_m") or 0
        if use and extent <= ATTRACTION_MAX_M:
            reach = extent + ATTRACTION_EDGE_M if extent else None
            attractions.add(item["lat"], item["lng"], reach, use, best_name(item["tags"]), "OpenStreetMap")
        verdict = classify(item["tags"], item["osm_id"].split("/", 1)[0])
        if verdict:
            osm_items.append((verdict, item))
    osm_items.sort(key=lambda t: (-t[0][1], t[1]["osm_id"]))
    for (evidence, weight), item in osm_items:
        tags = item["tags"]
        name = best_name(tags)
        ev = {"osm_id": item["osm_id"], "name": name, "kind": describe_kind(tags), "evidence": evidence,
              "weight": weight, "tags": tags, "first_seen": item["first_seen"], "source": "osm",
              "lat": item["lat"], "lng": item["lng"], "aliases": alt_names(tags), "entrance": entrance_kind(tags)}
        twin = next((s for s in grid.near(item["lat"], item["lng"])
                     if haversine_m(s["lat"], s["lng"], item["lat"], item["lng"]) <= TWIN_M
                     and (not name or not s["osm_name"] or name in _site_names(s))), None)
        if twin:
            twin["osm"].append(ev)
            if name and not twin["osm_name"]:
                twin["osm_name"] = twin["name"] = name
            continue
        site = {"key": f"osm:{item['osm_id']}", "name": name or f"Unnamed {ev['kind']}", "osm_name": name,
                "lat": item["lat"], "lng": item["lng"], "osm": [ev], "wikidata": [], "open": []}
        sites.append(site)
        grid.add(site)

    # Wikidata: attach to an OSM site at the same spot, otherwise a site of its own.
    intros = store.intros()
    for row in sorted(store.active_wd(), key=lambda r: r["qid"]):
        ev = evaluate(row, intros.get(wikipedia_title(row["wiki"]) or ""))
        if not ev:
            continue
        ev.update(first_seen=row["first_seen"], source="wikidata",
                  entrance=_entrance_from_text(f"{ev['kind']} {ev['name']}"))
        if ev.get("in_use"):
            attractions.add(ev["lat"], ev["lng"], None, ev["in_use"], ev["name"], "Wikidata")
        target = _best_match(ev, grid.near(ev["lat"], ev["lng"]))
        if target:
            target["wikidata"].append(ev)
            if target["name"].startswith("Unnamed "):
                target["name"] = ev["name"]
            continue
        site = {"key": f"wd:{ev['qid']}", "name": ev["name"], "osm_name": None, "lat": ev["lat"], "lng": ev["lng"],
                "osm": [], "wikidata": [ev], "open": []}
        sites.append(site)
        grid.add(site)

    # Open registers: join the site already at this spot, or stand alone like a Wikidata item.
    for row in store.active_od():
        # A register says what it recorded in its evidence ("Coflein records a mine shaft here"); its names
        # don't count ("Meadow Shaft Lead Mine: Rock-Crusher House" is a building). Your own imports say it
        # in their type, or failing that their name ("Gripwood Quarry Main Entrance").
        if row["dataset"] == "imported":
            said = row["kind"] if _entrance_from_text(row["kind"]) or row["kind"] not in ("place", "") else                 f"{row['kind']} {row['name']}"
        else:
            said = row["evidence"]
        ev = {"ref": row["ref"], "name": row["name"], "kind": row["kind"], "evidence": row["evidence"],
              "weight": row["weight"], "url": row["url"], "lat": row["lat"], "lng": row["lng"],
              "first_seen": row["first_seen"], "source": row["dataset"], "aliases": row.get("aliases") or [],
              "entrance": _entrance_from_text(said)}
        near = grid.near(row["lat"], row["lng"])
        # "Track II" and "Incline III" of the same quarry are one place to visit, named for the quarry.
        found = _same_complex(ev, [s for s in near if s["open"] and not s["osm"] and not s["wikidata"]])
        if found:
            complex_site, whole = found
            complex_site["open"].append(ev)
            complex_site["name"] = whole
            continue
        target = _best_match(ev, near)
        if target:
            target["open"].append(ev)
            if target["name"].startswith("Unnamed ") and not ev["name"].startswith("Unnamed "):
                target["name"] = ev["name"]
            continue
        site = {"key": f"{row['dataset']}:{row['ref']}", "name": ev["name"], "osm_name": None,
                "lat": row["lat"], "lng": row["lng"], "osm": [], "wikidata": [], "open": [ev]}
        sites.append(site)
        grid.add(site)

    sites = _join_entrances(sites)

    out = []
    for site in sites:
        site["name"] = _agreed_name(site)
        site["in_use"] = attractions.covering(site["lat"], site["lng"], site["name"])
        members = site["osm"] + site["wikidata"] + site["open"]
        score, reasons = score_site(site)
        first_seen = min(m["first_seen"] for m in members)
        # "New" only once a source's first full crawl is done, and only if every part of the site is new.
        is_new = all(baseline(m["source"]) and m["first_seen"] > baseline(m["source"]) for m in members)
        best = max(members, key=lambda m: m["weight"])
        order = {"osm": 0, "wikidata": 1}
        sources = "+".join(sorted({m["source"] for m in members}, key=lambda s: (order.get(s, 2), s)))
        out.append({
            "key": site["key"],
            "name": site["name"],
            "lat": site["lat"],
            "lng": site["lng"],
            "score": score,
            "strength": strength_for(score),
            "category": category_for(site),
            "condition": condition_for(site),
            "kind": best["kind"],
            "sources": sources,
            "reasons": reasons,
            "detail": {"osm": site["osm"], "wikidata": site["wikidata"], "open": site["open"]},
            "first_seen": first_seen,
            "added": first_seen if is_new else None,
            "aliases": _aliases(site),
            "entrances": _entrances(site),
        })
    store.replace_sites(out)
    return len(out)
