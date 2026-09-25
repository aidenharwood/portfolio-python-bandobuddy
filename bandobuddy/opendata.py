"""Open national registers: places OpenStreetMap and Wikidata miss.

Every dataset here is free to reuse with attribution, and each one says where to fetch it, which
records are worth keeping and how strong that evidence is. The updater treats them all the same,
so adding a register is a matter of describing it here.

A record in a national register means "this exists (or existed)", not "this is abandoned", so most
of them are weak leads. Military and underground records are the exception: an observation post or
a colliery shaft is disused by definition.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Callable, Iterator

import requests

from .config import USER_AGENT
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
    name = (row.get("NMRSNAME") or "").strip().title()
    verdict = judge_segments(row.get("SITETYPE") or "", name)
    if not verdict:
        return None
    weight, kind, what = verdict
    return {
        "ref": str(row.get("CANMOREID") or row.get("SITENUMBER") or "").strip(),
        "name": name,
        "kind": kind,
        "evidence": f"Canmore records {_a(what)} here",
        "weight": weight,
        "url": row.get("URL"),
    }


def _coflein(row: dict) -> dict | None:
    name = (row.get("name") or "").strip()
    verdict = judge_segments(row.get("site_type") or "", name)
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
                              CANMORE_WHERES, fields="CANMOREID,SITENUMBER,NMRSNAME,SITETYPE,BROADCLASS,URL"),
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
