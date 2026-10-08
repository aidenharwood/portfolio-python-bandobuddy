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
import json
import os
import re
import threading
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urljoin

import requests

from . import committees, registers
from .config import USER_AGENT
from .geo import bng_to_wgs84, haversine_m
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
    at_once: int = 4

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
        batches = [ids[start:start + self.batch] for start in range(0, len(ids), self.batch)]
        fetch = lambda batch: _features(session, self.url, {"objectIds": ",".join(str(i) for i in batch),  # noqa: E731
                                                            "outFields": self.fields})
        with ThreadPoolExecutor(max_workers=self.at_once) as pool:      # in order, a few in flight at a time
            for features in pool.map(fetch, batches):
                _stop(cancel)
                for row in _points(features):
                    yield row
                    done += 1
                progress("downloading", done, len(ids))


@dataclass
class WFS:
    """A GeoServer WFS layer (DataMap Wales). Only the columns read, in big pages: the whole record with its
    geometry is several times the size, for 113,000 sites."""

    url: str
    layer: str
    batch: int = 5000
    fields: str = ""
    sort_by: str = "nprn"          # pages need a steady order
    where: str = ""                # a CQL filter, to ask for only what's wanted
    lat_field: str = "lat"
    lng_field: str = "long"

    def __call__(self, session: requests.Session, progress: Progress, cancel=None) -> Records:
        params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeName": self.layer,
                  "outputFormat": "application/json", "count": self.batch, "sortBy": self.sort_by}
        if self.fields:
            params["propertyName"] = self.fields
        if self.where:
            params["CQL_FILTER"] = self.where
        total, done, start = None, 0, 0
        while True:
            _stop(cancel)
            page = _get(session, self.url, {**params, "startIndex": start})
            total = total or page.get("totalFeatures") or page.get("numberMatched")
            features = page.get("features") or []
            for feature in features:
                props = feature.get("properties") or {}
                try:  # a few rows carry spreadsheet leftovers ("#VALUE!") instead of a position
                    lat, lng = float(props.get(self.lat_field)), float(props.get(self.lng_field))
                except (TypeError, ValueError):
                    continue
                yield {**props, "lat": lat, "lng": lng}
                done += 1
            progress("downloading", done, total if isinstance(total, int) else None)
            # On to the total it says it has: a server may give fewer to a page than asked for.
            done_all = start + len(features) >= total if isinstance(total, int) else len(features) < self.batch
            if not features or done_all:
                return
            start += len(features)


