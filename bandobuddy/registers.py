"""Registers of buildings that have closed, emptied or been given up: where viable places are.

- CQC: care homes and hospitals that have closed (deregistered, with nothing registered there since)
- NHS estates (ERIC): NHS sites standing wholly or mostly empty
- National Highways' Historical Railways Estate: tunnels and viaducts on closed railway lines
- The MOD's disposals: barracks, airfields and depots being given up

Each keeps what it last read in the data folder, so a file that hasn't changed isn't read again, and places
found from an address aren't looked up twice.
"""
from __future__ import annotations

import csv
import io
import json
import re
import tempfile
import threading
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urljoin

import requests

from . import geocode
from .config import USER_AGENT
from .geo import bng_to_wgs84, grid_ref_to_bng
from .osm import Cancelled

TIMEOUT = 120
Progress = Callable[[str, int, "int | None"], None]
HEADERS = {"User-Agent": USER_AGENT}
_ODS = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_ODS_P = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p"
_XLSX = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _stop(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


# -- reading spreadsheets ----------------------------------------------------------------------------

def ods_stream(fileobj, sheet: str) -> Iterator[list[str]]:
    """The rows of one sheet of an OpenDocument spreadsheet, read as it goes: CQC's unpacks to half a
    gigabyte of XML, far too much to hold."""
    with zipfile.ZipFile(fileobj).open("content.xml") as xml:
        current = None
        for event, el in ET.iterparse(xml, events=("start", "end")):
            if event == "start" and el.tag == f"{_ODS}table":
                current = el.get(f"{_ODS}name")
            elif event == "end" and el.tag == f"{_ODS}table-row":
                if current == sheet:
                    cells: list[str] = []
                    blanks = 0       # held back: rows end with blank cells "repeated" thousands of times
                    for cell in el:
                        if not cell.tag.endswith("table-cell"):
                            continue
                        text = "\n".join("".join(p.itertext()) for p in cell.iter(_ODS_P))
                        repeat = int(cell.get(f"{_ODS}number-columns-repeated", "1"))
                        if not text:
                            blanks += repeat
                            continue
                        cells.extend([""] * blanks + [text] * min(repeat, 100))
                        blanks = 0
                    yield cells
                el.clear()


def xlsx_rows(data: bytes) -> list[list[str]]:
    """The first sheet of an Excel workbook, as text."""
    z = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        shared = ["".join(t.text or "" for t in si.iter(f"{_XLSX}t"))
                  for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{_XLSX}si")]
    rows = []
    for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter(f"{_XLSX}row"):
        cells: dict[int, str] = {}
        for c in row.iter(f"{_XLSX}c"):
            col = 0
            for ch in re.match(r"[A-Z]+", c.get("r", "A")).group(0):
                col = col * 26 + ord(ch) - 64
            v, inline = c.find(f"{_XLSX}v"), c.find(f"{_XLSX}is")
            if c.get("t") == "s" and v is not None:
                cells[col - 1] = shared[int(v.text)]
            elif inline is not None:
                cells[col - 1] = "".join(t.text or "" for t in inline.iter(f"{_XLSX}t"))
            elif v is not None:
                cells[col - 1] = v.text or ""
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return rows


def _records(rows, must_have: str) -> list[dict]:
    """Rows below the header row (the first with `must_have` in it), as dicts."""
    rows = list(rows)
    at = next((i for i, r in enumerate(rows) if must_have in r), None)
    if at is None:
        raise RuntimeError(f"The register's columns have changed (no '{must_have}')")
    head = [h.strip() for h in rows[at]]
    return [dict(zip(head, r)) for r in rows[at + 1:] if any(x.strip() for x in r)]


def _link(session: requests.Session, page: str, pattern: str) -> str:
    """A file linked from a publication page (its name changes with every edition)."""
    resp = session.get(page, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    m = re.search(pattern, resp.text)
    if not m:
        raise RuntimeError(f"{page} doesn't link the file any more")
    return urljoin(page, m.group(1).replace("&amp;", "&"))


class Kept:
    """What a source last read, in the data folder: {"from": what it was read from, "at": when, "rows": [...]}.
    Re-yielded while the source hasn't changed, so the updater knows the places are still there."""

    def __init__(self, data_dir: Path | None, name: str):
        self.path = Path(data_dir) / f"{name}.json" if data_dir else None
        self.data: dict = {}
        if self.path and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.data = {}

    def fresh(self, source: str | None = None, max_age_days: float | None = None) -> list[dict] | None:
        """The kept rows, if they came from `source` (and are younger than `max_age_days`)."""
        if "rows" not in self.data or (source is not None and self.data.get("from") != source):
            return None
        if max_age_days is not None:
            try:
                if datetime.fromisoformat(self.data["at"]) < datetime.now() - timedelta(days=max_age_days):
                    return None
            except (KeyError, ValueError):
                return None
        return self.data["rows"]

    def keep(self, source: str, rows: list[dict], **extra) -> None:
        self.data = {"from": source, "at": datetime.now().isoformat(timespec="seconds"), "rows": rows, **extra}
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)


def _download(session: requests.Session, url: str, cancel) -> tempfile.SpooledTemporaryFile:
    """A big file, to disk past 8 MB rather than into memory."""
    out = tempfile.SpooledTemporaryFile(max_size=8 << 20)
    with session.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True) as resp:
        resp.raise_for_status()
        for i, chunk in enumerate(resp.iter_content(1 << 16)):
            if i % 64 == 0:
                _stop(cancel)
            out.write(chunk)
    out.seek(0)
    return out


# -- CQC: closed care homes and hospitals --------------------------------------------------------------

_norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())          # noqa: E731
_pc = lambda s: re.sub(r"\s", "", (s or "").upper())                  # noqa: E731
CQC_HOSPITALS = re.compile(r"hospital|hospice|mental health - community & (?:hospital|residential)", re.I)
CQC_HOSPITAL_NAMES = re.compile(r"hospital|hospice|infirmary|asylum|nursing home|sanatorium", re.I)


