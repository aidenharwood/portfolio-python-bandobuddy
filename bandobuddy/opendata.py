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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urljoin

import requests

from . import committees, registers
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
        survey = re.search(r"(20\d\d)", link.group(1))
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
            yield {**row, "lat": lat, "lng": lng, "survey": survey.group(1) if survey else None}
            done += 1
        progress("reading the register", done, done)


# Words that make a demolition worth knowing about: the building's falling down, or empty.
RUNDOWN_WORDS = ("derelict", "dilapidated", "disused", "vacant", "redundant", "fire damaged", "abandoned", "ruinous",
                 "unsafe", "dangerous structure", "empty", "former")


# ...and words that make any application worth knowing about, demolition or not: a derelict chapel up for
# conversion is still standing, and falling down.
STATE_WORDS = ("derelict", "dilapidated", "ruinous", "fire damaged", "abandoned")
# A house begun and never finished, or finished and never lived in. Golden Hill, near Romsey: a replacement
# mansion approved in 2004, never occupied, and applied for as flats four times since. Few enough (about
# 120 since 2000) to ask for all of them each time.
STALLED_PHRASES = ("unfinished dwelling", "unfinished house", "unfinished building", "partially constructed dwelling",
                   "partially constructed house", "partially constructed building", "partially built dwelling",
                   "partially built house", "part built dwelling", "partly built dwelling", "partially completed dwelling",
                   "incomplete dwelling", "never been occupied", "never occupied",
                   # ...or one that can't be lived in, in so many words: "demolish existing uninhabitable house",
                   # "condemned as unfit for habitation". About 120 more since 2000.
                   "uninhabitable", "unfit for habitation", "unfit for human habitation")
# Living in a caravan on the plot while the house is done up: it couldn't be lived in then. Most are finished in a
# year or two, but not all: 3 Segensworth Road, Titchfield had a caravan "whilst the property is being renovated"
# in 2018, stood empty after, and was approved for demolition in 2024. About 500 since 2000, holiday parks and
# Traveller pitches among them (the judge drops those): two pages.
CARAVAN_SEARCH = " or ".join(f"{home} {work}" for home in ("caravan", '"mobile home"')
                             for work in ("renovated", "renovation", "renovating", "refurbishment", "refurbished"))


def _quoted(words) -> list[str]:
    return [f'"{w}"' if " " in w else w for w in words]


def _planit_search(words=RUNDOWN_WORDS, states=STATE_WORDS) -> str:
    """PlanIt reads "a b or c d" as (a and b) or (c and d): demolition next to one of the words, in
    either form ("demolition" and "demolish" don't share a stem), or one of the state words alone."""
    pairs = [f"{verb} {w}" for w in _quoted(words) for verb in ("demolition", "demolish")]
    return " or ".join(pairs + _quoted(states))


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
    stalled: str = " or ".join(_quoted(STALLED_PHRASES))   # asked for in full, every time: a page or so
    caravans: str = CARAVAN_SEARCH                           # ...and these: two pages
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
        asks = [(self.search, window, {"recent": "new applications", "decided": "decisions"}[window])
                for window in self.windows]
        asks += [(search, None, what) for search, what in ((self.stalled, "unfinished and unlivable buildings"),
                                                           (self.caravans, "houses being done up")) if search]
        for search, window, what in asks:
            page, done = 1, 0
            while True:
                _stop(cancel)
                if asked:
                    self._wait(cancel, self.gap_s)   # a minute between any two requests
                asked = True
                params = {"search": search, "pg_sz": self.batch, "page": page, "select": self.FIELDS,
                          "sort": "-start_date", "compress": "on"}
                if window:
                    params[window] = self.days
                data = self._ask(session, params, cancel)
                records = data.get("records") or []
                total = data.get("total")
                for row in records:
                    if row.get("location_x") is None or row.get("location_y") is None:
                        continue
                    yield {**row, "lat": float(row["location_y"]), "lng": float(row["location_x"])}
                done += len(records)
                progress(f"{what}, a minute between pages", done, total if isinstance(total, int) else None)
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
_ACRONYMS = {"roc", "raf", "mod", "nhs", "rc", "usaf", "ymca", "ywca", "gpo", "lms", "gwr", "lner", "hms", "acf",
             "atc", "hq", "ahq", "sfa", "ltpa", "dmc", "jscs", "jitg", "camhs", "cic"}


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
    remembers: bool = False       # keeps notes in the data folder between runs (which reports it has read)

    def enabled(self) -> bool:
        if not self.opt_in:
            return True
        return os.environ.get(f"BANDOBUDDY_{self.key.upper()}", "").strip().lower() in ("1", "true", "yes", "on")


