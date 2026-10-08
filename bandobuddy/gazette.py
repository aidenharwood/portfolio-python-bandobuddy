"""The Crown's disclaimers of land that belonged to dissolved companies, from The Gazette (notice code 2603).

When a company is dissolved, whatever it still owned passes to the Crown. Land and buildings nobody wants (a
derelict chapel, a burnt-out mill, a strip of verge) the Treasury Solicitor disclaims, in a notice in The Gazette
giving the company, its number, the title, and the property with its postcode. There are about 24,000 since 2008.
This reads the freeholds among them whose notice mentions a building worth a trip, about 1,000: a disclaimed
lease only goes back to the landlord. Each notice is a page of its own, read ten seconds apart as the Gazette's
robots.txt asks, so the first run takes about three hours; it keeps what it has read in the data folder, so each
notice is read once, ever, and later runs only read what's new. The notices are published under the Open
Government Licence."""
from __future__ import annotations

import html
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

import requests

from . import geocode
from .config import USER_AGENT
from .osm import Cancelled

SEARCH_URL = "https://www.thegazette.co.uk/all-notices/notice/data.json"
NOTICE_URL = "https://www.thegazette.co.uk/notice/{}"
NOTICE_CODE = "2603"          # "Notice of disclaimer" (Companies regulation)
# Asked for in the notice's text: a building, or what it was. The company's name counts too ("Harris (Holmes
# Chapel) Limited"), so what comes back is judged on its property alone.
BUILDING_WORDS = ("chapel", "church", "mill", "inn", '"public house"', "tavern", "hotel", "school", "hall", "works",
                  "factory", "warehouse", "cinema", "theatre", "barn", "farmhouse", "derelict", "former", "club",
                  "hospital", "station", "bank")
MEMORY = "gazette_disclaimers.json"

Progress = Callable[[str, int, int | None], None]


@dataclass
class Disclaimers:
    gap_s: float = 10            # robots.txt: Crawl-delay: 10
    page_size: int = 100
    per_run: int = 1500          # notices read each run, at most: the first run reads them all
    words: tuple = BUILDING_WORDS

    def _wait(self, cancel, seconds: float) -> None:
        if (cancel or threading.Event()).wait(seconds):
            raise Cancelled()

    def _get(self, session: requests.Session, url: str, cancel, **kw) -> requests.Response:
        self._wait(cancel, self.gap_s)       # before every request, the first included: runs can follow each other
        resp = session.get(url, headers={"User-Agent": USER_AGENT, **kw.pop("headers", {})}, timeout=60, **kw)
        resp.raise_for_status()
        return resp

    def __call__(self, session: requests.Session, progress: Progress, cancel=None,
                 data_dir: Path | None = None) -> Iterator[dict]:
        path = Path(data_dir) / MEMORY if data_dir else None
        memory = _load(path)
        # What's been read before comes first, read again from the words kept: how it's read and judged can
        # change without asking the Gazette again.
        for kept in list(memory["notices"].values()):
            notice = self._notice(session, kept, memory["places"])
            if notice.get("lat") is not None:
                yield notice
        _save(path, memory)
        # Then everything that matches, newest first, a page at a time...
        listed, page, total = [], 1, None
        while True:
            data = self._get(session, SEARCH_URL, cancel, headers={"Accept": "application/json"},
                             params={"noticetypes": NOTICE_CODE, "text": f"Freehold AND ({' OR '.join(self.words)})",
                                     "results-page-size": self.page_size, "results-page": page,
                                     "sort-by": "latest-date"}).json()
            entries = data.get("entry") or []
            total = _int(data.get("f:total"))
            listed += [(e["id"].rsplit("/", 1)[1], (e.get("published") or "")[:10]) for e in entries if e.get("id")]
            progress("listing the Gazette's disclaimers, ten seconds between pages", len(listed), total)
            if len(entries) < self.page_size or (total is not None and len(listed) >= total):
                break
            page += 1
        # ...and those not read yet, up to a run's worth.
        # (One kept without its words, by an early version, is read again.)
        unread = [(nid, published) for nid, published in listed
                  if not (memory["notices"].get(nid) or {}).get("text")][:self.per_run]
        for n, (nid, published) in enumerate(unread, 1):
            resp = self._get(session, NOTICE_URL.format(nid), cancel)
            kept = {"id": nid, "published": published, "url": NOTICE_URL.format(nid), "text": notice_text(resp.text)}
            memory["notices"][nid] = kept
            notice = self._notice(session, kept, memory["places"])
            _save(path, memory)
            progress("reading the disclaimers, ten seconds apart", n, len(unread))
            if notice.get("lat") is not None:
                yield notice

    def _notice(self, session: requests.Session, kept: dict, places: dict) -> dict:
        """A notice's fields, from its words, and where it is."""
        notice = {**(parse_notice(kept.get("text") or "") or {}),
                  **{k: kept.get(k) for k in ("id", "published", "url")}}
        place = self._place(session, notice, places)
        if place:
            notice["lat"], notice["lng"] = place
        return notice

    @staticmethod
    def _place(session: requests.Session, notice: dict, places: dict) -> tuple[float, float] | None:
        """Where it is: the middle of its postcode (postcodes.io), or failing one, its address found by Nominatim,
        a second apart, if it's a building by its name. Each kept, so asked once."""
        if notice.get("postcode"):
            key = notice["postcode"]
            if key not in places:
                places[key] = geocode.postcode_point(session, key)
        elif _BUILDING_NAMED.search((notice.get("property") or "").split(",")[0]):
            key = f"address:{notice['property']}"
            if key not in places:
                try:
                    hit = geocode.search(notice["property"], session)
                except (geocode.Busy, requests.RequestException):
                    return None                   # asked again next run
                places[key] = (hit["lat"], hit["lng"]) if hit else None
        else:
            return None
        return tuple(places[key]) if places.get(key) else None


