"""Abandonment likelihood scoring for a site built from open data.

  - OpenStreetMap evidence (abandoned/disused/ruins tags, old mines, bunkers, dead railway tunnels)
  - Wikidata/Wikipedia evidence (state of use, old mines and quarries, closure dates, intro wording)
  - A second independent source agreeing adds half its weight
  - Small hints: "former"/"derelict" in the name, an explorable building type
"""
from __future__ import annotations

import re

from .config import CATEGORIES, DEAD_NAME_WORDS, OTHER_CATEGORY, URBEX_NAME_WORDS, URBEX_TYPES

TIERS = [(60, "prime"), (35, "likely"), (15, "maybe"), (0, "long shot")]
_CATEGORY_RX = [(key, label, re.compile(rx, re.I)) for key, label, rx in CATEGORIES]


def urbex_hit(site: dict) -> str | None:
    kinds = [e.get("kind", "") for e in (site.get("osm") or []) + (site.get("wikidata") or [])]
    for kind in kinds:
        if kind.lower().replace(" ", "_") in URBEX_TYPES:
            return kind.replace("_", " ")
    name = (site.get("name") or "").lower()
    for word in URBEX_NAME_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", name):
            return word
    return None


def dead_name_hit(name: str | None) -> str | None:
    lowered = (name or "").lower()
    for word in DEAD_NAME_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return word
    return None


def tier_for(score: int) -> str:
    for threshold, label in TIERS:
        if score >= threshold:
            return label
    return TIERS[-1][1]


def category_for(site: dict) -> str:
    evidence = (site.get("osm") or []) + (site.get("wikidata") or [])
    text = " ".join([site.get("name") or ""] + [f"{e.get('kind', '')} {e.get('evidence', '')}" for e in evidence])
    for key, _, rx in _CATEGORY_RX:
        if rx.search(text):
            return key
    return OTHER_CATEGORY[0]


def score_site(site: dict) -> tuple[int, list[str]]:
    """Return (score 0-100, human-readable reasons)."""
    score = 0
    reasons: list[str] = []

    osm = site.get("osm") or []
    if osm:
        best = max(osm, key=lambda o: o["weight"])
        score += best["weight"]
        extra = f" (+{len(osm) - 1} more OSM tags nearby)" if len(osm) > 1 else ""
        reasons.append(f"Mapped as dead on OpenStreetMap - {best['evidence']}{extra}")

    wikidata = site.get("wikidata") or []
    if wikidata:
        best = max(wikidata, key=lambda w: w["weight"])
        score += best["weight"] if not osm else best["weight"] // 2
        reasons.append(best["evidence"])
        if best.get("snippet"):
            reasons.append(f"Wikipedia: \"{best['snippet']}\"")

    word = dead_name_hit(site.get("name"))
    if word:
        score += 15
        reasons.append(f"Name contains '{word}'")

    kind = urbex_hit(site)
    if kind:
        score += 10
        reasons.append(f"Explorable building type: {kind}")

    return max(0, min(100, score)), reasons