HAR_YEAR = 2025                   # the register's edition: each year's is a layer of its own


def _dated(value) -> str | None:
    """A register's date as a day ("2024-06-28"): from ISO text, "28/06/2024", or ArcGIS's milliseconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return m.group(0)
    m = re.match(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return None
    m = re.match(r"(\d{4})$", text)
    return m.group(1) if m else None


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
        "reported": str(HAR_YEAR), "reported_as": "on the register in",
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
        "reported": _dated(row.get("entry-date")), "reported_as": "register entry updated",
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
        "reported": _dated(row.get("LASTUPDATE")), "reported_as": "record updated",
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
        "reported": _dated(row.get("lastupdate")), "reported_as": "record updated",
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
        "reported": closed.isoformat() if closed else None, "reported_as": "closed",
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
        "reported": row.get("survey"), "reported_as": "land survey of",
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
    (re.compile(r"\bbank\b", re.I), "bank"),
    (re.compile(r"shipyard|boatyard|dockyard", re.I), "shipyard"),
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


# A building begun and never finished, or finished and never lived in.
_STALLED = re.compile(r"\b(?:unfinished|incomplete|partially (?:constructed|built|completed)|part[- ]built|partly built)"
                      r"\s+(?:dwelling|house|home|building)s?\b|\bnever (?:been )?occupied\b", re.I)
# A house that can't be lived in, said of the house: not of a loft, an annexe or a garage.
_UNLIVABLE = re.compile(r"\b(?:uninhabitable|uninhabited|unfit for (?:human )?habitation)\b", re.I)
_PARTS = (r"(?:loft|roof ?space|attic|annexe?|garages?|outbuildings?|outhouses?|sheds?|space|rooms?|area|basements?|cellars?"
          r"|island|mobile homes?|caravans?)")
_UNLIVABLE_PART = re.compile(r"(?:uninhabitable|uninhabited|unfit for (?:human )?habitation)\s+(?:[\w/-]+\s+){0,2}?"
                             + _PARTS + r"s?\b|\b" + _PARTS + r"\s+(?:[\w-]+\s+){0,3}?(?:uninhabitable|uninhabited)",
                             re.I)
# ...unless it's said of the house too: "demolish existing uninhabitable house and outbuildings".
_UNLIVABLE_HOME = re.compile(r"(?:uninhabitable|uninhabited|unfit for (?:human )?habitation)\s+(?:[\w,-]+\s+){0,3}?"
                             r"(?:house|dwelling|bungalow|cottage|property|building|farmhouse|home|maisonette|flat)s?\b"
                             r"|\b(?:house|dwelling|bungalow|cottage|property|farmhouse)s?\b[^.]{0,40}\b(?:is|was|are|were)"
                             r" (?:\w+ )?(?:uninhabitable|unfit for)", re.I)
# Living on the plot in a caravan while the house is done up...
_DOING_UP = re.compile(r"\b(?:caravan|mobile home|static home)s?\b.{0,160}?\b(?:whilst|while|during|pending|until"
                       r"|to (?:enable|allow|facilitate)|incidental to|for the duration of|for (?:the )?(?:house|home|dwelling))\b.{0,80}?\b(?:renovat|refurbish|restor|repair|rebuil"
                       r"|reconstruct)", re.I)
# ...not a holiday park, a Traveller site, or a house being knocked down and replaced.
_NOT_DOING_UP = re.compile(r"holiday|glamping|touring|\bpitch|traveller|gypsy|caravan (?:park|site|club)|camp ?site"
                           r"|camping|\blodges\b|chalets?|demoli|replacement dwelling|new dwelling", re.I)
# Said of the building itself, with no demolition in sight.
_STATE = re.compile(r"derelict|dilapidated|ruinous|fire[- ]damaged|abandoned|dangerous (?:structure|building)", re.I)
# ...but not of something too small to go and see: the windows, a tree, a shed, a wall.
_SMALL_THING = re.compile(r"(?:derelict|dilapidated|ruinous|fire[- ]damaged|abandoned)\s+(?:[\w-]+\s+){0,3}?"
                          r"(?:windows?|doors?|sash\w*|trees?|hedges?|fences?|walls?|gates?|railings?|sheds?|garages?"
                          r"|greenhouses?|outbuildings?|conservator(?:y|ies)|porch(?:es)?|roofs?|chimneys?|signs?"
                          r"|kiosks?|canop(?:y|ies)|caravans?|vehicles?|boats?|cars?)\b", re.I)
_TREE_WORK = re.compile(r"\btrees?\b|\bT\d+\b|\bTPO\b|\bfell\b|\bpollard|\bcrown (?:reduc|lift|thin)", re.I)


def _excerpt(said: str, focus: re.Match | None, size: int = 160) -> str:
    """The description, or the part of a long one that says why it's here."""
    if len(said) <= size:
        return said
    start = 0 if focus is None or focus.end() <= size - 20 else max(0, focus.start() - 60)
    return ("…" if start else "") + said[start:start + size] + ("…" if start + size < len(said) else "")


