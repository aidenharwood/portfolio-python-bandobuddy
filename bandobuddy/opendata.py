"""Open national registers: places OpenStreetMap and Wikidata miss.

Every dataset here is free to reuse with attribution, and each one says where to fetch it, which
records are worth keeping and how strong that evidence is. The updater treats them all the same,
so adding a register is a matter of describing it here.

A record in a national register means "this exists (or existed)", not "this is abandoned", so most
of them are weak leads. Military and underground records are the exception: an observation post or
a colliery shaft is disused by definition.
"""
from __future__ import annotations

import codecs
import csv
import io
import os
import re
import threading
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Iterator
from urllib.parse import urljoin

import requests

from .config import USER_AGENT
from .geo import bng_to_wgs84
from .osm import Cancelled

PAGE = 1000
TIMEOUT = 120
Progress = Callable[[str, int, "int | None"], None]
Records = Iterator[dict]


def _get(session: requests.Session, url: str, params: dict, post: bool = False) -> dict:
    headers = {"User-Agent": USER_AGENT}
    # POST for ArcGIS: a batch of object ids makes for a URL longer than servers accept.
    resp = (session.post(url, data=params, headers=headers, timeout=TIMEOUT) if post
            else session.get(url, params=params, headers=headers, timeout=TIMEOUT))
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"{url} returned something that isn't JSON")
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"{url}: {data['error'].get('message', data['error'])}")
    return data


def _stop(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def _features(session: requests.Session, url: str, params: dict) -> list[dict]:
    return _get(session, f"{url}/query", {"outSR": "4326", "f": "json", **params}, post=True).get("features") or []


def _points(features: list[dict]) -> Records:
    """Where each record is. Layers hand this over as a point, or (Canmore) a multipoint."""
    for feature in features:
        geometry = feature.get("geometry") or {}
        if geometry.get("x") is not None:
            lng, lat = geometry["x"], geometry["y"]
        elif geometry.get("points"):
            lng, lat = geometry["points"][0][:2]
        else:
            continue
        yield {**feature.get("attributes", {}), "lat": lat, "lng": lng}


@dataclass
class ArcGIS:
    """A feature layer that pages through its results (Historic England and most others)."""

    url: str
    where: str = "1=1"
    fields: str = "*"
    batch: int = PAGE

    def __call__(self, session: requests.Session, progress: Progress, cancel=None) -> Records:
        total = _get(session, f"{self.url}/query",
                     {"where": self.where, "returnCountOnly": "true", "f": "json"}).get("count")
        progress("downloading", 0, total)
        done, offset = 0, 0
        while True:
            _stop(cancel)
            features = _features(session, self.url, {"where": self.where, "outFields": self.fields,
                                                     "resultOffset": offset, "resultRecordCount": self.batch})
            for row in _points(features):
                yield row
                done += 1
            progress("downloading", done, total)
            if len(features) < self.batch:
                return
            offset += self.batch


@dataclass
class ArcGISByIds:
    """A layer that won't page (Canmore). One query per search term to collect object ids - the
    server takes too long over a single query with every term in it - then fetch them in batches."""

    url: str
    wheres: list[str]
    fields: str = "*"
    batch: int = 400

    def __call__(self, session: requests.Session, progress: Progress, cancel=None) -> Records:
        ids: list[int] = []
        seen = set()
        for i, where in enumerate(self.wheres, 1):
            _stop(cancel)
            progress(f"looking up records ({i}/{len(self.wheres)})", 0, None)
            for oid in _get(session, f"{self.url}/query",
                            {"where": where, "returnIdsOnly": "true", "f": "json"}).get("objectIds") or []:
                if oid not in seen:
                    seen.add(oid)
                    ids.append(oid)
        ids.sort()
        progress("downloading", 0, len(ids))
        done = 0
        for start in range(0, len(ids), self.batch):
            _stop(cancel)
            batch = ids[start:start + self.batch]
            for row in _points(_features(session, self.url, {"objectIds": ",".join(str(i) for i in batch),
                                                             "outFields": self.fields})):
                yield row
                done += 1
            progress("downloading", done, len(ids))


@dataclass
class WFS:
    """A GeoServer WFS layer (DataMap Wales)."""

    url: str
    layer: str
    batch: int = PAGE

    def __call__(self, session: requests.Session, progress: Progress, cancel=None) -> Records:
        params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeName": self.layer,
                  "outputFormat": "application/json", "count": self.batch}
        total, done, start = None, 0, 0
        while True:
            _stop(cancel)
            page = _get(session, self.url, {**params, "startIndex": start})
            total = total or page.get("totalFeatures") or page.get("numberMatched")
            features = page.get("features") or []
            for feature in features:
                props = feature.get("properties") or {}
                try:  # a few rows carry spreadsheet leftovers ("#VALUE!") instead of a position
                    lat, lng = float(props.get("lat")), float(props.get("long"))
                except (TypeError, ValueError):
                    continue
                yield {**props, "lat": lat, "lng": lng}
                done += 1
            progress("downloading", done, total if isinstance(total, int) else None)
            if len(features) < self.batch:
                return
            start += self.batch