@dataclass
class PlanningData:
    """planning.data.gov.uk, which collects the registers English councils publish: the whole dataset as one
    file (20 MB for brownfield land), read as it arrives, rather than page after page of its API. Entries a
    council has taken off its register (an end date) are left out."""

    dataset: str
    url: str = "https://files.planning.data.gov.uk/dataset/{dataset}.csv"

    def __call__(self, session: requests.Session, progress: Progress, cancel=None) -> Records:
        progress("downloading", 0, None)
        resp = session.get(self.url.format(dataset=self.dataset), headers={"User-Agent": USER_AGENT},
                           timeout=TIMEOUT, stream=True)
        resp.raise_for_status()
        done = 0
        for i, row in enumerate(csv.DictReader(_lines(resp, "utf-8-sig"))):
            if i % 2000 == 0:
                _stop(cancel)
                progress("downloading", done, None)
            if row.get("end-date"):
                continue
            point = _point(row.get("point"))
            if point:
                yield {**row, "lat": point[0], "lng": point[1]}
                done += 1
        progress("downloading", done, done)


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
    days, mostly council sites being re-read: hours of pages a week, and past PlanIt's 5,000 limit.)

    A house being done up, or one that couldn't be lived in or was never finished, should have been sorted out a
    year and a half on, and most have. So each of those is looked up again, by where it is, for anything applied
    for at the same house since: knocking it down, replacing it, or doing it up all over again says the work never
    got done. (3 Segensworth Road, Titchfield: a caravan on the plot while it was renovated in 2018, then an
    application to demolish the house in 2023.) A few hundred of them, a minute apart, a share each run; then each
    again only every six months. What's found is kept in the data folder."""

    url: str = "https://www.planit.org.uk/api/applics/json"
    search: str = _planit_search()
    days: int = 14                 # a fortnight: a weekly run with a week to spare
    windows: tuple = ("recent", "decided")   # made lately, and decided lately
    stalled: str = " or ".join(_quoted(STALLED_PHRASES))   # asked for in full, every time: a page or so
    caravans: str = CARAVAN_SEARCH                           # ...and these: two pages
    batch: int = 300
    gap_s: float = 61
    follow_ups: int = 90           # houses looked up again each run, at most: an hour and a half
    recheck_days: int = 182
    krad: float = 0.1              # how far around a house to look, in km: councils place one house differently

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

    def __call__(self, session: requests.Session, progress: Progress, cancel=None,
                 data_dir: Path | None = None) -> Records:
        path = Path(data_dir) / "planit_later.json" if data_dir else None
        memory = _load(path, {"checked": {}, "later": {}})
        today = date.today()
        due: dict[str, tuple[str, dict]] = {}        # houses to look up again, and when they last were
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
                    row = {**row, "lat": float(row["location_y"]), "lng": float(row["location_x"])}
                    key = str(row.get("name") or "")
                    if memory["later"].get(key):
                        row["later"] = memory["later"][key]
                    if key and _worth_a_second_look(row, today):
                        checked = memory["checked"].get(key) or ""
                        if checked < (today - timedelta(days=self.recheck_days)).isoformat():
                            due[key] = (checked, row)
                    yield row
                done += len(records)
                progress(f"{what}, a minute between pages", done, total if isinstance(total, int) else None)
                if len(records) < self.batch or (isinstance(total, int) and done >= total):
                    break
                page += 1
        # Then the houses that should have been sorted out by now: those never looked up first, then the longest
        # since, and the newest applications before the oldest.
        queue = sorted(due.items(), key=lambda kv: kv[1][1].get("start_date") or "", reverse=True)
        queue = sorted(queue, key=lambda kv: kv[1][0])[:self.follow_ups]
        for n, (key, (_, row)) in enumerate(queue, 1):
            _stop(cancel)
            if asked:
                self._wait(cancel, self.gap_s)
            asked = True
            later = self._since(session, row, cancel)
            memory["checked"][key] = today.isoformat()
            if later:
                memory["later"][key] = later
            else:
                memory["later"].pop(key, None)
            _save(path, memory)
            progress("houses looked up again for anything since, a minute apart", n, len(queue))
            if later != row.get("later"):
                yield {**row, "later": later}

    def _since(self, session: requests.Session, row: dict, cancel) -> list[dict]:
        """What's been applied for at the same house since that says the work never got done."""
        data = self._ask(session, {"lat": row["lat"], "lng": row["lng"], "krad": self.krad,
                                   "start_date": (row.get("start_date") or "")[:10], "pg_sz": self.batch,
                                   "select": self.FIELDS, "sort": "-start_date", "compress": "on"}, cancel)
        return [{k: r.get(k) for k in ("name", "description", "app_state", "start_date", "decided_date", "url")}
                for r in data.get("records") or []
                if r.get("name") != row.get("name") and (r.get("start_date") or "") > (row.get("start_date") or "")
                and _same_house(row.get("address"), r.get("address")) and _gave_up_on(r, row.get("start_date"))]


def _load(path: Path | None, empty: dict) -> dict:
    if path and path.exists():
        try:
            return {**empty, **json.loads(path.read_text(encoding="utf-8"))}
        except (OSError, ValueError):
            pass
    return empty


def _save(path: Path | None, data: dict) -> None:
    if path:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)


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
    every_days: int | None = None  # how often it's worth asking, if not the usual: a record that never changes
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


def latest_date(dates) -> tuple[str | None, str | None]:
    """The most recent of a record's dates that isn't still to come, as (what, when). A bare year counts as its end
    once it's begun ("on the register in 2025" is later than "closed 2025-03-31")."""
    today = date.today().isoformat()
    past = [(what, when) for what, when in dates or () if when and (when if len(when) > 4 else f"{when}-01-01") <= today]
    if not past:
        return None, None
    return max(past, key=lambda d: d[1] + ("-12-31" if len(d[1]) == 4 else ""))


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
        "dates": [("on the register in", str(HAR_YEAR))],
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
        "dates": [("first on the register", _dated(row.get("start-date"))),
                  ("register entry updated", _dated(row.get("entry-date")))],
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
        "dates": [("recorded", _dated(row.get("ENTRYDATE"))), ("record updated", _dated(row.get("LASTUPDATE")))],
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
        "dates": [("record updated", _dated(row.get("lastupdate")))],
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
        "dates": [("closed", closed.isoformat() if closed else None)],
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
        "dates": [("land survey of", row.get("survey"))],
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
                        r"|discharge of conditions?|details reserved by condition|details pursuant to"
                        r"|non[- ]material amendment"
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
# The house itself knocked down or replaced: not a garage, an extension or a shed.
_REPLACED = re.compile(r"demoli\w*\s+(?:of\s+)?(?:the\s+)?(?:existing\s+)?(?:[\w-]+\s+){0,2}?"
                       r"(?:house|dwelling(?:house)?|bungalow|cottage|farmhouse|property|home)s?\b"
                       r"|replacement (?:dwelling|house|bungalow|home)", re.I)
