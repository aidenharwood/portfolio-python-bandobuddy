"""Wikidata + Wikipedia: notable disused mines, quarries, tunnels, stations, mills and "former" buildings.

Free, worldwide and keyless; crawled box by box across the UK. It catches places OpenStreetMap doesn't mark as dead - e.g. Gripwood
(Bethel) Quarry near Bradford-on-Avon, an underground Bath stone mine with no OSM tags at all.
Wikipedia intros then confirm ("disused", "now-closed") or rule out ("demolished", "converted to flats").
"""
from __future__ import annotations

import re
from urllib.parse import unquote

import requests

from .config import USER_AGENT

SPARQL_URL = "https://query.wikidata.org/sparql"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
TILE_ROW_LIMIT = 4000  # a box returning this many rows is split, in case some were cut off
INTRO_BATCH = 20  # the extracts API only returns intros for 20 pages per request

# Server-side prefilter, so dense towns (thousands of listed buildings) don't blow the row limit.
NAME_RX = (r"quarr|\bmines?\b|colliery|tunnel|bunker|pillbox|\bfort\b|asylum|sanatori|workhouse|\bmills?\b"
           r"|factory|brewery|\bruins?\b|former|disused|abandoned|derelict|\bcaves?\b|\badit\b|\bshafts?\b")
TYPE_RX = (r"quarr|\bmines?\b|colliery|tunnel|bunker|pillbox|\bfort\b|asylum|sanatori|workhouse|\bmill"
           r"|factory|brewery|ruin|abandon|former|disused|decommission|\bcaves?\b")

# --- classification -------------------------------------------------------------------------

# Any of these types means the thing is gone, or isn't a place at all.
HARD_EXCLUDE = re.compile(
    r"destroyed|demolished|administrative|civil parish|\bvillage\b|hamlet|human settlement|\bdistrict\b"
    r"|\bcompany\b|business|organi[sz]ation|\bbrand\b", re.I)
# Items whose types are *only* these are listed-building clutter (garden walls, gate piers, cottages).
SOFT_EXCLUDE = re.compile(
    r"^(?:wall|retaining wall|gate|gateway|gazebo|monument|memorial|war memorial|baptismal font|bridge"
    r"|cottage|house|detached house|terraced house|farmhouse|pub|shrine|religious site|archaeological site"
    r"|hillfort|contour fort|promontory fort|fortification|cemetery|cemetery chapel|outbuilding|carriage house"
    r"|stable|barn|village lock-up|chimney|grave|tomb|milestone|boundary stone|lamp post|telephone box"
    r"|street|park|garden|well|pump|statue|sculpture|cross)$", re.I)
GONE_STATES = re.compile(r"in use|demolish|destroy|dismantl|removed|planned|under construction", re.I)
DEAD_STATES = re.compile(r"abandon|disused|decommission|derelict|ruin|closed|vacant|mothball|out of use|inactive", re.I)
UNDERGROUND = re.compile(r"\bmines?\b|quarr|colliery|bunker|pillbox|\bcaves?\b|\badit\b|\bshafts?\b|catacomb"
                         r"|air[- ]raid shelter|underground", re.I)
_UNDERGROUND_NAMES = {"quarr": "quarry", "mines": "mine", "caves": "cave", "shafts": "shaft",
                      "air-raid shelter": "air raid shelter"}
TUNNEL = re.compile(r"tunnel", re.I)
STATION = re.compile(r"railway station|train station|\bhalt\b", re.I)
HALT = re.compile(r"\bhalt\b", re.I)
FORMER = re.compile(r"^former\b|\(former|\bformerly\b", re.I)
BUILDINGISH = re.compile(
    r"mill|factory|brewery|\bworks\b|school|hospital|asylum|sanatori|workhouse|chapel|church|cinema|movie theater"
    r"|theatre|station|institute|\bstore\b|warehouse|engine house|drying house|pump(?:ing)? house|boiler house"
    r"|barracks|prison|gaol|hotel|colliery|power station|tunnel", re.I)
MILL_TYPES = re.compile(r"mill|factory|brewery|industrial building|\bworks\b", re.I)

# Wikipedia intro language
WIKI_GONE = re.compile(
    r"\b(?:was demolished|were demolished|has been demolished|have been demolished|since been demolished"
    r"|was destroyed|no longer exists|nothing (?:now )?remains|no trace (?:now )?remains|has been redeveloped"
    r"|was built over)\b", re.I)
WIKI_REUSED = re.compile(
    r"\bnow (?:a|an|the) (?:private )?(?:house|home|residence|dwelling|flats|apartments|offices|hotel|museum"
    r"|restaurant|pub|shop|cycle ?path|cycleway|greenway|footpath)\b|\bconverted (?:in)?to (?:flats|apartments"
    r"|housing|homes|offices|a house|a hotel|a museum|residential)|\breopened\b"
    r"|\bnow (?:in )?private (?:hands|ownership|use|residence|house)\b", re.I)