@dataclass
class PlanningData:
    """planning.data.gov.uk, which collects the registers English councils publish."""

    dataset: str
    url: str = "https://www.planning.data.gov.uk/entity.json"
    batch: int = 500

    def __call__(self, session: requests.Session, progress: Progress, cancel=None) -> Records:
        done, offset = 0, 0
        while True:
            _stop(cancel)
            entities = _get(session, self.url, {"dataset": self.dataset, "limit": self.batch,
                                                "offset": offset}).get("entities") or []
            for row in entities:
                point = _point(row.get("point"))
                if point:
                    yield {**row, "lat": point[0], "lng": point[1]}
                    done += 1
            progress("downloading", done, None)
            if len(entities) < self.batch:
                return
            offset += self.batch


def _lines(resp, encoding: str) -> Iterator[str]:
    """A big text download, a line at a time, without holding it all."""
    pending = ""
    for chunk in codecs.iterdecode(resp.iter_content(1 << 16), encoding):
        pending += chunk
        *done, pending = pending.split("\n")
        for line in done:
            yield line + "\n"
    if pending:
        yield pending


def _grid_cell(e: float, n: float, size: float) -> tuple[int, int]:
    return int(e // size), int(n // size)


@dataclass
class SchoolsRegister:
    """Get Information about Schools (DfE): every school in England, open or closed, as one daily CSV
    of about 65 MB, read as it arrives. A closed school is only kept when no open school stands on the
    same spot (an academy carrying on in the same building), and one record per site: the latest."""

    url: str = "https://ea-edubase-api-prod.azurewebsites.net/edubase/downloads/public/edubasealldata{day}.csv"
    still_open_m: float = 100
    same_site_m: float = 60

    def __call__(self, session: requests.Session, progress: Progress, cancel=None) -> Records:
        resp = None
        for back in range(8):          # published every morning; fall back a few days if it isn't out yet
            day = (date.today() - timedelta(days=back)).strftime("%Y%m%d")
            r = session.get(self.url.format(day=day), headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
                            stream=True)
            if r.status_code == 200:
                resp = r
                break
        if resp is None:
            raise RuntimeError("Get Information about Schools hasn't published a download in the last week")
        open_cells: dict[tuple[int, int], list[tuple[float, float]]] = {}
        closed: list[dict] = []
        for i, row in enumerate(csv.DictReader(_lines(resp, "cp1252"))):
            if i % 5000 == 0:
                _stop(cancel)
                progress("reading the register", i, None)
            try:
                e, n = float(row.get("Easting") or 0), float(row.get("Northing") or 0)
            except ValueError:
                continue
            if not e or not n:
                continue
            if row.get("EstablishmentStatus (name)") != "Closed":
                open_cells.setdefault(_grid_cell(e, n, self.still_open_m), []).append((e, n))
                continue
            closed.append({k: row.get(k) or "" for k in SCHOOL_FIELDS} | {"e": e, "n": n})

        def near(cells, e, n, metres):
            ci, cj = _grid_cell(e, n, metres)
            return any((e - x) ** 2 + (n - y) ** 2 <= metres ** 2
                       for di in (-1, 0, 1) for dj in (-1, 0, 1) for x, y in cells.get((ci + di, cj + dj), ()))

        closed = [c for c in closed if not near(open_cells, c["e"], c["n"], self.still_open_m)]
        closed.sort(key=lambda c: _school_closed(c) or date.min, reverse=True)
        kept: dict[tuple[int, int], list[tuple[float, float]]] = {}
        latest: list[dict] = []
        for c in closed:   # infant and junior schools that closed on one site are one place
            if near(kept, c["e"], c["n"], self.same_site_m):
                continue
            kept.setdefault(_grid_cell(c["e"], c["n"], self.same_site_m), []).append((c["e"], c["n"]))
            latest.append(c)
        for c in latest:
            lat, lng = bng_to_wgs84(c["e"], c["n"])
            yield {**c, "lat": lat, "lng": lng}
        progress("reading the register", len(latest), len(latest))


_ODS = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_ODS_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p"


def ods_rows(data: bytes, sheet: str) -> list[list[str]]:
    """The cells of one sheet of an OpenDocument spreadsheet, as text."""
    root = ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("content.xml"))
    table = next((t for t in root.iter(f"{_ODS}table") if t.get(f"{_ODS}name") == sheet), None)
    if table is None:
        raise RuntimeError(f"The spreadsheet has no '{sheet}' sheet any more")
    rows = []
    for row in table.iter(f"{_ODS}table-row"):
        cells: list[str] = []
        blanks = 0      # held back: a row ends with blank cells "repeated" thousands of times
        for cell in row:
            if not cell.tag.endswith("table-cell"):
                continue
            text = "\n".join("".join(p.itertext()) for p in cell.iter(_ODS_TEXT))
            repeat = int(cell.get(f"{_ODS}number-columns-repeated", "1"))
            if not text:
                blanks += repeat
                continue
            cells.extend([""] * blanks + [text] * repeat)
            blanks = 0
        rows.append(cells)
    return rows