def _planit(row: dict) -> dict | None:
    """A planning application worth knowing about: one to demolish something derelict, empty or
    redundant; one that calls the building itself derelict or falling down, or unfit to live in; one about a
    house begun and never finished, or never lived in; or one to live in a caravan on the plot while the house
    is done up."""
    said = re.sub(r"\s+", " ", row.get("description") or "").strip()
    unlivable = _UNLIVABLE.search(said) \
        if (_UNLIVABLE_HOME.search(said) or not _UNLIVABLE_PART.search(said)) and not _TREE_WORK.search(said) else None
    doing_up = _DOING_UP.search(said) if not _NOT_DOING_UP.search(said) else None
    # Advice before applying decides nothing, but an application that calls a house unlivable still says so.
    if _NO_DECISION.search(said) and not (unlivable or doing_up):
        return None
    demolition = bool(re.search(r"demoli", said, re.I))
    weight = kind = None
    stalled = _STALLED.search(said)
    focus = stalled or unlivable or doing_up
    if unlivable and not stalled:
        weight, kind = 18, _kind_of(said) or "building"
    elif doing_up and not stalled:
        weight, kind = 12, _kind_of(said) or "house"
    elif stalled:
        weight = 14
        if "occupied" in stalled.group(0).lower():      # finished, never lived in: a house, or a gym unit
            kind = _kind_of(said) or "building"
        elif re.search(r"dwelling|house|home", stalled.group(0), re.I):
            kind = "unfinished house"
        else:
            kind = "unfinished building"
    elif demolition:
        if not _RUNDOWN.search(said) or _SMALL_JOB.search(said):
            return None
    elif not _STATE.search(said) or _SMALL_THING.search(said) or _TREE_WORK.search(said):
        return None
    if kind is None:
        verdict = judge_record(re.sub(r"demoli\w*", "", said, flags=re.I))   # "demolish" would read as gone
        if verdict:
            weight, kind = verdict
        else:
            weight = 20 if _FALLING_DOWN.search(said) else 12
            kind = next((k for rx, k in _PLANNED_KINDS if rx.search(said)), "building")
            if not demolition and kind == "farm buildings":
                weight = 8          # a derelict barn up for conversion: there are a great many of those
    outcome = _PLANIT_DECIDED.get(row.get("app_state") or "", "")
    decided = (row.get("decided_date") or "")[:10]
    if _FOLLOW_UP.search(said):
        state = (f"{'demolition' if demolition else 'the work'} approved earlier; this follows it up, "
                 "so the work may be under way")
        weight = 5
    elif outcome == "approved" and decided:
        try:
            long_ago = date.fromisoformat(decided) < date.today() - timedelta(days=548)
        except ValueError:
            long_ago = False
        if demolition:
            state = f"demolition approved on {decided}" + (", so it may well be gone" if long_ago else "")
        else:
            state = f"approved on {decided}" + (", so the work may well be done" if long_ago else "")
        weight = 5 if long_ago else weight
    elif outcome:
        state = (f"demolition {outcome}" if demolition else outcome) + (f" on {decided}" if decided else "")
    else:
        started = (row.get("start_date") or "")[:10]
        state = (f"applied to demolish it on {started}" if demolition else f"applied for on {started}") \
            + ", no decision yet"
    # An address for a name: without its postcode, and not the whole of a long one with no commas.
    parts = [_POSTCODE.sub("", p).strip(" ,") for p in (row.get("address") or "").split(",")]
    name = ", ".join([p for p in parts if p][:2])
    if len(name) > 60:
        name = name[:60].rsplit(" ", 1)[0]
    return {
        "ref": str(row.get("name") or row.get("uid") or "").strip(),
        "name": name,
        "kind": kind,
        "reported": decided or (row.get("start_date") or "")[:10] or None,
        "reported_as": "decided" if decided else "applied for",
        "evidence": f"Planning application ({state}): \"{_excerpt(said, focus or _STATE.search(said))}\""
                    + ("; someone was to live in a caravan on the plot meanwhile, so it couldn't be lived in then"
                       if doing_up and not (stalled or unlivable) else ""),
        "weight": weight,
        "url": row.get("url") or row.get("link"),
    }