WIKI_ACTIVE = re.compile(
    r"\b(?:working|active|operational) (?:quarry|mine)\b|\bstill in use\b|\bremains in use\b|\bis operated by\b"
    r"|\bcurrently (?:operated|used)\b", re.I)
WIKI_DEAD = re.compile(
    r"\b(?:disused|abandoned|derelict|now[- ]closed|closed down|ruined|ruins of|lies in ruins"
    r"|vacant|mothballed|decommissioned|out of use|no longer used|fell into disuse"
    r"|was an? (?:railway station|station|school|hospital|mine|quarry|factory|mill|brewery|cinema|church|chapel"
    r"|colliery|asylum|workhouse))\b", re.I)


def classify(label: str, types: list[str], states: list[str], ended: str | None) -> tuple[str, int, str] | None:
    """Return (evidence, weight, kind) for a Wikidata item, or None if it isn't bando material."""
    types = [t for t in types if t]
    kind = next((t for t in types if not SOFT_EXCLUDE.match(t) and t != "protected area"), types[0] if types else "")
    joined = " | ".join(types)
    if HARD_EXCLUDE.search(joined):
        return None
    if types and all(SOFT_EXCLUDE.match(t) for t in types):
        return None
    if any(GONE_STATES.search(s) for s in states):
        return None
    text = f"{label} | {joined}"
    dead_state = next((s for s in states if DEAD_STATES.search(s)), None)
    if dead_state:
        evidence = f"Wikidata: state of use is '{dead_state}'"
        if STATION.search(joined):
            # Most closed stations, and almost all halts, were cleared decades ago.
            return evidence, 10 if HALT.search(label) else 20, kind or "railway station"
        return evidence, 30, kind or "structure"

    m = UNDERGROUND.search(text)
    if m:
        word = _UNDERGROUND_NAMES.get(m.group(0).lower(), m.group(0).lower())
        return f"Wikidata: old {word}", 25, kind or word
    if ended and BUILDINGISH.search(text):
        return f"Wikidata: closed in {ended[:4]}", 20, kind or "building"
    if FORMER.search(label) and BUILDINGISH.search(text):
        # Listed "former" chapels, schools and stores are usually converted, so this is only a hint.
        return "Wikidata: listed as a former building", 10, kind or "building"
    if TUNNEL.search(text):
        return "Wikidata: tunnel", 10, kind or "tunnel"
    if MILL_TYPES.search(joined):
        return "Wikidata: historic mill or factory", 10, kind
    return None


def judge_intro(extract: str) -> tuple[int, str | None, str | None]:
    """Score a Wikipedia intro: (weight change, reason, quoted sentence). -999 means "it's gone"."""
    sentences = re.split(r"(?<=[.!?])\s+", extract.replace("\n", " "))

    def quote(rx: re.Pattern) -> str | None:
        for s in sentences:
            if rx.search(s):
                return s.strip()[:220]
        return None

    if quote(WIKI_GONE):
        return -999, "Wikipedia says it's gone", quote(WIKI_GONE)
    delta, reason, snippet = 0, None, None
    dead = quote(WIKI_DEAD)
    if dead:
        delta, reason, snippet = 15, "Wikipedia describes it as disused", dead
    active = quote(WIKI_ACTIVE)
    if active:
        delta, reason, snippet = delta - 20, "Wikipedia says it's still in use", active
    reused = quote(WIKI_REUSED)
    if reused:
        delta -= 15
        if not active:
            reason, snippet = "Wikipedia says it has a new use", reused
    return delta, reason, snippet


# --- fetching -----------------------------------------------------------------------------

class TileTooBig(Exception):
    """The query timed out or hit the row limit: split the box and try the quarters."""


class ServiceBusy(Exception):
    def __init__(self, message: str, retry_after: float = 30):
        super().__init__(message)
        self.retry_after = retry_after


def _sparql_literal(rx: str) -> str:
    return '"' + rx.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_tile_query(s: float, w: float, n: float, e: float, limit: int = TILE_ROW_LIMIT) -> str:
    """Candidate items inside a lat/lng box that belong to the UK (P17 = Q145)."""
    return f"""
SELECT ?item ?label ?coord
       (GROUP_CONCAT(DISTINCT ?typeLabel; separator="|") AS ?types)
       (GROUP_CONCAT(DISTINCT ?stateLabel; separator="|") AS ?states)
       (SAMPLE(?end) AS ?ended) (SAMPLE(?article) AS ?wiki)
WHERE {{
  SERVICE wikibase:box {{
    ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:cornerSouthWest "Point({w:.6f} {s:.6f})"^^geo:wktLiteral .
    bd:serviceParam wikibase:cornerNorthEast "Point({e:.6f} {n:.6f})"^^geo:wktLiteral .
  }}
  ?item wdt:P17 wd:Q145 .
  ?item rdfs:label ?label . FILTER(LANG(?label) = "en")
  OPTIONAL {{ ?item wdt:P31 ?type . ?type rdfs:label ?typeLabel . FILTER(LANG(?typeLabel) = "en") }}
  OPTIONAL {{ ?item wdt:P5817 ?state . ?state rdfs:label ?stateLabel . FILTER(LANG(?stateLabel) = "en") }}
  OPTIONAL {{ ?item wdt:P576|wdt:P3999 ?end . }}
  OPTIONAL {{ ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . }}
  FILTER(REGEX(?label, {_sparql_literal(NAME_RX)}, "i") || BOUND(?state) || BOUND(?end)
         || EXISTS {{ ?item wdt:P31/rdfs:label ?tl . FILTER(LANG(?tl) = "en" && REGEX(?tl, {_sparql_literal(TYPE_RX)}, "i")) }})
}}
GROUP BY ?item ?label ?coord
LIMIT {limit}
"""