# How an address can start without saying which house: "Land to the rear of 5 Mill Lane".
_NEAR = re.compile(r"^(?:land|site|plot|garden|part)s?\b(?:\s+[\w/]+){0,4}?\s+(?:at|of|adj\w*|adjoining|behind"
                   r"|opposite|next to)\s+", re.I)
SORTED_OUT_DAYS = 548       # a year and a half: what an approval's given before the work's taken to be done


def _said(row: dict) -> str:
    return re.sub(r"\s+", " ", row.get("description") or "").strip()


def _house_state(said: str) -> tuple:
    """What an application says of the house: (begun and never finished, can't be lived in, being done up), as
    matches or None."""
    unlivable = _UNLIVABLE.search(said) \
        if (_UNLIVABLE_HOME.search(said) or not _UNLIVABLE_PART.search(said)) and not _TREE_WORK.search(said) else None
    doing_up = _DOING_UP.search(said) if not _NOT_DOING_UP.search(said) else None
    return _STALLED.search(said), unlivable, doing_up


def _worth_a_second_look(row: dict, today: date) -> bool:
    """A house being done up, or one that couldn't be lived in or was never finished, applied for long enough ago
    that it should have been sorted out. Not one that was to be knocked down anyway: applying again to knock it
    down says nothing new."""
    said = _said(row)
    if _FOLLOW_UP.search(said) or re.search(r"demoli", said, re.I) or not any(_house_state(said)):
        return False
    try:
        return date.fromisoformat((row.get("start_date") or "")[:10]) < today - timedelta(days=SORTED_OUT_DAYS)
    except ValueError:
        return False


def _gave_up_on(row: dict, since: str | None) -> bool:
    """A later application that says the work never got done: knock the house down or replace it, or do it up all
    over again a year and a half on (not the same plan sent in again a few months later)."""
    said = _said(row)
    if _FOLLOW_UP.search(said):
        return False
    if _REPLACED.search(said):
        return True
    try:
        again = date.fromisoformat((row.get("start_date") or "")[:10]) \
            >= date.fromisoformat((since or "")[:10]) + timedelta(days=SORTED_OUT_DAYS)
    except ValueError:
        return False
    return again and any(_house_state(said))