@dataclass
class CommitteeReports:
    """Planning committee reports on the councils' ModernGov sites (committees.py): every report since 2016
    the first time, then the last few weeks' each week, a request a second to each council."""

    sites: tuple = committees.MODERNGOV_SITES
    gap_s: float = 1.0

    def __call__(self, session: requests.Session, progress: Progress, cancel=None,
                 data_dir: Path | None = None) -> Records:
        memory = committees.Memory(Path(data_dir) / "committee_reports.json" if data_dir else None)
        yield from committees.crawl(session, self.sites, progress, cancel, memory, self.gap_s)


# A shop or office unit standing empty is ordinary; a house, a chapel or a mill isn't.
_UNIT = re.compile(r"\b(?:shop|retail|unit|office|premises|commercial)\b", re.I)
_HOME = re.compile(r"dwelling|house|residence|bungalow|cottage|mansion|villa|\bhome\b", re.I)
_LAND = re.compile(r"\b(?:site|land|plot)\b", re.I)
_PROPOSED = re.compile(r"\b(?:erection|construction|into|to form|to create|to provide|replacement|new build)\b", re.I)
_STANDING = re.compile(r"building|house|dwelling|residence|chapel|church|mill|hall|barn|\bpub\b|hotel|school|premises"
                       r"|\bunit\b|shop|office|factory|warehouse|property|structure|bank|\binn\b|cinema", re.I)


def _kind_of(text: str) -> str | None:
    return next((k for rx, k in _PLANNED_KINDS if rx.search(text)), None) or ("house" if _HOME.search(text) else None)