_POINT = re.compile(r"Point\(\s*([-\d.eE]+)\s+([-\d.eE]+)\s*\)")


def fetch_tile(s: float, w: float, n: float, e: float, session: requests.Session) -> list[dict]:
    """Raw candidate rows for one box: {qid, label, lat, lng, types, states, ended, wiki}."""
    try:
        resp = session.get(
            SPARQL_URL,
            params={"query": build_tile_query(s, w, n, e), "format": "json"},
            headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
            timeout=75,
        )
    except requests.Timeout:
        raise TileTooBig("query took too long")
    except requests.RequestException as exc:
        raise ServiceBusy(f"Wikidata unreachable: {type(exc).__name__}")
    if resp.status_code == 429:
        raise ServiceBusy("Wikidata rate limit", float(resp.headers.get("Retry-After") or 60))
    if resp.status_code in (500, 502, 503, 504) and ("TimeoutException" in resp.text or resp.status_code == 504):
        raise TileTooBig("query timed out on the server")
    if resp.status_code != 200:
        raise ServiceBusy(f"Wikidata query failed: HTTP {resp.status_code}")
    try:
        bindings = resp.json()["results"]["bindings"]
    except (ValueError, KeyError):
        raise ServiceBusy("Wikidata returned an unreadable response")
    if len(bindings) >= TILE_ROW_LIMIT:
        raise TileTooBig("too many results for one query")

    rows: dict[str, dict] = {}
    for b in bindings:
        m = _POINT.search(b.get("coord", {}).get("value", ""))
        if not m:
            continue
        qid = b["item"]["value"].rsplit("/", 1)[1]
        rows.setdefault(qid, {
            "qid": qid,
            "label": b["label"]["value"],
            "lat": float(m.group(2)),
            "lng": float(m.group(1)),
            "types": [t for t in b.get("types", {}).get("value", "").split("|") if t],
            "states": [x for x in b.get("states", {}).get("value", "").split("|") if x],
            "ended": b.get("ended", {}).get("value") or None,
            "wiki": b.get("wiki", {}).get("value") or None,
        })
    return list(rows.values())


def wikipedia_title(wiki_url: str | None) -> str | None:
    return unquote(wiki_url.rsplit("/", 1)[1]).replace("_", " ") if wiki_url else None


def evaluate(row: dict, intro: str | None) -> dict | None:
    """Turn a raw Wikidata row (plus its Wikipedia intro, if fetched) into site evidence, or None."""
    verdict = classify(row["label"], row["types"], row["states"], row["ended"])
    if not verdict:
        return None
    evidence, weight, kind = verdict
    snippet = None
    if intro:
        delta, reason, snippet = judge_intro(intro)
        weight += delta
        if reason:
            evidence += f"; {reason}"
    if weight <= 0:
        return None
    return {
        "qid": row["qid"],
        "name": row["label"],
        "kind": kind,
        "lat": row["lat"],
        "lng": row["lng"],
        "evidence": evidence,
        "weight": weight,
        "url": f"https://www.wikidata.org/wiki/{row['qid']}",
        "wikipedia_url": row.get("wiki"),
        "snippet": snippet,
    }


def fetch_intros(titles: list[str], session: requests.Session) -> dict[str, str]:
    """Plain-text Wikipedia intros for up to INTRO_BATCH titles, keyed by the title asked for."""
    try:
        data = session.get(
            WIKIPEDIA_API,
            params={"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
                    "exsentences": 5, "redirects": 1, "format": "json", "titles": "|".join(titles)},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        ).json()
    except (requests.RequestException, ValueError):
        return {}
    query = data.get("query") or {}
    rename = {r["to"]: r["from"] for r in query.get("redirects", []) + query.get("normalized", [])}
    wanted = set(titles)
    out = {}
    for page in (query.get("pages") or {}).values():
        title = page.get("title", "")
        while title not in wanted and title in rename:
            title = rename[title]
        if title in wanted:
            out[title] = page.get("extract") or ""
    return out