def _same_house(address: str | None, other: str | None) -> bool:
    """"3 Segensworth Road Titchfield Fareham PO15 5DY" and "Land at 3 Segensworth Road, Titchfield": one house.
    13 Segensworth Road, or Oak Cottage next door, isn't."""
    def words(a):
        return re.findall(r"[a-z0-9]+", _NEAR.sub("", _POSTCODE.sub(" ", a or "").strip()).lower())
    mine, theirs = words(address), words(other)
    if len(mine) < 2 or len(theirs) < 2:
        return False
    return f" {' '.join(mine[:3])} " in f" {' '.join(theirs)} " or f" {' '.join(theirs[:3])} " in f" {' '.join(mine)} "


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
    said = _said(row)
    stalled, unlivable, doing_up = _house_state(said)
    # Advice before applying decides nothing, but an application that calls a house unlivable still says so.
    if _NO_DECISION.search(said) and not (unlivable or doing_up):
        return None
    demolition = bool(re.search(r"demoli", said, re.I))
    weight = kind = None
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
            long_ago = date.fromisoformat(decided) < date.today() - timedelta(days=SORTED_OUT_DAYS)
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
    # Since then, the same house in again to be knocked down, replaced or done up: the work never got done.
    since, dates = "", [("applied for", (row.get("start_date") or "")[:10] or None), ("decided", decided or None)]
    later = [r for r in row.get("later") or () if _gave_up_on(r, row.get("start_date"))] \
        if focus and not demolition and not _FOLLOW_UP.search(said) else None
    if later:
        last = max(later, key=lambda r: r.get("start_date") or "")
        then = _PLANIT_DECIDED.get(last.get("app_state") or "", "") or "undecided"
        then_on = (last.get("decided_date") or "")[:10]
        applied = (last.get("start_date") or "")[:10]
        ref = str(last.get("name") or "").split("/", 1)[-1]
        state = state.replace(", so the work may well be done", "")
        weight, gone = 20, ""
        try:      # knocking it down approved long enough ago to have lapsed, if it wasn't done: it may be gone
            if then == "approved" and _REPLACED.search(_said(last)) \
                    and date.fromisoformat(then_on) < date.today() - timedelta(days=3 * 365):
                weight, gone = 12, ", and it may since have gone"
        except ValueError:
            pass
        since = (f"; then, in {applied[:4]}, another application here ({ref}, "
                 f"{then}{f' on {then_on}' if then_on and then != 'undecided' else ''}): "
                 f"\"{_excerpt(_said(last), None, 120)}\", so it seems the work was never finished{gone}")
        dates += [("applied again", applied or None), ("decided again", then_on or None)]
    # An address for a name: without its postcode, and not the whole of a long one with no commas.
    parts = [_POSTCODE.sub("", p).strip(" ,") for p in (row.get("address") or "").split(",")]
    name = ", ".join([p for p in parts if p][:2])
    if len(name) > 60:
        name = name[:60].rsplit(" ", 1)[0]
    return {
        "ref": str(row.get("name") or row.get("uid") or "").strip(),
        "name": name,
        "kind": kind,
        "dates": dates,
        "evidence": f"Planning application ({state}): \"{_excerpt(said, focus or _STATE.search(said))}\""
                    + ("; someone was to live in a caravan on the plot meanwhile, so it couldn't be lived in then"
                       if doing_up and not (stalled or unlivable) else "") + since,
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
        "dates": [("committee meeting", row.get("meeting") or None)],
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
                "url": f"https://www.cqc.org.uk/location/{row['ref']}", "dates": [("closed", row["ended"])]}
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
            "url": f"https://www.cqc.org.uk/location/{row['ref']}", "dates": [("closed", row["ended"])]}


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
            "dates": [("estates return for the year to", f"{year.group(1)[:2]}{year.group(2)}-03-31" if year else None)]}


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
    return {"ref": row["ref"], "name": name, "kind": kind, "weight": weight, "evidence": said, "url": None,
            "dates": [("list updated", row.get("updated"))]}


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
            "dates": [("reported to Parliament", row.get("reported"))]}


# -- the old Ordnance Survey six-inch maps (GB1900) ----------------------------------------------------------

# GB1900: every word on the second edition of the OS six-inch maps of Great Britain (surveyed 1888-1913), typed in by
# volunteers. Asked for are labels that might be a building or works still standing, or its ruins: anything the map
# already called old, disused or ruined, and the kind of works that leaves lasting remains (lime kilns, engine
# houses, chimneys). Butser Hill Lime Works, above Petersfield, is on the map as "Butserhill Lime Works" and in no
# other open source.
OLD_MAP_YEARS = ("1888", "1913")
_OLD_MAP_WORDS = ("mill", "kiln", "works", "chapel", "church", "school", "smithy", "engine", "colliery", "pumping",
                  "furnace", "foundry", "brewery", "warehouse", "station", "inn", "barracks", "fort", "battery",
                  "lighthouse")


# A quarry the map shows air shafts beside was worked underground: Bethel Quarry, under Bradford-on-Avon, is a
# "Quarry" by Frome Road with "Air Shaft"s across the hill, and in no other open source. (Railway tunnels have air
# shafts too, so a quarry by one is a false lead now and then. So do mines: not one with a mine's old shafts
# or levels near.)
_QUARRY_LABELS = ("quarry", "quarries", "old quarry", "old quarries", "quarry (disused)", "stone quarry")
_SHAFT = re.compile(r"\b(?:air|slope|ventilating|ventilation) shafts?\b", re.I)
# ...unless the shafts are a mine's: a quarry in a lead or coal field, with old shafts and levels all round.
_MINE = re.compile(r"\bold shafts?\b|\bshafts? \(disused\)|\b(?:old )?levels?\b(?! crossing)|\blevel \(disused\)"
                   r"|\bmines?\b|\badits?\b|engine house|\bwhim\b|\bcoal pits?\b", re.I)
UNDERGROUND_WITHIN_M = 500