@dataclass
class ScotlandDerelictLand:
    """The Scottish Vacant and Derelict Land Survey's site register: one spreadsheet a year, linked
    from its publication page (the file name changes with the year)."""

    page: str = "https://www.gov.scot/publications/the-scottish-vacant-and-derelict-land-survey-site-register/"
    sheet: str = "Site_Register"

    def __call__(self, session: requests.Session, progress: Progress, cancel=None) -> Records:
        headers = {"User-Agent": USER_AGENT}
        resp = session.get(self.page, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        link = re.search(r'href="([^"]+\.ods)"', resp.text)
        if not link:
            raise RuntimeError("The site register's spreadsheet isn't linked from its page any more")
        _stop(cancel)
        resp = session.get(urljoin(self.page, link.group(1)), headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        rows = ods_rows(resp.content, self.sheet)
        at = next((i for i, r in enumerate(rows) if "Site Code" in r), None)
        if at is None:
            raise RuntimeError("The site register's columns have changed")
        header = rows[at]
        done = 0
        for cells in rows[at + 1:]:
            row = dict(zip(header, cells))
            try:
                lat, lng = bng_to_wgs84(float(row.get("East") or 0), float(row.get("North") or 0))
            except ValueError:
                continue
            if not row.get("Site Code"):
                continue
            yield {**row, "lat": lat, "lng": lng}
            done += 1
        progress("reading the register", done, done)


# Words that make a demolition worth knowing about: the building's falling down, or empty.
RUNDOWN_WORDS = ("derelict", "dilapidated", "disused", "vacant", "redundant", "fire damaged", "abandoned", "ruinous",
                 "unsafe", "dangerous structure", "empty", "former")


def _planit_search(words=RUNDOWN_WORDS) -> str:
    """PlanIt reads "a b or c d" as (a and b) or (c and d): demolition next to one of the words, in
    either form ("demolition" and "demolish" don't share a stem)."""
    said = [f'"{w}"' if " " in w else w for w in words]
    return " or ".join(f"{verb} {w}" for w in said for verb in ("demolition", "demolish"))


@dataclass
class PlanIt:
    """UK PlanIt: planning applications scraped from council websites by one person, who asks that the
    API isn't hit more than about once a minute and isn't used to hoover up its data. So this asks only
    for applications to demolish something described as derelict, empty or redundant, made in the last
    fortnight, and then for decisions in the last fortnight on any made earlier: a page or two each, a
    minute apart. Run weekly, it builds up a picture as it goes; it never backfills.

    (Asking instead for every demolition application whose details changed lately found 4,756 in two
    days, mostly council sites being re-read: hours of pages a week, and past PlanIt's 5,000 limit.)"""

    url: str = "https://www.planit.org.uk/api/applics/json"
    search: str = _planit_search()
    days: int = 14                 # a fortnight: a weekly run with a week to spare
    windows: tuple = ("recent", "decided")   # made lately, and decided lately
    batch: int = 300
    gap_s: float = 61

    FIELDS = ("name,uid,description,address,postcode,app_state,app_size,start_date,decided_date,"
              "location_x,location_y,link,url,area_name")

    def _wait(self, cancel, seconds: float) -> None:
        if (cancel or threading.Event()).wait(seconds):
            raise Cancelled()

    def _ask(self, session: requests.Session, params: dict, cancel) -> dict:
        for attempt in range(3):
            resp = session.get(self.url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            if resp.status_code != 429:
                resp.raise_for_status()
                return resp.json()
            try:   # too soon: wait as long as it says, and no less than our own gap
                wait = max(self.gap_s, float(resp.headers.get("Retry-After") or 0))
            except ValueError:
                wait = self.gap_s
            self._wait(cancel, wait)
        raise RuntimeError("PlanIt kept asking us to slow down; trying again next time")

    def __call__(self, session: requests.Session, progress: Progress, cancel=None) -> Records:
        asked = False
        for window in self.windows:
            page, done = 1, 0
            while True:
                _stop(cancel)
                if asked:
                    self._wait(cancel, self.gap_s)   # a minute between any two requests
                asked = True
                data = self._ask(session, {"search": self.search, window: self.days, "pg_sz": self.batch,
                                           "page": page, "select": self.FIELDS, "sort": "-start_date",
                                           "compress": "on"}, cancel)
                records = data.get("records") or []
                total = data.get("total")
                for row in records:
                    if row.get("location_x") is None or row.get("location_y") is None:
                        continue
                    yield {**row, "lat": float(row["location_y"]), "lng": float(row["location_x"])}
                done += len(records)
                progress(f"{'new applications' if window == 'recent' else 'decisions'}, a minute between pages",
                         done, total if isinstance(total, int) else None)
                if len(records) < self.batch or (isinstance(total, int) and done >= total):
                    break
                page += 1


_POINT = re.compile(r"POINT\s*\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s*\)", re.I)


def _point(wkt: str | None) -> tuple[float, float] | None:
    m = _POINT.match((wkt or "").strip())
    return (float(m.group(2)), float(m.group(1))) if m else None


# -- judging records ---------------------------------------------------------------------------

# What a record is, and what it's worth as a lead. First match wins.
RECORD_TYPES = [
    (r"observation post|observer corps|\broc\b post|monitoring post", 25, "observation post"),
    (r"bunker|pillbox|blockhouse|gun emplacement|searchlight|anti[- ]aircraft|battery|air raid shelter", 25,
     "military structure"),
    (r"colliery|coal mine|\bmines?\b|mine shaft|mineshaft|\badit\b|quarry|quarries|limekiln|lime kiln", 22,
     "old workings"),
    (r"airfield|aerodrome|hangar|airship", 20, "airfield"),
    (r"tunnel|viaduct|railway station|signal box|engine shed|goods shed", 15, "railway structure"),
    (r"\bmills?\b|factory|\bworks\b|foundry|brewery|distillery|maltings|engine house|brickworks|gasworks"
     r"|pumping station|kiln|colliery", 15, "industrial building"),
    # A register listing a church or a big house says nothing about whether it's still in use.
    (r"asylum|sanatorium|workhouse|hospital|prison|barracks", 12, "institution"),
    (r"country house|mansion|castle|tower house|\bfolly\b", 10, "big house"),
]
_RECORD_TYPES = [(re.compile(rx, re.I), weight, kind) for rx, weight, kind in RECORD_TYPES]
# Something still stands, and it's a wreck: worth a little more.
_WRECK = re.compile(r"remains of|\bruin|derelict|disused|abandoned|former", re.I)
# Nothing left to see. "Site" on its own isn't enough: a "decoy site" or "mine site" is a place, not
# an absence.
_GONE = re.compile(r"\bsite of\b|\(site\)|demolish|destroyed|\bremoved\b|no longer extant|nothing (now )?remains"
                   r"|\blevelled\b|built over|\bobliterated\b", re.I)


# Words that say what a part of a name *is*: "Ackergill, Quadrant Observation Tower" is about the tower.
_FEATURE = re.compile("|".join(rx for rx, _, _ in RECORD_TYPES)
                      + r"|\btower\b|\bhall\b|\bstation\b|\bcamp\b|\bpit\b|\bincline\b|tramway|ropeway"
                      + r"|\btrack\b|\bbridge\b|chapel|church|\bhouse\b", re.I)
_SMALL_WORDS = {"of", "the", "and", "on", "in", "at", "by", "upon", "y", "yr"}
_ROMAN = re.compile(r"(?:i{1,3}|iv|vi{0,3}|ix|x)", re.I)
_ACRONYMS = {"roc", "raf", "mod", "nhs", "rc", "usaf", "ymca", "ywca", "gpo", "lms", "gwr", "lner"}


def _cased(word: str, first: bool) -> str:
    low = word.lower()
    if low in _ACRONYMS or (_ROMAN.fullmatch(low) and not first):
        return low.upper()
    if low in _SMALL_WORDS and not first:
        return low
    return low[:1].upper() + low[1:]


def tidy_name(name: str, shouting: bool = False) -> str:
    """Make a register's name read like a name. Canmore shouts ("LOCH OF BRECK, NORSE MILL") and
    both registers lead with the parish; lead with the thing instead, then where it is:
    "Coetgae, Abertillery, Former Opencast Mine" -> "Former Opencast Mine, Abertillery"."""
    name = re.sub(r"\s*\[[^\]]*\]", "", name or "")        # "[Disused]" is the condition, not the name
    if shouting:
        # Any letters (CAFÉ), and either apostrophe (VICTORIA’S).
        name = re.sub(r"[^\W\d_]+(?:['’][^\W\d_]+)?", lambda m: _cased(m.group(0), m.start() == 0), name.strip())
    parts = [p.strip() for p in re.sub(r"\s+,", ",", name).split(",") if p.strip()]
    feature = next((i for i in range(len(parts) - 1, -1, -1) if _FEATURE.search(parts[i])), None)
    if feature:                                              # found, and not already first
        parts = [parts[feature], parts[feature - 1]]
    return ", ".join(parts)


def sounds_gone(text: str) -> bool:
    """Does a record say the place isn't there any more?"""
    return bool(_GONE.search(text))


def judge_segments(site_type: str, name: str = "") -> tuple[int, str, str] | None:
    """Registers often list everything ever recorded on a spot ("FARMSTEAD (18TH CENTURY),
    OBSERVATION POST (20TH CENTURY)"). Take the first part that interests us, without its dates."""
    for raw in re.split(r"[,;]", site_type):
        verdict = judge_record(raw, name)      # with its brackets: "(SITE OF)" matters
        if verdict:
            return verdict[0], verdict[1], re.sub(r"\([^)]*\)", "", raw).strip().lower()
    return None


def judge_record(site_type: str, name: str = "") -> tuple[int, str] | None:
    """(weight, kind) for a register's own description of a place, or None if it isn't our sort of
    thing or isn't there any more. The name only says whether it's a wreck, or gone."""
    said = f"{site_type} {name}"
    if sounds_gone(said):
        return None
    for rx, weight, kind in _RECORD_TYPES:
        if rx.search(site_type):
            return (weight + 8 if _WRECK.search(said) else weight), kind
    return None


@dataclass
class Dataset:
    key: str
    label: str
    licence: str
    attribution: str
    home: str                     # the human page, for the credits
    fetch: Callable[..., Records]
    judge: Callable[[dict], dict | None]
    incremental: bool = False     # each run brings only what's new, so nothing it leaves out has gone
    opt_in: bool = False          # off unless BANDOBUDDY_<KEY>=1, or run by hand with `update --source`

    def enabled(self) -> bool:
        if not self.opt_in:
            return True
        return os.environ.get(f"BANDOBUDDY_{self.key.upper()}", "").strip().lower() in ("1", "true", "yes", "on")


def _har(row: dict) -> dict | None:
    kinds = {"Listed Building": ("listed building", 25), "Scheduled Monument": ("scheduled monument", 15)}
    if row.get("HeritageCa") not in kinds:  # conservation areas and parks are whole districts, not places
        return None
    kind, weight = kinds[row["HeritageCa"]]
    name = (row.get("EntryName") or "").strip()
    return {
        "ref": str(row.get("List_Entry") or row.get("uid") or "").strip(),
        "name": name,
        "kind": kind,
        "evidence": f"Historic England has it on the Heritage at Risk register ({kind})",
        "weight": weight,
        "url": row.get("URL"),
    }


_BUILT = re.compile(r"complete|completed|built out|under construction|now built|development finished", re.I)


def _brownfield(row: dict) -> dict | None:
    name = (row.get("site-address") or row.get("name") or "").strip()
    notes = (row.get("notes") or "").strip()
    if _BUILT.search(notes):  # the register keeps sites after they're developed
        return None
    return {
        "ref": str(row.get("entity") or row.get("reference") or "").strip(),
        "name": name.split(",")[0][:120] if name else "",
        "kind": "brownfield land",
        "evidence": "On the council's brownfield land register" + (f": {notes[:120]}" if notes else ""),
        "weight": 10,
        "url": row.get("site-plan-url") or None,
    }


def _canmore(row: dict) -> dict | None:
    raw = (row.get("NMRSNAME") or "").strip()
    verdict = judge_segments(row.get("SITETYPE") or "", raw)
    name = tidy_name(raw, shouting=True)
    aliases = [tidy_name(a, shouting=True) for a in (row.get("ALTNAME") or "").split(";") if a.strip()]
    if not verdict:
        return None
    weight, kind, what = verdict
    return {
        "ref": str(row.get("CANMOREID") or row.get("SITENUMBER") or "").strip(),
        "name": name,
        "kind": kind,
        "evidence": f"Canmore records {_a(what)} here",
        "aliases": [a for a in aliases if a and a != name],
        "weight": weight,
        "url": row.get("URL"),
    }


def _coflein(row: dict) -> dict | None:
    raw = (row.get("name") or "").strip()
    verdict = judge_segments(row.get("site_type") or "", raw)
    name = tidy_name(raw)
    if not verdict:
        return None
    weight, kind, what = verdict
    return {
        "ref": str(row.get("nprn") or "").strip(),
        "name": name,
        "kind": kind,
        "evidence": f"Coflein records {_a(what)} here",
        "weight": weight,
        "url": row.get("url"),
    }


SCHOOL_FIELDS = ("URN", "EstablishmentName", "TypeOfEstablishment (name)", "PhaseOfEducation (name)",
                 "ReasonEstablishmentClosed (name)", "CloseDate", "Town")
# Closed, as in the building stopped being a school. Not conversions to academies or new sponsors (the
# school carries on under a new number), and not records closed in error.
SCHOOL_CLOSURES = {"Closure", "Close Nursery School", "Result of Amalgamation/Merger", "Not applicable", "",
                   "Fresh Start", "De-registered", "Does not meet criteria for registration"}
# Not a building in England, or not a school building of its own.
SCHOOL_ELSEWHERE = {"Welsh establishment", "Offshore schools", "Service children's education",
                    "Higher education institutions", "Miscellaneous", "Online provider", "British schools overseas",
                    "Institution funded by other government department"}


def _school_closed(row: dict) -> date | None:
    try:
        day, month, year = (int(x) for x in (row.get("CloseDate") or "").split("-"))
        closed = date(year, month, day)
    except ValueError:
        return None
    return closed if closed.year >= 1950 else None   # 1900 means "not recorded"


def _school(row: dict) -> dict | None:
    if row.get("TypeOfEstablishment (name)") in SCHOOL_ELSEWHERE \
            or row.get("ReasonEstablishmentClosed (name)") not in SCHOOL_CLOSURES:
        return None
    closed = _school_closed(row)
    years = (date.today() - closed).days / 365.25 if closed else None
    # Recently closed schools are the ones most likely to be standing empty; long-closed ones have
    # usually been sold, converted or knocked down.
    weight = 22 if years is not None and years <= 5 else 15 if years is not None and years <= 15 else 8
    kinds = f"{row.get('TypeOfEstablishment (name)', '')} {row.get('PhaseOfEducation (name)', '')}".lower()
    kind = ("nursery school" if "nursery" in kinds else "special school" if "special" in kinds
            else "school (pupil referral unit)" if "referral" in kinds else "school")
    merged = row.get("ReasonEstablishmentClosed (name)") == "Result of Amalgamation/Merger"
    when = f"closed in {closed.year}" if closed else "closed"
    return {
        "ref": str(row.get("URN") or "").strip(),
        "name": (row.get("EstablishmentName") or "").strip(),
        "kind": kind,
        "evidence": f"The Department for Education records it as {when}"
                    + (", merged into another school" if merged else ""),
        "weight": weight,
        "url": f"https://get-information-schools.service.gov.uk/Establishments/Establishment/Details/{row.get('URN')}",
    }


# What a Scottish derelict site used to be: (kind, how much more interesting that makes it).
VDL_USES = {
    "Defence": ("military site", 8), "Mineral Activity": ("old workings", 5), "Manufacturing": ("industrial site", 5),
    "Other General Industry": ("industrial site", 4), "Education": ("school", 5),
    "Community & Health": ("hospital or community building", 5), "Residential - Hotels, Hostels etc": ("hotel", 5),
    "Recreation & Leisure": ("leisure site", 3), "Transport": ("transport depot", 3), "Utilities": ("utility works", 3),
    "Offices": ("offices", 2), "Retailing": ("shops", 0), "Storage": ("warehouse or storage yard", 0),
    "Agriculture": ("farm buildings", 0), "Residential - Housing": ("residential buildings", 0),
}


def _vdl(row: dict) -> dict | None:
    site_type = (row.get("Site Type") or "").strip()
    use = (row.get("Previous Use of Site") or "").strip()
    kind, bonus = VDL_USES.get(use, ("site", 0))
    if site_type == "Vacant Land":
        weight = 6       # cleared ground: nothing standing, usually
    elif "Buildings" in site_type:
        weight = 22 + bonus
    else:                # derelict: damaged land, sometimes with ruins on it
        weight = 16 + bonus
    since = (row.get("Period when site became Vacant or Derelict") or "").strip()
    # Said so the condition comes out right: derelict, empty buildings, or a cleared plot.
    state = ("it as cleared vacant land" if site_type == "Vacant Land"
             else "its buildings as vacant" if "Buildings" in site_type else "it as derelict")
    said = state + (f" since {since}" if since and since != "Unknown" else "") \
        + (f", previously {use.lower()}" if use and use not in ("Unknown", "Other") else "")
    name = tidy_name(row.get("Site Name (If Supplied)") or "", shouting=True) \
        or tidy_name((row.get("Address (If Supplied)") or "").split(",")[0], shouting=True)
    return {
        "ref": f"{(row.get('Planning Authority') or '').strip()}:{(row.get('Site Code') or '').strip()}",
        "name": name,
        "kind": kind,
        "evidence": f"Scotland's land survey lists {said}",
        "weight": weight,
        "url": None,
    }


# A building that's falling down, or empty, is worth a look before it goes.
_RUNDOWN = re.compile(r"derelict|dilapidated|disused|vacant|redundant|fire[- ]damaged|abandoned|ruinous|unsafe"
                      r"|dangerous (structure|building)|empty|\bformer\b", re.I)
# ...a garage, a conservatory or the house itself being replaced isn't.
_SMALL_JOB = re.compile(r"demoli\w*\s+(?:of\s+)?(?:the\s+|an?\s+)?(?:existing\s+)?(?:[\w-]+\s+){0,4}?"
                        r"(garages?|conservatory|extensions?|porch|outbuildings?|sheds?|outhouses?|car ?ports?|walls?"
                        r"|chimneys?|lean[- ]to|summer ?house|greenhouse|boundary|fence|stables?|annexe?|canopy"
                        r"|kiosk|porta[ck]abins?|bungalows?|dwelling(?:house)?s?|(?<!public )houses?|garden room"
                        r"|glasshouse)\b", re.I)
# Only "former" or "empty" says less than "derelict" or "fire damaged" does.
_FALLING_DOWN = re.compile(r"derelict|dilapidated|disused|redundant|fire[- ]damaged|abandoned|ruinous|unsafe"
                           r"|dangerous (structure|building)", re.I)
# What it was, when the description says: "demolition of the former Red Lion public house".
_PLANNED_KINDS = [
    (re.compile(r"public house|\bpub\b|\binn\b|\btavern\b", re.I), "pub"),
    (re.compile(r"chapel|church", re.I), "chapel"),
    (re.compile(r"hotel", re.I), "hotel"),
    (re.compile(r"school", re.I), "school"),
    (re.compile(r"cinema|theatre", re.I), "cinema"),
    (re.compile(r"\bclub\b|social club|working men", re.I), "club"),
    (re.compile(r"care home|nursing home", re.I), "care home"),
    (re.compile(r"farm|barns?\b", re.I), "farm buildings"),
    (re.compile(r"warehouse|industrial|commercial|offices?\b", re.I), "industrial building"),
]
_PLANIT_DECIDED = {"Permitted": "approved", "Conditions": "approved", "Rejected": "refused",
                   "Withdrawn": "withdrawn", "Referred": "referred", "Unresolved": "undecided"}
# About an earlier application ("Discharge of condition 8 from 21/01856/O: demolition of disused bus
# depot...", "An application under Section 96A for a non-material amendment..."): that demolition was
# approved, and the work is getting going.
_FOLLOW_UP = re.compile(r"^\W*(?:application for |request for |proposed )?(?:the )?(?:discharge|details reserved"
                        r"|approval of (?:details|reserved matters)|reserved matters|confirmation)"
                        r"|discharge of conditions?|details reserved by condition|non[- ]material amendment"
                        r"|minor material amendment|variation of conditions?|removal of conditions?"
                        r"|\bs(?:ection|\.)? ?96a\b", re.I)
_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}\b", re.I)
# Not a decision on anything: advice before applying, or a certificate that something would be lawful.
_NO_DECISION = re.compile(r"\bpre[- ]?app(?:lication)?\b|certificate of lawful|lawful development"
                          r"|screening opinion|scoping opinion", re.I)


def _planit(row: dict) -> dict | None:
    """A demolition application: a lead when it says the building is derelict, empty or redundant."""
    said = re.sub(r"\s+", " ", row.get("description") or "").strip()
    if not re.search(r"demoli", said, re.I) or not _RUNDOWN.search(said) or _SMALL_JOB.search(said) \
            or _NO_DECISION.search(said):
        return None
    verdict = judge_record(re.sub(r"demoli\w*", "", said, flags=re.I))   # "demolish" would read as gone
    if verdict:
        weight, kind = verdict
    else:
        weight = 20 if _FALLING_DOWN.search(said) else 12
        kind = next((k for rx, k in _PLANNED_KINDS if rx.search(said)), "building")
    outcome = _PLANIT_DECIDED.get(row.get("app_state") or "", "")
    decided = (row.get("decided_date") or "")[:10]
    if _FOLLOW_UP.search(said):
        state = "demolition approved earlier; this follows it up, so the work may be under way"
        weight = 5
    elif outcome == "approved" and decided:
        try:
            gone_soon = date.fromisoformat(decided) < date.today() - timedelta(days=548)
        except ValueError:
            gone_soon = False
        state = f"demolition approved on {decided}" + (", so it may well be gone" if gone_soon else "")
        weight = 5 if gone_soon else weight
    elif outcome:
        state = f"demolition {outcome}" + (f" on {decided}" if decided else "")
    else:
        state = f"applied to demolish it on {(row.get('start_date') or '')[:10]}, no decision yet"
    # An address for a name: without its postcode, and not the whole of a long one with no commas.
    parts = [_POSTCODE.sub("", p).strip(" ,") for p in (row.get("address") or "").split(",")]
    name = ", ".join([p for p in parts if p][:2])
    if len(name) > 60:
        name = name[:60].rsplit(" ", 1)[0]
    return {
        "ref": str(row.get("name") or row.get("uid") or "").strip(),
        "name": name,
        "kind": kind,
        "evidence": f"Planning application ({state}): \"{said[:160]}{'…' if len(said) > 160 else ''}\"",
        "weight": weight,
        "url": row.get("url") or row.get("link"),
    }


def _a(thing: str) -> str:
    return f"{'an' if thing[:1].lower() in 'aeiou' else 'a'} {thing}"


CANMORE_TERMS = ("OBSERVATION POST", "BUNKER", "PILLBOX", "BATTERY", "AIRFIELD", "AERODROME", "COLLIERY",
                 "MINE", "QUARR", "ADIT", "TUNNEL", "VIADUCT", "RAILWAY STATION", "MILL", "FACTORY", "FOUNDRY",
                 "BREWERY", "DISTILLERY", "ENGINE HOUSE", "IRONWORKS", "BRICKWORKS", "GASWORKS", "STEELWORKS",
                 "PUMPING STATION", "ASYLUM", "SANATORIUM", "WORKHOUSE")
CANMORE_WHERES = [f"UPPER(SITETYPE) LIKE '%{term}%'" for term in CANMORE_TERMS]

DATASETS = {
    d.key: d for d in [
        Dataset(
            key="heritage_at_risk",
            label="Heritage at Risk (England)",
            licence="Open Government Licence v3.0",
            attribution="Contains Historic England data © Historic England",
            home="https://opendata-historicengland.hub.arcgis.com/",
            fetch=ArcGIS("https://services-eu1.arcgis.com/ZOdPfBS3aqqDYPUQ/arcgis/rest/services"
                         "/HAR_2025_OTHR_WGS84_Point/FeatureServer/0",
                         where="HeritageCa IN ('Listed Building','Scheduled Monument')",
                         fields="List_Entry,HeritageCa,EntryName,URL,uid"),
            judge=_har,
        ),
        Dataset(
            key="canmore",
            label="Canmore (Scotland)",
            licence="Open Government Licence v3.0",
            attribution="Contains Historic Environment Scotland and Ordnance Survey data "
                        "© Historic Environment Scotland",
            home="https://canmore.org.uk/",
            fetch=ArcGISByIds("https://inspire.hes.scot/arcgis/rest/services/CANMORE/Canmore_Points/MapServer/0",
                              CANMORE_WHERES, fields="CANMOREID,SITENUMBER,NMRSNAME,ALTNAME,SITETYPE,BROADCLASS,URL"),
            judge=_canmore,
        ),
        Dataset(
            key="coflein",
            label="Coflein (Wales)",
            licence="Open Government Licence v2.0",
            attribution="Site data from the National Monuments Record of Wales (RCAHMW)",
            home="https://coflein.gov.uk/",
            fetch=WFS("https://datamap.gov.wales/geoserver/wfs",
                      "geonode:rcahmw_nmrw_terrestrialsites_rcahmw_bng"),
            judge=_coflein,
        ),
        Dataset(
            key="schools",
            label="Closed schools (England)",
            licence="Open Government Licence v3.0",
            attribution="Contains Department for Education data © Crown copyright, from Get Information about Schools",
            home="https://www.get-information-schools.service.gov.uk/",
            fetch=SchoolsRegister(),
            judge=_school,
        ),
        Dataset(
            key="scotland_vdl",
            label="Vacant & derelict land (Scotland)",
            licence="Open Government Licence v3.0",
            attribution="Contains Scottish Vacant and Derelict Land Survey data © Scottish Government",
            home="https://www.gov.scot/publications/the-scottish-vacant-and-derelict-land-survey-site-register/",
            fetch=ScotlandDerelictLand(),
            judge=_vdl,
        ),
        Dataset(
            key="planit",
            label="Demolition applications (UK PlanIt)",
            licence="Planning register data, via UK PlanIt",
            attribution="Planning applications collected by UK PlanIt (planit.org.uk) from council websites",
            home="https://www.planit.org.uk/",
            fetch=PlanIt(),
            judge=_planit,
            incremental=True,
            opt_in=True,
        ),
        Dataset(
            key="brownfield",
            label="Brownfield registers (England)",
            licence="Open Government Licence v3.0",
            attribution="Contains public sector information licensed under the Open Government Licence v3.0",
            home="https://www.planning.data.gov.uk/dataset/brownfield-land",
            fetch=PlanningData("brownfield-land"),
            judge=_brownfield,
        ),
    ]
}


def collect(dataset: Dataset, session: requests.Session, progress: Progress, cancel=None) -> Iterator[dict]:
    """Every record from one register that's worth keeping, ready for the store."""
    for row in dataset.fetch(session, progress, cancel):
        item = dataset.judge(row)
        if not item or not item["ref"]:
            continue
        item["dataset"] = dataset.key
        item["lat"], item["lng"] = row["lat"], row["lng"]
        if not item["name"]:
            item["name"] = f"Unnamed {item['kind']}"
        yield item