def notice_text(page: str) -> str:
    """The words of the notice itself, from its page: from "NOTICE OF DISCLAIMER" to the end of the notice."""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S | re.I)
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text)))
    start = text.upper().find("NOTICE OF DISCLAIMER")
    if start < 0:
        return ""
    end = text.find(" Actions Save notice", start)
    return text[start:end if end > 0 else None].strip()


_FIELDS = {
    "company": r"Company Name:\s*(.+?)\s*\.?\s*(?:Previous Names? of (?:the )?Company:|Company Number:|Company No)",
    "number": r"Company (?:Number|No)\.?:\s*([A-Z]{0,2}\d{5,8})",
    "interest": r"Interest:\s*(Freehold|Leasehold|Commonhold)",
    "title": r"Title [Nn]umbers?:\s*([A-Z]{1,4}\s?\d+)",
    "dissolved": r"Dissolution Date:\s*(\d{1,2} \w+ \d{4})",
    # ...up to the next field, whichever comes next: they come in more than one order.
    "property": r"Property:\s*(.+?)\s*(?:Title [Nn]umbers?:|Interest:|Lease:|Dissolution Date:|Company (?:Name|Number):"
                r"|(?:The )?(?:Treasury|Duchy) Solicitor|\b2\.?\s+In pursuance|$)",
}
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?) ?(\d[A-Z]{2})\b")
# How a property's given, around what it is: "The Property situated at ... being the land comprised in ...",
# "Interest in Lease relating to the premises known as ...", "Lease of public house at ...".
_LEAD = re.compile(r"^.*?\b(?:situated at|situate at|known as|described as)\s+|^(?:the )?(?:property|premises)\s+(?:being\s+)?"
                   r"|^lease of [\w ]+? at\s+", re.I)
_TAIL = re.compile(r"\s*,?\s*being (?:the|all the) land .*$|\s*,?\s*(?:and )?(?:as )?(?:the same is )?registered .*$",
                   re.I)


def parse_notice(text: str) -> dict | None:
    """The fields of a disclaimer notice; None if it isn't one, or doesn't say what property."""
    if not text:
        return None
    out = {}
    for key, rx in _FIELDS.items():
        m = re.search(rx, text, re.S)
        out[key] = m.group(1).strip() if m else None
    if not out["property"]:
        return None
    prop = _TAIL.sub("", _LEAD.sub("", out["property"].strip(' "\''), count=1)).strip(' .,"\'')
    out["property"] = prop
    pc = _POSTCODE.search(prop.upper())
    out["postcode"] = f"{pc.group(1)} {pc.group(2)}" if pc else None
    out["dissolved"] = _day(out["dissolved"])
    # Signed at the end, whoever signed it: the last date in the notice.
    dates = re.findall(rf"\b(\d{{1,2}} (?:{_MONTHS}) \d{{4}})\b", text)
    out["signed"] = _day(dates[-1]) if dates else None
    return out


# Worth finding on the map without a postcode: a building, by its own name. Not "land on the south side of...".
_BUILDING_NAMED = re.compile(r"^(?!land\b|any\b|interest\b).*\b(?:chapel|church|mill|inn|public house|tavern|hotel"
                             r"|school|hall|works|factory|warehouse|cinema|theatre|barn|farmhouse|club|hospital|station"
                             r"|bank)\b", re.I)


def _day(text: str | None) -> str | None:
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text or "", fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load(path: Path | None) -> dict:
    empty = {"notices": {}, "places": {}}
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