def _committee(row: dict) -> dict | None:
    """A council planning officer saying, in a committee report, that the site stands empty, unfinished or
    derelict: about as reliable as it gets, as the officer will have been to look."""
    said = row["sentence"]
    # What the officer says it is, or what the proposal says is there now: "demolition of existing B2 use
    # shipyard buildings" and not "...and the erection of 3no. replacement C3 dwellings".
    existing = _PROPOSED.split(row.get("proposal") or "", 1)[0]
    kind = _kind_of(said) or _kind_of(existing) or "building"
    if re.search(r"partially|unfinished", row["phrase"]):
        weight, kind = 16, "unfinished house" if kind == "house" else kind
    elif _UNIT.search(said) and kind in ("building", "industrial building"):
        weight = 8
    elif _LAND.search(said) and not _STANDING.search(said):    # "a vacant and derelict site": nothing standing
        weight, kind = 8, "vacant land"
    else:
        weight = 22
    try:
        years = (date.today() - date.fromisoformat(row["meeting"])).days / 365.25
    except (KeyError, ValueError):
        years = 0
    if years > 10:          # long enough ago that it may well have been done up or knocked down since
        weight = min(weight, 6)
    elif years > 5:
        weight -= 8
    parts = committees.tidy_address(row["site"]).split(", ")
    name = ", ".join(parts[:2])
    if len(name) > 60:
        name = name[:60].rsplit(" ", 1)[0]
    when = f", {row['committee']}, {row['meeting']}" if row.get("meeting") else ""
    return {
        "ref": f"{row['council']}:{row['ref']}",
        "name": name,
        "kind": kind,
        "evidence": f"{row['council']}'s planning report ({row['ref']}{when}): \"{said[:280]}\"",
        "weight": weight,
        "url": row["url"],
        "reported": row.get("meeting") or None, "reported_as": "committee meeting",
    }


def _a(thing: str) -> str:
    return f"{'an' if thing[:1].lower() in 'aeiou' else 'a'} {thing}"


# -- closed care homes and hospitals, empty NHS sites, closed railways, MOD disposals --

def _title(text: str) -> str:
    """A name a register gives in capitals, as it'd be written: "ST GEORGE'S BARRACKS" -> "St George's Barracks"."""
    if not text.isupper():
        return text
    return re.sub(r"[^\W\d_]+(?:['’][^\W\d_]+)?", lambda m: _cased(m.group(0), m.start() == 0), text.strip())


def _years_since(day: str) -> float | None:
    try:
        return (date.today() - date.fromisoformat(day[:10])).days / 365.25
    except (TypeError, ValueError):
        return None


def _cqc(row: dict) -> dict | None:
    """A care home or hospital the Care Quality Commission no longer regulates, with nothing registered there
    since: the building's empty, or turned into something else (the more years, the likelier)."""
    said = f"{row['name']} {row['category']}"
    years = _years_since(row["ended"])
    if row.get("centre"):        # a day-service building: councils take years to sell them (Fiveways, Yeovil)
        weight = 22 if years is not None and years <= 5 else 20
        if years is not None and years > 14:
            weight = 8
        elif years is not None and years > 10:
            weight = 12
        return {"ref": row["ref"], "name": row["centre"], "kind": "day centre", "weight": weight,
                "evidence": f"The Care Quality Commission records it as closed in {row['ended'][:4]}: the last care "
                            f"service at {row['centre']} ended then, and none is registered there now",
                "url": f"https://www.cqc.org.uk/location/{row['ref']}", "reported": row["ended"], "reported_as": "closed"}
    if row["care_home"]:
        kind = "nursing home" if re.search(r"nursing", said, re.I) else "care home"
        weight, what = (20 if row["beds"] >= 40 else 16), f"{_a(kind)} with {row['beds']} beds"
    else:
        kind = "hospice" if re.search(r"hospice", said, re.I) else "hospital"
        weight, what = 20, _a(kind)
    if years is not None and years <= 5:
        weight += 4
    elif years is not None and years > 12:
        weight = min(weight, 8)
    elif years is not None and years > 8:
        weight -= 6
    return {"ref": row["ref"], "name": row["name"], "kind": kind, "weight": weight,
            "evidence": f"The Care Quality Commission records it as closed in {row['ended'][:4]} ({what}); "
                        "no care service is registered there now",
            "url": f"https://www.cqc.org.uk/location/{row['ref']}", "reported": row["ended"], "reported_as": "closed"}