def _cqc_date(value: str) -> date | None:
    try:
        d, m, y = (int(x) for x in (value or "").split("/"))
        return date(y, m, d)
    except ValueError:
        return None


class CqcClosures:
    """The Care Quality Commission's deactivated locations: every care home and hospital it no longer
    regulates. Kept only where the building is closed rather than under new management: the last
    registration at the property ended, and nothing's registered at that address now (the directory of
    what's active). Care homes with 20 or more beds, and hospitals: small supported-living homes are
    ordinary houses that go back to being homes."""

    page = "https://www.cqc.org.uk/about-us/transparency/using-cqc-data"
    min_beds = 20

    def __call__(self, session: requests.Session, progress: Progress, cancel=None,
                 data_dir: Path | None = None) -> Iterator[dict]:
        kept = Kept(data_dir, "cqc_closures")
        progress("looking for this month's files", 0, None)
        closed_url = _link(session, self.page, r'href="([^"]+Deactivated_Locations\.ods)"')
        active_url = _link(session, self.page, r'href="([^"]+CQC_directory\.csv)"')
        rows = kept.fresh(f"{closed_url} {active_url}")
        if rows is None:
            progress("reading CQC's closed locations (about 30 MB)", 0, None)
            active = self._active(session, active_url, cancel)
            with _download(session, closed_url, cancel) as f:
                rows = self._closed(f, active, progress, cancel)
            kept.keep(f"{closed_url} {active_url}", rows)
        progress("closed care homes and hospitals", len(rows), len(rows))
        yield from rows

    def _active(self, session: requests.Session, url: str, cancel) -> dict[str, list[tuple[str, str]]]:
        resp = session.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        _stop(cancel)
        active: dict[str, list[tuple[str, str]]] = {}
        for r in _records(csv.reader(io.StringIO(resp.content.decode("utf-8-sig", "replace"))), "Postcode"):
            active.setdefault(_pc(r.get("Postcode")), []).append((_norm((r.get("Address") or "").split(",")[0]),
                                                                _norm(r.get("Name"))))
        return active

    def _closed(self, f, active: dict, progress: Progress, cancel) -> list[dict]:
        latest: dict[str, dict] = {}
        for i, r in enumerate(_records_stream(ods_stream(f, "Deactivated_Locations"), "Location ID")):
            if i % 5000 == 0:
                _stop(cancel)
                progress("reading CQC's closed locations", i, None)
            care_home = r.get("Care home?") == "Y"
            try:
                beds = int(r.get("Care homes beds at point location de-activated") or 0)
            except ValueError:
                beds = 0
            hospital = not care_home and CQC_HOSPITALS.search(r.get("Location Primary Inspection Category") or "") \
                and ("NHS" in (r.get("Location Type/Sector") or "") or CQC_HOSPITAL_NAMES.search(r.get("Location Name") or ""))
            if not (care_home and beds >= self.min_beds or hospital):
                continue
            ended = _cqc_date(r.get("Location HSCA End Date"))
            try:
                lat, lng = float(r["Location Latitude"]), float(r["Location Longitude"])
            except (KeyError, ValueError):
                continue
            if not ended:
                continue
            key = r.get("Location UPRN ID") or _pc(r.get("Location Postal Code")) + _norm(r.get("Location Street Address"))
            row = {"ref": r["Location ID"], "name": (r.get("Location Name") or "").strip(),
                   "address": ", ".join(x for x in (r.get("Location Street Address"), r.get("Location Address Line 2"),
                                                     r.get("Location City")) if x),
                   "postcode": r.get("Location Postal Code") or "", "lat": lat, "lng": lng, "care_home": care_home,
                   "beds": beds, "category": r.get("Location Primary Inspection Category") or "",
                   "sector": r.get("Location Type/Sector") or "", "ended": ended.isoformat()}
            if key not in latest or row["ended"] > latest[key]["ended"]:
                latest[key] = row
        rows = []
        for row in latest.values():
            line, name = _norm(row["address"].split(",")[0]), _norm(row["name"])
            still = any((line and al and (line == al or line in al or al in line)) or (name and name == an)
                        for al, an in active.get(_pc(row["postcode"]), []))
            if not still:
                rows.append(row)
        return rows