@dataclass
class OldMaps:
    """GB1900's labels, from the National Library of Scotland's map server: those _old_map judges, then every
    quarry with an air shaft within UNDERGROUND_WITHIN_M, marked as worked underground."""

    url: str = "https://geoserver.nls.uk/geoserver/wfs"
    layer: str = "nls:gb1900_21_December"

    def _ask(self, where: str) -> WFS:
        return WFS(self.url, self.layer, fields="pin_id,final_text,latitude,longitude,parish", sort_by="pin_id",
                   where=where, lat_field="latitude", lng_field="longitude")

    def __call__(self, session: requests.Session, progress: Progress, cancel=None) -> Records:
        yield from self._ask(_old_map_where())(session, progress, cancel)
        quarries = "final_text_lower IN ({})".format(", ".join(f"'{q}'" for q in _QUARRY_LABELS))
        others = " OR ".join(f"final_text_lower LIKE '%{w}%'" for w in ("shaft", "level", "mine", "adit", "engine house",
                                                                       "whim", "coal pit"))
        found, shafts, mines = [], {}, {}
        for row in self._ask(f"{quarries} OR {others}")(session, progress, cancel):
            text = (row.get("final_text") or "").strip()
            cell = (round(row["lat"], 2), round(row["lng"], 2))
            if _SHAFT.search(text):
                shafts.setdefault(cell, []).append(row)
            elif _MINE.search(text):
                mines.setdefault(cell, []).append(row)
            elif text.lower() in _QUARRY_LABELS:
                found.append(row)
        for quarry in found:
            near = _near(quarry, shafts)
            if near and not _near(quarry, mines):
                yield {**quarry, "air_shafts": len(near)}


def _near(row: dict, cells: dict) -> list[dict]:
    """What's in `cells` (rows by position to 0.01 degrees) within UNDERGROUND_WITHIN_M of the row."""
    la, ln = round(row["lat"], 2), round(row["lng"], 2)
    return [s for dla in (-0.01, 0, 0.01) for dln in (-0.01, 0, 0.01)
            for s in cells.get((round(la + dla, 2), round(ln + dln, 2)), ())
            if haversine_m(row["lat"], row["lng"], s["lat"], s["lng"]) <= UNDERGROUND_WITHIN_M]


def _old_map_where() -> str:
    like = "final_text_lower LIKE '{}'".format
    old = " OR ".join(like(f"%{w}%") for w in _OLD_MAP_WORDS)
    return " OR ".join([like(t) for t in ("%works%", "%kiln%", "%engine house%", "%disused%", "%ruin%", "%chimney%")]
                       + [f"({like('old %')} AND ({old}))"])


# What a label is, read from its end: "Butserhill Lime Works", "Corn Mill (Disused)". (pattern, kind, lasting): a
# lasting kind is worth a look even if it was working then; the rest only if the map already called it old,
# disused or ruined. Anything else ending the label ("Kiln Lane", "Limekiln Wood", "Old Mill Pond") is a place
# named after one, not the thing.
_OLD_MAP_KINDS = [(re.compile(rf"(?:^|\b){p}$", re.I), kind, lasting) for p, kind, lasting in (
    (r"lime ?works", "lime works", True),
    (r"(?:lime ?|brick ?)?kilns?", "lime kiln", True),
    (r"cement works", "cement works", True),
    (r"(?:pumping |fire )?engine ?houses?", "engine house", True),
    (r"chimneys?", "chimney", True),
    (r"(?:blast )?furnaces?", "furnace", True),
    (r"(?:brick|tile|pipe)(?: (?:&|and) (?:tile|pipe))? ?works", "brick works", False),
    (r"(?:[\w&']+ )*works", "works", False),
    (r"windmill", "windmill", False),
    (r"(?:[\w']+ )?mill", "mill", False),
    (r"colliery|(?:coal|lead|copper|tin|iron|ironstone|silver|zinc|barytes|manganese) mines?", "mine", False),
    (r"level", "adit", False),
    (r"(?:[\w.']+ )*(?:chapel|church|kirk|meeting house)", "chapel", False),
    (r"school", "school", False),
    (r"(?:[\w.']+ )*(?:castle|tower|abbey|priory)", "ruin", False),
    (r"tannery|brewery|distillery|maltings?|foundry|warehouse|granary|smithy|inn", "building", False),
    (r"(?:railway |signal |coastguard )?station|lighthouse|fort|battery|barracks", "building", False),
    (r"ruins?|ruins? of [\w .']+", "ruin", False),
)]
_OLD_MAP_SAID = re.compile(r"\s*\((?:disused|in ruins?|ruins?|ruins? of|pumping)\)\s*", re.I)