def _nhs_estate(row: dict) -> dict | None:
    """An NHS site its trust reports as wholly unoccupied, or mostly standing empty."""
    name = _title(row["name"])
    if re.match(r"other reportable sites?$", name, re.I):     # unnamed in the return: say whose it is
        name = f"Empty site of {_title(row['trust'])}"[:80]
    kind = "hospital" if re.search(r"hospital|infirmary|asylum|sanatori", name, re.I) else "NHS building"
    if row["whole"]:
        area = row["unoccupied_m2"] or row["floor_m2"]
        said = f"NHS estates return ({row['year']}): the whole site{f' ({int(area):,} m²)' if area else ''} is unoccupied"
        weight = 22 + (4 if row.get("pre_1948", 0) >= 50 else 0)
    else:
        said = (f"NHS estates return ({row['year']}): {int(row['empty_m2']):,} of its {int(row['floor_m2']):,} m² "
                "stand empty")
        weight = 18
    year = re.match(r"(\d{4})/(\d{2})", row.get("year") or "")
    return {"ref": row["ref"], "name": name, "kind": kind, "weight": weight, "evidence": said, "url": None,
            "reported": f"{year.group(1)[:2]}{year.group(2)}-03-31" if year else None,
            "reported_as": "estates return for the year to"}


def _railway_estate(row: dict) -> dict | None:
    """A tunnel or viaduct on a closed railway, looked after by National Highways."""
    tunnel = row["kind"] == "tunnel"
    kind = "railway tunnel" if tunnel else "railway viaduct"
    line = f" ({row['line']})" if row.get("line") else ""
    name = row["name"] or f"{kind.capitalize()}{line}"
    reused = row.get("path") in ("Railway Path", "Sustrans")
    said = f"National Highways' Historical Railways Estate: {_a(row['kind'])} on a closed railway{line}"
    if reused:
        said += "; the line is now a path, so it has a new use"
    weight = 10 if reused else 26 if tunnel else 14
    return {"ref": row["ref"], "name": name, "kind": kind, "weight": weight, "evidence": said, "url": None}


_MOD_KINDS = [(re.compile(p, re.I), k) for p, k in (
    (r"\bmess(?:es)?\b", "officers' mess"), (r"\bacf\b|\batc\b|cadet", "cadet hut"), (r"barracks", "barracks"),
    (r"\braf\b|airfield|air station|aerodrome", "airfield"), (r"\branges?\b", "firing range"),
    (r"camp\b", "military camp"), (r"depot|distribution|stores?\b", "military depot"),
    (r"school|college|academy", "military school"), (r"married quarters|\bhousing\b|\bsfa\b", "military housing"),
    (r"hospital", "military hospital"), (r"\bfort\b|citadel", "fort"))]


