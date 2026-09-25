"""Describe a site for people: what it was (category), what state it's in (condition), and the
evidence in plain English. An internal evidence score is kept only to rank and filter:

  - OpenStreetMap evidence (abandoned/disused/ruins tags, old mines, bunkers, dead railway tunnels)
  - Wikidata/Wikipedia evidence (state of use, old mines and quarries, closure dates, intro wording)
  - A second independent source agreeing adds half its weight
  - Small hints: "former"/"derelict" in the name, an explorable building type
"""
from __future__ import annotations

import re

from .config import (
    CATEGORIES,
    DEAD_NAME_WORDS,
    OTHER_CATEGORY,
    STRENGTHS,
    URBEX_NAME_WORDS,
    URBEX_TYPES,
)

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


def strength_for(score: int) -> str:
    for threshold, label in STRENGTHS:
        if score >= threshold:
            return label
    return STRENGTHS[-1][1]


def members_of(site: dict) -> list[dict]:
    return (site.get("osm") or []) + (site.get("wikidata") or []) + (site.get("open") or [])


def category_for(site: dict) -> str:
    evidence = members_of(site)
    text = " ".join([site.get("name") or ""] + [f"{e.get('kind', '')} {e.get('evidence', '')}" for e in evidence])
    for key, _, rx in _CATEGORY_RX:
        if rx.search(text):
            return key
    return OTHER_CATEGORY[0]


def _condition_from(evidence: str) -> str | None:
    text = evidence.lower()
    if "heritage at risk" in text:
        return "At risk"
    if "heritage site" in text:
        return "Heritage site"
    if "new use" in text:
        return "Reused"
    if "single shop unit" in text:
        return "Closed"
    if re.search(r"\bruin", text):
        return "Ruin"
    if re.search(r"abandoned|derelict|boarded up", text):
        return "Abandoned"
    m = re.search(r"closed in (\d{4})", text)
    if m:
        return f"Closed {m.group(1)}"
    if re.search(r"disused|decommission|mothball|out of use|\bclosed\b|vacant|inactive", text):
        return "Disused"
    if "brownfield" in text:
        return "Brownfield"
    if "cave entrance" in text:
        return "Cave"
    if re.search(r"old (mine|quarry|colliery)|adit|mineshaft|mine_shaft", text):
        return "Old workings"
    if re.search(r"bunker|pillbox|observation post|observer corps|monitoring post|roc post", text):
        return "Old military"
    if "former" in text:
        return "Former"
    return None


_NAME_CONDITIONS = {"abandoned": "Abandoned", "derelict": "Abandoned", "disused": "Disused", "defunct": "Disused",
                    "ruin": "Ruin", "ruins": "Ruin", "former": "Former", "closed down": "Closed"}


def condition_for(site: dict) -> str:
    """The state a site is in, judged from its strongest piece of evidence first. Registers often
    say no more than what a place is, so the name has the last word ("Disused Quarry, Cwm Llwyd")."""
    members = sorted(members_of(site), key=lambda m: -m["weight"])
    for m in members:
        found = _condition_from(m.get("evidence", ""))
        if found:
            return found
    return _NAME_CONDITIONS.get(dead_name_hit(site.get("name")) or "", "Historic")


_LIFECYCLE = re.compile(r"^OSM: (abandoned|disused):(\w+)=([^\s(]+)\s*(.*)$")
_SAYS = re.compile(r"^OSM: (\w+) says '(.+)'$")


