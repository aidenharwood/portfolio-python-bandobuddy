"""Turn raw OSM and Wikidata items into merged, scored sites.

Rebuilt from scratch after each update step (a few seconds for the whole UK), which keeps the
merging rules in one place and means tweaking the scoring never needs a re-crawl.
"""
from __future__ import annotations

import math
from collections import defaultdict
from difflib import SequenceMatcher

from .geo import haversine_m
from .osm import best_name, classify, describe_kind
from .scoring import category_for, score_site, tier_for
from .store import Store
from .wikidata import evaluate, wikipedia_title

TWIN_M = 40      # OSM elements this close (and not differently named) are one site
MATCH_M = 80     # Wikidata items this close and similarly named, or within SAME_SPOT_M, join a site
SAME_SPOT_M = 25
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


def _best_match(item: dict, candidates: list[dict]) -> dict | None:
    best, best_score = None, 0.0
    for c in candidates:
        d = haversine_m(item["lat"], item["lng"], c["lat"], c["lng"])
        if d > MATCH_M:
            continue
        sim = _similar(item["name"], c["name"]) if item.get("name") else 0.0
        if d <= SAME_SPOT_M or sim >= 0.6:
            s = sim + (1 - d / MATCH_M)
            if s > best_score:
                best, best_score = c, s
    return best


def build_sites(store: Store) -> int:
    """Rebuild the sites table from the raw items. Returns how many sites there are."""
    grid = Grid()
    sites: list[dict] = []
    baseline = {src: (store.get_setting(f"baseline_{src}") or None) for src in ("osm", "wikidata")}

    # OSM: strongest evidence first, so a cluster is keyed by (and named after) its best element.
    osm_items = []
    for item in store.active_osm():
        verdict = classify(item["tags"], item["osm_id"].split("/", 1)[0])
        if verdict:
            osm_items.append((verdict, item))
    osm_items.sort(key=lambda t: (-t[0][1], t[1]["osm_id"]))
    for (evidence, weight), item in osm_items:
        tags = item["tags"]
        name = best_name(tags)
        ev = {"osm_id": item["osm_id"], "name": name, "kind": describe_kind(tags), "evidence": evidence,
              "weight": weight, "tags": tags, "first_seen": item["first_seen"], "source": "osm"}
        twin = next((s for s in grid.near(item["lat"], item["lng"])
                     if haversine_m(s["lat"], s["lng"], item["lat"], item["lng"]) <= TWIN_M
                     and (not name or not s["osm_name"] or name == s["osm_name"])), None)
        if twin:
            twin["osm"].append(ev)
            if name and not twin["osm_name"]:
                twin["osm_name"] = twin["name"] = name
            continue
        site = {"key": f"osm:{item['osm_id']}", "name": name or f"Unnamed {ev['kind']}", "osm_name": name,
                "lat": item["lat"], "lng": item["lng"], "osm": [ev], "wikidata": []}
        sites.append(site)
        grid.add(site)

    # Wikidata: attach to an OSM site at the same spot, otherwise a site of its own.
    intros = store.intros()
    for row in sorted(store.active_wd(), key=lambda r: r["qid"]):
        ev = evaluate(row, intros.get(wikipedia_title(row["wiki"]) or ""))
        if not ev:
            continue
        ev.update(first_seen=row["first_seen"], source="wikidata")
        target = _best_match(ev, grid.near(ev["lat"], ev["lng"]))
        if target:
            target["wikidata"].append(ev)
            if target["name"].startswith("Unnamed "):
                target["name"] = ev["name"]
            continue
        site = {"key": f"wd:{ev['qid']}", "name": ev["name"], "osm_name": None, "lat": ev["lat"], "lng": ev["lng"],
                "osm": [], "wikidata": [ev]}
        sites.append(site)
        grid.add(site)

    out = []
    for site in sites:
        members = site["osm"] + site["wikidata"]
        score, reasons = score_site(site)
        first_seen = min(m["first_seen"] for m in members)
        # "New" only once a source's first full crawl is done, and only if every part of the site is new.
        is_new = all(baseline[m["source"]] and m["first_seen"] > baseline[m["source"]] for m in members)
        best = max(members, key=lambda m: m["weight"])
        sources = "+".join(src for src in ("osm", "wikidata") if site[src])
        out.append({
            "key": site["key"],
            "name": site["name"],
            "lat": site["lat"],
            "lng": site["lng"],
            "score": score,
            "tier": tier_for(score),
            "category": category_for(site),
            "kind": best["kind"],
            "sources": sources,
            "reasons": reasons,
            "detail": {"osm": site["osm"], "wikidata": site["wikidata"]},
            "first_seen": first_seen,
            "added": first_seen if is_new else None,
        })
    store.replace_sites(out)
    return len(out)