def _mod(row: dict) -> dict | None:
    """A site the Ministry of Defence is disposing of: given up already, or due to be."""
    establishment, parcel = _title(row["establishment"]), _title(row["parcel"])
    name = establishment if not parcel or parcel.lower() in (establishment.lower(), "various parcels") \
        else parcel if establishment.lower() in parcel.lower() else f"{parcel}, {establishment}"
    kind = next((k for text in (parcel, establishment) for rx, k in _MOD_KINDS if rx.search(text)), "military site")
    year = int(row["year"]) if (row.get("year") or "").isdigit() else None
    if year and year <= date.today().year:
        said, weight = f"The Ministry of Defence lists it as surplus, to be sold from {year}", 18
    elif year:
        said, weight = f"The Ministry of Defence is due to close it and sell it from {year}", 8
    else:
        said, weight = "The Ministry of Defence lists it as surplus, to be sold", 8
    if kind == "cadet hut":         # a hut in a town: not much to see
        weight = min(weight, 8)
    status = (row.get("status") or "").strip().lower()
    return {"ref": row["ref"], "name": name[:90], "kind": kind, "weight": weight,
            "evidence": said + (f" (stage: {status})" if status else ""),
            "url": "https://www.gov.uk/government/publications/disposal-database-house-of-commons-report",
            "reported": row.get("reported"), "reported_as": "reported to Parliament"}


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
                         f"/HAR_{HAR_YEAR}_OTHR_WGS84_Point/FeatureServer/0",
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
                              CANMORE_WHERES, fields="CANMOREID,SITENUMBER,NMRSNAME,ALTNAME,SITETYPE,BROADCLASS,URL,LASTUPDATE"),
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
            label="Planning applications (UK PlanIt)",
            licence="Planning register data, via UK PlanIt",
            attribution="Planning applications collected by UK PlanIt (planit.org.uk) from council websites",
            home="https://www.planit.org.uk/",
            fetch=PlanIt(),
            judge=_planit,
            incremental=True,
            opt_in=True,
        ),
        Dataset(
            key="committees",
            label="Planning committee reports",
            licence="Quoted from councils' committee papers, most published under the Open Government Licence",
            attribution="A sentence from each council's own planning report, found through its ModernGov site",
            home="https://github.com/aidenharwood/portfolio-python-bandobuddy#planning-committee-reports",
            fetch=CommitteeReports(),
            judge=_committee,
            incremental=True,
            opt_in=True,
            remembers=True,
        ),
        Dataset(
            key="care_closures",
            label="Closed care homes & hospitals (England)",
            licence="Open Government Licence v3.0",
            attribution="Contains Care Quality Commission data © CQC",
            home="https://www.cqc.org.uk/about-us/transparency/using-cqc-data",
            fetch=registers.CqcClosures(),
            judge=_cqc,
            remembers=True,
        ),
        Dataset(
            key="nhs_estates",
            label="Empty NHS sites (England)",
            licence="Open Government Licence v3.0",
            attribution="Contains NHS England data (Estates Returns Information Collection)",
            home="https://digital.nhs.uk/data-and-information/publications/statistical/"
                 "estates-returns-information-collection",
            fetch=registers.NhsEstates(),
            judge=_nhs_estate,
            remembers=True,
            opt_in=True,       # its file host's robots.txt turns away every robot: the owner's call to make
        ),
        Dataset(
            key="railway_estate",
            label="Closed railway tunnels & viaducts",
            licence="Published by National Highways (Historical Railways Estate)",
            attribution="Contains National Highways information (Historical Railways Estate structures)",
            home="https://nationalhighways.co.uk/our-work/historical-railways-estate/about-the-hre/",
            fetch=registers.RailwayEstate(),
            judge=_railway_estate,
            remembers=True,
        ),
        Dataset(
            key="mod_disposals",
            label="MOD sites being disposed of",
            licence="Open Government Licence v3.0",
            attribution="Contains Ministry of Defence data (Disposal Database)",
            home="https://www.gov.uk/government/publications/disposal-database-house-of-commons-report",
            fetch=registers.MoDisposals(),
            judge=_mod,
            remembers=True,
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


def collect(dataset: Dataset, session: requests.Session, progress: Progress, cancel=None,
            data_dir: Path | None = None) -> Iterator[dict]:
    """Every record from one register that's worth keeping, ready for the store."""
    rows = (dataset.fetch(session, progress, cancel, data_dir=data_dir) if dataset.remembers
            else dataset.fetch(session, progress, cancel))
    for row in rows:
        item = dataset.judge(row)
        if not item or not item["ref"]:
            continue
        item["dataset"] = dataset.key
        item["lat"], item["lng"] = row["lat"], row["lng"]
        if not item["name"]:
            item["name"] = f"Unnamed {item['kind']}"
        yield item