def describe_osm(evidence: str) -> str:
    """'OSM: disused:amenity=hospital' -> 'OpenStreetMap lists it as a disused hospital'."""
    m = _LIFECYCLE.match(evidence)
    if m:
        state, key, value, rest = m.groups()
        thing = key if value == "yes" else value
        thing = thing.replace("_", " ")
        if key == "shop" and value != "yes":
            thing += " shop"
        article = "an" if state[0] in "aeiou" else "a"
        return f"OpenStreetMap lists it as {article} {state} {thing}{(' ' + rest) if rest else ''}"
    m = _SAYS.match(evidence)
    if m:
        key, word = m.groups()
        return f"Its name says '{word}'" if key == "name" else f"Its OpenStreetMap {key} says '{word}'"
    plain = {
        "OSM: building=ruins": "OpenStreetMap maps it as a ruined building",
        "OSM: building=abandoned": "OpenStreetMap maps it as an abandoned building",
        "OSM: abandoned=yes": "OpenStreetMap tags it as abandoned",
        "OSM: disused=yes": "OpenStreetMap tags it as disused",
        "OSM: landuse=brownfield": "OpenStreetMap maps it as brownfield land",
        "OSM: cave entrance": "OpenStreetMap maps a cave entrance here",
        "OSM: historic=ruins": "OpenStreetMap maps ruins here",
        "OSM: abandoned railway tunnel": "OpenStreetMap maps an abandoned railway tunnel here",
        "OSM: disused railway tunnel": "OpenStreetMap maps a disused railway tunnel here",
        "OSM: old mine or quarry": "OpenStreetMap maps an old mine or quarry here",
        "OSM: military bunker": "OpenStreetMap maps a military bunker here",
        "OSM: former railway station": "OpenStreetMap maps a former railway station here",
    }
    for prefix, text in plain.items():
        if evidence.startswith(prefix):
            return text + evidence[len(prefix):]
    return "OpenStreetMap: " + evidence.removeprefix("OSM: ")


_WIKIDATA = [
    (re.compile(r"^Wikidata: state of use is '(.+)'$"), r"Wikidata records its state of use as '\1'"),
    (re.compile(r"^Wikidata: closed in (\d{4})$"), r"Wikidata says it closed in \1"),
    (re.compile(r"^Wikidata: old (.+)$"), r"Wikidata lists it as an old \1"),
    (re.compile(r"^Wikidata: listed as (.+)$"), r"Wikidata lists it as \1"),
    (re.compile(r"^Wikidata: (.+)$"), r"Wikidata lists it as a \1"),
]


def describe_wikidata(evidence: str) -> list[str]:
    """"Wikidata: old quarry; Wikipedia describes it as disused" -> two plain-English reasons."""
    first, *rest = evidence.split("; ")
    for rx, text in _WIKIDATA:
        if rx.match(first):
            first = rx.sub(text, first)
            break
    return [first, *rest]


def _source_label(key: str | None) -> str:
    from .opendata import DATASETS

    if key in DATASETS:
        return DATASETS[key].label
    return "your own imports" if key == "imported" else str(key)


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def score_site(site: dict) -> tuple[int, list[str]]:
    """Return (internal evidence score 0-100, the evidence in plain English)."""
    score = 0
    reasons: list[str] = []

    osm = site.get("osm") or []
    if osm:
        best = max(osm, key=lambda o: o["weight"])
        score += best["weight"]
        reasons.append(describe_osm(best["evidence"]))
        if len(osm) > 1:
            reasons.append(f"{len(osm) - 1} more OpenStreetMap feature{'s' if len(osm) > 2 else ''} here agree")

    wikidata = site.get("wikidata") or []
    if wikidata:
        best = max(wikidata, key=lambda w: w["weight"])
        score += best["weight"] if not osm else best["weight"] // 2
        reasons.extend(describe_wikidata(best["evidence"]))
        if best.get("snippet"):
            reasons.append(f"Wikipedia: \"{best['snippet']}\"")

    registers = site.get("open") or []
    if registers:
        best = max(registers, key=lambda r: r["weight"])
        score += best["weight"] if not (osm or wikidata) else best["weight"] // 2
        reasons.append(best["evidence"])
        parts = sum(1 for r in registers if r is not best and r.get("source") == best.get("source"))
        if parts:
            reasons.append(f"{_source_label(best.get('source'))} records {parts} more part{'s' if parts > 1 else ''}"
                           " of it")
        others = sorted({r.get("source") for r in registers} - {best.get("source")})
        if others:
            reasons.append("Also recorded by " + _and([_source_label(s) for s in others]))

    # Independent sources agreeing is the strongest evidence there is: each past the second adds some.
    agreeing = ({"osm"} if osm else set()) | ({"wikidata"} if wikidata else set()) \
        | {r.get("source") for r in registers}
    if len(agreeing) > 2:
        score += min(10, 5 * (len(agreeing) - 2))

    word = dead_name_hit(site.get("name"))
    if word:
        score += 15
        if f"Its name says '{word}'" not in reasons:
            reasons.append(f"Its name says '{word}'")

    if urbex_hit(site):
        score += 10  # ranking only: an explorable kind of building beats, say, a closed shop unit

    return max(0, min(100, score)), reasons