def _records_stream(rows: Iterator[list[str]], must_have: str) -> Iterator[dict]:
    """Like _records, for a sheet read as it goes."""
    head = None
    for r in rows:
        if head is None:
            if must_have in r:
                head = [h.strip() for h in r]
            continue
        if any(x.strip() for x in r):
            yield dict(zip(head, r))


# -- NHS estates: sites standing empty ------------------------------------------------------------------

class NhsEstates:
    """NHS England's Estates Returns Information Collection: every NHS site, each year, with how much of
    it is unoccupied. Kept: sites that are wholly unoccupied, and those where most of the floor space stands
    empty (an old hospital wound down around a few services). Placed by postcode."""

    series = "https://digital.nhs.uk/data-and-information/publications/statistical/estates-returns-information-collection"
    mostly = 0.5
    min_empty_m2 = 1000

    def __call__(self, session: requests.Session, progress: Progress, cancel=None,
                 data_dir: Path | None = None) -> Iterator[dict]:
        kept = Kept(data_dir, "nhs_estates")
        csv_url = self._latest(session)
        rows = kept.fresh(csv_url)
        if rows is None:
            rows = self._read(session, csv_url, progress, cancel)
            kept.keep(csv_url, rows)
        progress("empty NHS sites", len(rows), len(rows))
        yield from rows

    def _latest(self, session: requests.Session) -> str:
        """The newest edition's site data: the series lists an edition before it's out, so the first that has one."""
        resp = session.get(self.series, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        editions = list(dict.fromkeys(re.findall(r'href="([^"]*summary-page-and-dataset-for-eric-\d{4}-\d{2})"', resp.text)))
        for edition in sorted(editions, reverse=True)[:3]:
            page = session.get(urljoin(self.series, edition), headers=HEADERS, timeout=TIMEOUT)
            m = re.search(r'href="([^"]+Site(?:%20|\s)data\.csv)"', page.text) if page.status_code == 200 else None
            if m:
                return urljoin(self.series, m.group(1))
        raise RuntimeError("No edition of the NHS estates return links its site data")

    @staticmethod
    def _number(value: str) -> float:
        try:
            return float((value or "").replace(",", ""))
        except ValueError:
            return 0.0

    def _read(self, session: requests.Session, url: str, progress: Progress, cancel) -> list[dict]:
        resp = session.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        text = resp.content.decode("cp1252", "replace")
        records = list(csv.DictReader(io.StringIO(text)))
        col = lambda start: next((c for c in (records[0] if records else {}) if c.startswith(start)), start)  # noqa: E731
        unocc, empty, gia, pre = (col("Internal floor area - unoccupied"), col("Floor area - empty"),
                                  col("Gross internal floor area"), col("Age profile - pre 1948"))
        year = re.search(r"(\d{4})_(\d{2})", url)
        rows = []
        wanted = []
        for r in records:
            whole = self._number(r.get(unocc)) > 0 or r.get("Site Type") == "Unoccupied"
            floor, gone = self._number(r.get(gia)), self._number(r.get(empty))
            mostly = floor and gone >= self.min_empty_m2 and gone / floor >= self.mostly
            if whole or mostly:
                wanted.append((r, whole))
        for n, (r, whole) in enumerate(wanted):
            _stop(cancel)
            progress("placing empty NHS sites by postcode", n, len(wanted))
            point = geocode.postcode_point(session, r.get("Post Code") or "") if r.get("Post Code") else None
            if not point:
                continue
            rows.append({"ref": r.get("Site Code") or r.get("Site Name"), "name": (r.get("Site Name") or "").strip(),
                         "trust": (r.get("Trust Name") or "").strip(), "site_type": r.get("Site Type") or "",
                         "whole": whole, "unoccupied_m2": self._number(r.get(unocc)),
                         "empty_m2": self._number(r.get(empty)), "floor_m2": self._number(r.get(gia)),
                         "pre_1948": self._number(r.get(pre)), "year": f"{year.group(1)}/{year.group(2)}" if year else "",
                         "postcode": r.get("Post Code") or "", "lat": point[0], "lng": point[1]})
        return rows


# -- National Highways' Historical Railways Estate -------------------------------------------------------

class RailwayEstate:
    """The structures National Highways looks after on railway lines closed long ago: tunnels and viaducts
    (not the two thousand road bridges), each with a six-figure grid reference and its old line's name."""

    url = "https://hre.s3.eu-west-2.amazonaws.com/HRE+structures.xlsx"
    kinds = ("Tunnel", "Viaduct")

    def __call__(self, session: requests.Session, progress: Progress, cancel=None,
                 data_dir: Path | None = None) -> Iterator[dict]:
        progress("reading the list of structures", 0, None)
        resp = session.get(self.url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        seen = set()
        for r in _records(xlsx_rows(resp.content), "StructureType"):
            kind = (r.get("StructureType") or "").strip()
            grid = grid_ref_to_bng(r.get("OSReference") or "")
            ref = (r.get("EPIMRef") or "").strip() or f"{r.get('ELR')}-{r.get('Mileage')}-{r.get('Chainage')}"
            if kind not in self.kinds or not grid or ref in seen:
                continue
            seen.add(ref)
            lat, lng = bng_to_wgs84(*grid)
            yield {"ref": ref, "name": (r.get("Name") or "").strip(), "kind": kind.lower(),
                   "line": (r.get("ELR.LineName") or "").strip(), "path": (r.get("RPL or Sustrans?") or "").strip(),
                   "status": (r.get("Status") or "").strip(), "os_ref": r.get("OSReference"), "lat": lat, "lng": lng}


# -- The MOD's disposals ---------------------------------------------------------------------------------

# A parcel that's only land: a field, a training area, a strip by a road.
MOD_LAND = re.compile(r"^(?:misc )?land\b|\bland (?:at|adj|behind|off|by|to|north|south|east|west)\b|retained land"
                      r"|pasture|paddock|recreation(?:al)? (?:field|ground)|playing fields?|sports? pitch|\bisland\b"
                      r"|training area|sewerage|sewage|grazing|woodland|allotment|\bverge\b", re.I)
MOD_BUILDINGS = re.compile(r"\bbldgs\b|buildings", re.I)


class MoDisposals:
    """The Ministry of Defence's list of sites it's disposing of, as it gives the House of Commons: barracks,
    airfields, ranges and depots, each with the year it goes. It gives no positions, so each is looked up by
    name and town (OpenStreetMap's Nominatim, a second apart), and kept. If Nominatim can't answer (it's down,
    or has asked for a pause), the rest wait for the next run: only a search that found nothing is remembered."""

    page = "https://www.gov.uk/government/publications/disposal-database-house-of-commons-report"

    def __call__(self, session: requests.Session, progress: Progress, cancel=None,
                 data_dir: Path | None = None) -> Iterator[dict]:
        kept = Kept(data_dir, "mod_disposals")
        places = dict(kept.data.get("places") or {})
        url = _link(session, self.page, r'href="([^"]+\.ods)"')
        resp = session.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        records = _records(ods_stream(io.BytesIO(resp.content), _first_sheet(resp.content)), "Primary Establishment Name")
        rows, seen, searching = [], set(), True
        for n, r in enumerate(records):
            _stop(cancel)
            progress("finding each MOD site", n, len(records))
            ref = (r.get("ID") or "").strip()
            parcel = (r.get("Primary Parcel Name") or "").strip()
            same = ((r.get("Primary Establishment Name") or "").strip().upper(), parcel.upper())
            if not ref or same in seen or (MOD_LAND.search(parcel) and not MOD_BUILDINGS.search(parcel)):
                continue
            seen.add(same)
            if ref not in places and searching:
                try:
                    places[ref] = self._place(session, r)
                except (requests.RequestException, ValueError):
                    searching = False
                    progress("Nominatim isn't answering: the other MOD sites are looked up next time", n, len(records))
            if not places.get(ref):
                continue
            rows.append({"ref": ref, "establishment": (r.get("Primary Establishment Name") or "").strip(),
                         "parcel": (r.get("Primary Parcel Name") or "").strip(), "status": (r.get("Status") or "").strip(),
                         "year": (r.get("Disposal From") or "").strip(), "town": (r.get("Town") or "").strip(),
                         "county": (r.get("County") or "").strip(), "area_ha": (r.get("Total Area (ha)") or "").strip(),
                         "lat": places[ref][0], "lng": places[ref][1]})
        kept.keep(url, rows, places=places)
        yield from rows

    @staticmethod
    def _place(session: requests.Session, r: dict) -> list[float] | None:
        name, parcel = (r.get("Primary Establishment Name") or "").title(), (r.get("Primary Parcel Name") or "").title()
        town, county = r.get("Town") or "", r.get("County") or ""
        for q in (f"{name}, {town}", f"{parcel}, {town}", f"{r.get('Address') or ''}, {town}, {county}"):
            hit = geocode.search(q, session)
            if hit:
                return [hit["lat"], hit["lng"]]
        return None


def _first_sheet(data: bytes) -> str:
    root_names = re.findall(rb'table:name="([^"]+)"', zipfile.ZipFile(io.BytesIO(data)).read("content.xml")[:200000])
    return root_names[0].decode("utf-8") if root_names else ""