def _old_map(row: dict) -> dict | None:
    """A label on the OS six-inch map of 1888-1913 for a works, kiln or mill, or anything it already called old,
    disused or ruined."""
    label = re.sub(r"\s+", " ", row.get("final_text") or "").strip()
    lower = label.lower()
    lat, lng = float(row["lat"]), float(row["lng"])
    if row.get("air_shafts"):
        parish = (row.get("parish") or "").strip().title()
        shafts = row["air_shafts"]
        return {
            "ref": str(row.get("pin_id") or ""),
            "name": f"Underground quarry, {parish}" if lower in _QUARRY_LABELS and parish else label,
            "kind": "underground quarry",
            "weight": 20,
            "evidence": f"The Ordnance Survey six-inch map of {OLD_MAP_YEARS[0]}-{OLD_MAP_YEARS[1]} marks \"{label}\" "
                        f"here, with {shafts} air shaft{'' if shafts == 1 else 's'} within {UNDERGROUND_WITHIN_M} m: worked "
                        "underground",
            "url": f"https://maps.nls.uk/projects/os1900/#zoom=17.0&lat={lat:.5f}&lon={lng:.5f}",
            "dates": [("on the map by", OLD_MAP_YEARS[1])],
        }
    ruined = bool(re.search(r"\bruins?\b|\(in ruins?\)", lower))
    disused = "(disused)" in lower or lower.startswith("old ")
    base = _OLD_MAP_SAID.sub(" ", label).strip()
    base = re.sub(r"^old\s+", "", base, flags=re.I).strip()
    if lower.endswith("(pumping)"):
        base += " (pumping)"
    found = next(((kind, lasting) for rx, kind, lasting in _OLD_MAP_KINDS if rx.search(base.replace(" (pumping)", ""))),
                 None)
    if not found or len(base) < 3:
        return None
    kind, lasting = found
    if not (ruined or disused or lasting):
        return None          # a gas works or a mill at work in 1900 is most likely long gone, or still at work
    name = _OLD_MAP_SAID.sub(" ", label).strip()
    name = name if any(c.isupper() for c in name) else name.title()
    then = " (in ruins even then)" if ruined else " (disused even then)" if disused else ""
    return {
        "ref": str(row.get("pin_id") or ""),
        "name": name,
        "kind": kind,
        "weight": 10 if ruined or disused else 6,
        "evidence": f"The Ordnance Survey six-inch map of {OLD_MAP_YEARS[0]}-{OLD_MAP_YEARS[1]} marks "
                    f"\"{label}\" here{then}",
        "url": f"https://maps.nls.uk/projects/os1900/#zoom=17.0&lat={lat:.5f}&lon={lng:.5f}",
        "dates": [("on the map by", OLD_MAP_YEARS[1])],
    }


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
                              CANMORE_WHERES, fields="CANMOREID,SITENUMBER,NMRSNAME,ALTNAME,SITETYPE,BROADCLASS,URL,ENTRYDATE,LASTUPDATE"),
            judge=_canmore,
        ),
        Dataset(
            key="coflein",
            label="Coflein (Wales)",
            licence="Open Government Licence v2.0",
            attribution="Site data from the National Monuments Record of Wales (RCAHMW)",
            home="https://coflein.gov.uk/",
            fetch=WFS("https://datamap.gov.wales/geoserver/wfs",
                      "geonode:rcahmw_nmrw_terrestrialsites_rcahmw_bng",
                      fields="nprn,name,site_type,lat,long,url,lastupdate"),
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
            remembers=True,
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
            key="old_maps",
            label="Old OS maps (1888-1913)",
            licence="CC BY-SA 4.0 (GB1900 gazetteer)",
            attribution="GB1900 gazetteer: Great Britain Historical GIS, University of Portsmouth, the GB1900 partners "
                        "and volunteers; served by the National Library of Scotland",
            home="https://www.visionofbritain.org.uk/data/#tabgb1900",
            fetch=OldMaps(),
            judge=_old_map,
            every_days=90,     # the transcription's finished: six pages a season is plenty
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
        item["dates"] = [[what, when] for what, when in item.get("dates") or () if when]
        item["reported_as"], item["reported"] = latest_date(item["dates"])
        if not item["name"]:
            item["name"] = f"Unnamed {item['kind']}"
        yield item
