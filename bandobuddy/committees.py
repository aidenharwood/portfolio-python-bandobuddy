"""Planning committee reports: what a council's own planning officers write about a site.

An officer's report describes the site before weighing up the proposal, and says plainly when the
building on it stands empty, unfinished or falling down. Golden Hill, near Romsey "was built as a
substantial, single residence but has not been occupied since its construction in 2004". No register
records that; the report is the only place it's written down.

Most councils in England and Wales publish their committee papers with ModernGov, which has a free-text
search over every document it holds. So this asks each council's ModernGov for planning committee papers
that use the phrases officers use for empty and derelict buildings, reads the reports that match (a few
hundred kilobytes each), and keeps the sentence where the phrase is said of the site.
"""
from __future__ import annotations

import html
import io
import json
import logging
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import quote, urljoin

import requests

from . import geocode
from .config import USER_AGENT
from .geo import bng_to_wgs84, haversine_m
from .osm import Cancelled

TIMEOUT = 60
Progress = Callable[[str, int, "int | None"], None]

# What officers write when the building on the site stands empty, unfinished or falling down. Asked for
# together ("a" OR "b"...), so each council is one search a week, plus a page per ten papers found.
PHRASES = (
    "has not been occupied since", "has never been occupied", "have never been occupied", "has not been lived in",
    "vacant since", "has been vacant for", "has remained vacant", "has stood vacant",
    "has been empty since", "has been empty for", "has remained empty", "has stood empty", "has been unoccupied",
    "vacant and derelict", "derelict and vacant", "in a derelict state", "in a derelict condition", "has become derelict",
    "fallen into disrepair", "fallen into dereliction", "state of disrepair", "poor state of repair", "dilapidated",
    "boarded up", "safety fencing", "has been redundant", "has been disused", "out of use since",
    "has not been used since", "partially constructed dwelling", "partially built dwelling", "unfinished dwelling",
    # Burnes Shipyard, Bosham: "The site has been redundant for more than twenty years, with the buildings in a
    # poor state of repair with the site enclosed with safety fencing."
)
# Planning committees go by many names: "Southern Area Planning Committee", "Development Control",
# "Plans Sub-Committee", "Regulatory Committee"... but not the ones that write planning policy.
PLANNING_COMMITTEE = re.compile(r"plann|development|\bplans\b|applications|regulatory|\bDC\b", re.I)
NOT_APPLICATIONS = re.compile(r"polic|task|framework|working|advisory|forum|steering|liaison|scrutiny|panel", re.I)
COMMITTEES_EVERY_DAYS = 90       # how often to look again at which committees a council has
COUNCILS_AT_ONCE = 8             # each a website of its own, asked a request a second
READER = 2                       # raised when the phrases or the way reports are read change
FIRST_SINCE = date(2016, 1, 1)   # a first look goes back ten years; older papers are mostly overtaken
OVERLAP_DAYS = 35                # later looks go back a few weeks before the last, for papers published late
MAX_PAGES = 40                   # ten results a page
MAX_REPORT_BYTES = 15 << 20      # a report is a few hundred kilobytes; whole agenda packs (with plans) aren't read
_POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?) ?(\d[A-Z]{2})\b")


def _stop(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def is_planning(committee: str) -> bool:
    return bool(PLANNING_COMMITTEE.search(committee)) and not NOT_APPLICATIONS.search(committee)


def search_url(base: str, since: date, until: date, page: int, committee: str | None = None,
               phrases=PHRASES) -> str:
    """ModernGov's document search, as its own search form sends it: any of the phrases, in one committee's
    papers (or everyone's) dated between the two days."""
    query = " OR ".join(f'"{p}"' for p in phrases)
    day = lambda d: quote(f"{d:%d/%m/%Y}", safe="")   # noqa: E731
    only = f"&CI={committee}" if committee else ""
    return (f"{base}/ieSearchResults2.aspx?SS={quote(query)}&SD={day(since)}&ED={day(until)}"
            f"&DT=3{only}&ADV=1&CA=false&SB=true&PG={page}")


_SELECT = re.compile(r'<select[^>]*\bid="CommitteeId"[^>]*>(.*?)</select>', re.S | re.I)
_OPTION = re.compile(r'<option[^>]*value="(\d+)"[^>]*>([^<]*)', re.I)


def planning_committees(form: str) -> list[str] | None:
    """The planning committees in ModernGov's advanced search form, past ones too; None if it has no list."""
    select = _SELECT.search(form)
    if not select:
        return None
    return [cid for cid, name in _OPTION.findall(select.group(1))
            if cid != "0" and is_planning(html.unescape(name)) and not _wound_up(name)]


def _wound_up(name: str) -> bool:
    """"Development Control (1998-2003)": a committee that ended before the papers worth reading begin."""
    m = re.search(r"\b(?:19|20)\d\d\s*[-–]\s*((?:19|20)\d\d)\b", name)
    return bool(m) and int(m.group(1)) < FIRST_SINCE.year


_MEETING = re.compile(r'<a\s[^>]*href="ieListDocuments\.aspx\?CId=(\d+)&(?:amp;)?MI[Dd]=(\d+)"[^>]*>\s*'
                      r'(\d\d)&#47;(\d\d)&#47;(\d{4})\s*-\s*(.*?)\s*\(\d+\)\s*</a>', re.S)
_DOC = re.compile(r'<a\s[^>]*href="((?:mgConvert2PDF\.aspx\?ID=\d+[^"#]*|documents/[^"#]+\.pdf))(?:#[^"]*)?"[^>]*>'
                  r'(.*?)</a>', re.S | re.I)
_ITEM = re.compile(r'<a\s[^>]*href="ieListDocuments\.aspx\?[^"]*#AI\d+"[^>]*>(.*?)</a>', re.S)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_results(page: str, base: str, number: int = 1) -> tuple[list[dict], bool]:
    """The papers on one page of ModernGov's search results, and whether there's another page (a link to it)."""
    hits = []
    starts = list(_MEETING.finditer(page))
    for i, m in enumerate(starts):
        block = page[m.end():starts[i + 1].start() if i + 1 < len(starts) else len(page)]
        doc = _DOC.search(block)
        if not doc:
            continue
        item = _ITEM.search(block)
        try:
            when = date(int(m.group(5)), int(m.group(4)), int(m.group(3))).isoformat()
        except ValueError:
            when = ""
        hits.append({"committee": _text(m.group(6)), "meeting": when,
                     "item": _text(item.group(1)) if item else "",
                     "url": urljoin(base + "/", html.unescape(doc.group(1))),
                     "title": _text(doc.group(2))})
    return hits, bool(re.search(rf"PG={number + 1}\b", page))


# -- reading a report ---------------------------------------------------------------------------------

_LABEL = (r"(?:APPLICATION\s*(?:NO\.?|NUMBER|REF(?:ERENCE)?|TYPE)|APPLICANT|AGENT|REGISTERED|RECEIVED|"
          r"SITE(?:\s+ADDRESS)?|LOCATION|ADDRESS|PROPOSAL|DESCRIPTION|AMENDMENTS|CASE\s+OFFICER|OFFICER|WARD|PARISH|"
          r"RECOMMENDATION|TARGET\s+DATE|Applicant|Agent|Proposal|Ward|Parish|Case\s+[Oo]fficer|Recommendation)")
_SITE = re.compile(r"(?:^|\n)[ \t]*(?:SITE(?:\s+ADDRESS)?|Site(?:\s+[Aa]ddress)?|LOCATION|Location|ADDRESS|Address)"
                   r"[ \t]*[:\-]?[ \t]+(.+?)(?=\n[ \t]*" + _LABEL + r"\b|\n[ \t]*\n|$)", re.S)
_PROPOSAL = re.compile(r"(?:^|\n)[ \t]*(?:PROPOSAL|Proposal|DESCRIPTION|Description(?: of [Dd]evelopment)?)"
                       r"[ \t]*[:\-]?[ \t]+(.+?)(?=\n[ \t]*" + _LABEL + r"\b|\n[ \t]*\n|$)", re.S)
# A planning reference standing on its own: "BO/21/00620/FUL", "22/00362/FULLS", "P/2024/0156/OUT".
_BARE_REF = re.compile(r"\b(?:[A-Z]{1,4}/)?\d{2,4}/\d{3,6}/[A-Z]{2,6}\b")
# Where the site is, in grid metres: "Map Ref (E) 480388 (N) 104217", "Easting: 480388 Northing: 104217".
_GRID = re.compile(r"(?:\(E\)|Easting:?)\s*(\d{6})\s*(?:\(N\)|Northing:?)\s*(\d{6,7})", re.I)
_REF = re.compile(r"(?:APPLICATION|Application)\s*(?:NO\.?|No\.?|NUMBER|Number|REF(?:ERENCE)?|Ref(?:erence)?)"
                  r"\s*[:.]?\s*([A-Z0-9][A-Za-z0-9/._-]{4,})")
# Said of something other than the site, or of who may live there rather than whether anyone does...
_ELSEWHERE = re.compile(r"neighbour|adjacent|adjoining|next door|nearby|opposite|elsewhere|other (?:propert|building|site)"
                        r"|surrounding|in the (?:area|vicinity|village|town)|across the|agricultur|forestry|occupancy"
                        r"|holiday|by a person|in breach|no evidence|has not been (?:demonstrated|shown)|\bif\b|whether",
                        re.I)
# ...or the words of a policy or a rule, quoted to weigh the proposal against: "will only be permitted where
# ... the premises has been vacant for at least 12 months", "Class MA ... vacant for a continuous period".
_RULE = re.compile(r"permitted|unless|demonstrat|continuous|at least|prior approval|\bclass [a-z]{1,2}\b|\bpolic(?:y|ies)\b"
                   r"|criteri|\bmust\b|\bshould\b|\bwill only\b|\bmay\b|\brequire", re.I)
LONGEST_SENTENCE = 320           # an officer says it in a sentence; longer is a list or a quoted rule
# A full stop that ends a sentence: not the one in "No.", "e.g.", "approx." or "St.".
_ENDS = re.compile(r"(?<!\bNo)(?<!\bNos)(?<!\bapprox)(?<!\bApprox)(?<!\bca)(?<!\bSt)(?<!\bRd)(?<!\bMr)(?<!\bMrs)"
                   r"(?<!\b[A-Za-z])[.!?](?=\s+[A-Z0-9\"'(]|\s*$)")
# The building's gone: "vacant since 2013, when the former buildings on site were demolished".
_GONE = re.compile(r"demolished|cleared|knocked down|razed|burnt down|burned down", re.I)


def read_pdf(data: bytes) -> list[str]:
    """Each page's text. pypdf reads a report in a fraction of a second."""
    from pypdf import PdfReader   # only this source needs it
    logging.getLogger("pypdf").setLevel(logging.ERROR)   # councils' PDFs are often a little off; it copes
    reader = PdfReader(io.BytesIO(data))
    return [(p.extract_text() or "") for p in reader.pages]


def _sentence(text: str, at: int, end: int) -> str:
    """The sentence around a phrase: back to the last full stop, on to the next, without its paragraph
    number ("2.1 Golden Hill is...")."""
    before = re.sub(r"\s+", " ", text[max(0, at - 400):at])
    after = re.sub(r"\s+", " ", text[end:end + 400])
    ends = list(_ENDS.finditer(before))
    head = before[ends[-1].end():] if ends else before[-200:]
    head = head.rsplit(";", 1)[-1]           # one clause of a list: "(ix) no adverse impact...; The site..."
    stop = _ENDS.search(after)
    tail = (after[:stop.end()] if stop else after[:200]).split(";", 1)[0]
    sentence = (head + re.sub(r"\s+", " ", text[at:end]) + tail).strip()
    return re.sub(r"^\d+(?:\.\d+)+\.?\s+", "", sentence)


def find_statements(pages: list[str], phrases=PHRASES) -> list[dict]:
    """Where a report says the site is empty, unfinished or derelict: the sentence, its page, and the site
    it's said of (the nearest application header before it, as one paper can cover several)."""
    text, starts = "", []
    for page in pages:
        starts.append(len(text))
        text += page + "\n"
    pattern = re.compile("|".join(r"\s+".join(map(re.escape, p.split())) for p in phrases), re.I)
    found = []
    for m in pattern.finditer(text):
        sentence = _sentence(text, m.start(), m.end())
        if _ELSEWHERE.search(sentence) or _RULE.search(sentence) or _GONE.search(sentence) \
                or sentence.endswith("?") or len(sentence) > LONGEST_SENTENCE:   # a question is a commenter's
            continue
        found.append({"phrase": re.sub(r"\s+", " ", m.group(0)).lower(), "sentence": sentence,
                      "page": sum(1 for st in starts if st <= m.start()), **_header(text[:m.start()])})
    return found


def _header(text: str) -> dict:
    """The application a passage belongs to: the last reference, site and proposal written before it. Some
    councils label the reference ("APPLICATION NO. 22/00362/FULLS"); others (Chichester) set it on a line
    of its own above "Site" and "Proposal", with a grid reference below ("Map Ref (E) 480388 (N) 104217")."""
    refs = list(_REF.finditer(text))
    if refs:
        head, ref = text[refs[-1].start():], refs[-1].group(1)
    else:
        sites = list(_SITE.finditer(text))
        start = max(0, sites[-1].start() - 1500) if sites else max(0, len(text) - 20000)
        head = text[start:]
        bare = _BARE_REF.search(head[:3000])
        ref = bare.group(0) if bare else ""
    site, proposal, grid = _SITE.search(head), _PROPOSAL.search(head), _GRID.search(head[:5000])
    clean = lambda m: re.sub(r"\s+", " ", m.group(1)).strip(" ,") if m else ""   # noqa: E731
    where = re.sub(r"^(?:Comments|Details)\s+", "", clean(site))   # a table's next column heading, read along
    return {"ref": ref.rstrip(".,"), "site": where[:200], "proposal": clean(proposal)[:300],
            "grid": [int(grid.group(1)), int(grid.group(2))] if grid else None}


def postcode_of(text: str) -> str | None:
    m = _POSTCODE.search((text or "").upper())
    return f"{m.group(1)} {m.group(2)}" if m else None


def tidy_address(site: str) -> str:
    """"Golden Hill , Belbins, Romsey, SO51 0PE, ROMSEY EXTRA" -> "Golden Hill, Belbins, Romsey": no postcode,
    and no parish in capitals on the end (unless the whole address is in capitals)."""
    parts = [p.strip() for p in _POSTCODE.sub("", site or "").split(",")]
    parts = [p for p in parts if p]
    if len(parts) > 1 and parts[-1].isupper() and any(not p.isupper() for p in parts[:-1]):
        parts = parts[:-1]
    return ", ".join(parts)


# -- finding the place -------------------------------------------------------------------------------

def locate(session: requests.Session, site: str, council: str) -> tuple[float, float] | None:
    """Where a report's site is: the address itself where OpenStreetMap knows it (a house name finds the
    house), else the middle of its postcode. An address match far from its own postcode is somewhere else."""
    postcode = postcode_of(site)
    near = geocode.postcode_point(session, postcode) if postcode else None
    address = tidy_address(site)
    try:
        hit = geocode.search(f"{address}, {postcode}" if postcode else f"{address}, {council}", session)
    except (requests.RequestException, ValueError):
        hit = None
    if hit and (near is None or haversine_m(hit["lat"], hit["lng"], *near) < 1500):
        return hit["lat"], hit["lng"]
    return near


# -- the crawl ---------------------------------------------------------------------------------------

class Memory:
    """Which papers have been read, when each council was last asked and what its planning committees are,
    kept in the data folder so a paper is read once, ever."""

    def __init__(self, path: Path | None):
        self.path = path
        self.data = {"read": {}, "asked": {}, "committees": {}}
        self.lock = threading.Lock()      # several councils are asked at once
        if path and path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        if self.data.get("reader") != READER:     # read with older phrases or layouts: read them all again
            self.data.update({"read": {}, "asked": {}, "reader": READER})

    def read(self, url: str) -> bool:
        return url in self.data["read"]

    def mark_read(self, url: str) -> None:
        with self.lock:
            self.data["read"][url] = date.today().isoformat()

    def asked(self, base: str) -> date | None:
        when = self.data["asked"].get(base)
        return date.fromisoformat(when) if when else None

    def mark_asked(self, base: str, when: date) -> None:
        with self.lock:
            self.data["asked"][base] = when.isoformat()

    def committees(self, base: str, today: date) -> list[str] | None:
        """The council's planning committees, if looked up lately ([] meaning its search has no list)."""
        kept = self.data["committees"].get(base)
        if not kept or date.fromisoformat(kept["at"]) < today - timedelta(days=COMMITTEES_EVERY_DAYS):
            return None
        return kept["ids"]

    def mark_committees(self, base: str, ids: list[str], today: date) -> None:
        with self.lock:
            self.data["committees"][base] = {"ids": ids, "at": today.isoformat()}

    def save(self) -> None:
        if not self.path:
            return
        with self.lock:
            text = json.dumps(self.data, separators=(",", ":"))
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.path)


def crawl(session: requests.Session, sites, progress: Progress, cancel=None, memory: Memory | None = None,
          gap_s: float = 1.0, today: date | None = None, at_once: int = COUNCILS_AT_ONCE) -> Iterator[dict]:
    """Each site a council's planning papers say stands empty, unfinished or derelict. Councils are separate
    websites, so several are asked at once; each one still gets a request a second, and each paper is read
    once."""
    memory = memory or Memory(None)
    today = today or date.today()
    halt = threading.Event()                  # stops every council's worker when the crawl stops
    out: queue.Queue = queue.Queue()

    def ask(council: str, base: str) -> None:
        try:
            for row in _ask_council(session, council, base, memory, halt, gap_s, today):
                out.put(("row", row))
        except Cancelled:
            pass
        except Exception as exc:              # a bug, not a council being down: stop and say so
            out.put(("error", exc))
        finally:
            memory.save()
            out.put(("done", council))

    pool = ThreadPoolExecutor(max_workers=max(1, at_once), thread_name_prefix="committees")
    for council, base in sites:
        pool.submit(ask, council, base)
    done = 0
    progress(f"asking {min(at_once, len(sites))} councils at a time", done, len(sites))
    try:
        while done < len(sites):
            _stop(cancel)
            try:
                kind, value = out.get(timeout=0.5)
            except queue.Empty:
                continue
            if kind == "row":
                yield value
            elif kind == "error":
                raise value
            else:
                done += 1
                progress(f"asking {min(at_once, len(sites) - done) or 1} councils at a time", done, len(sites))
    finally:
        halt.set()
        pool.shutdown(wait=False, cancel_futures=True)


def _ask_council(session: requests.Session, council: str, base: str, memory: Memory, halt: threading.Event,
                 gap_s: float, today: date) -> Iterator[dict]:
    """One council: its planning papers since it was last asked, each read once."""
    headers = {"User-Agent": USER_AGENT}
    last = memory.asked(base)
    since = max(FIRST_SINCE, last - timedelta(days=OVERLAP_DAYS)) if last else FIRST_SINCE
    until = today + timedelta(days=120)      # agendas go up a week or two before the meeting
    try:
        # Ten years of papers: one search per planning committee, so theirs aren't lost among Cabinet's.
        # A few weeks' papers: one search of everything is a page or so. Either way, other committees'
        # papers are set aside below.
        committees = [None] if last else memory.committees(base, today)
        if committees is None:          # which committees decide planning applications
            _stop(halt)
            r = session.get(f"{base}/ieDocSearch.aspx?ADV=1&bcr=1", headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            committees = planning_committees(r.text) or []
            memory.mark_committees(base, committees, today)
            _pause(gap_s, halt)
        hits = []
        for committee in committees or [None]:
            for page in range(1, MAX_PAGES + 1):
                _stop(halt)
                r = session.get(search_url(base, since, until, page, committee), headers=headers, timeout=TIMEOUT)
                r.raise_for_status()
                found, more = parse_results(r.text, base, page)
                hits += found
                _pause(gap_s, halt)
                if not more:
                    break
        for hit in hits:
            if memory.read(hit["url"]) or not is_planning(hit["committee"]):
                continue
            _stop(halt)
            yield from _read_report(session, council, hit, headers)
            memory.mark_read(hit["url"])
            _pause(gap_s, halt)
        memory.mark_asked(base, today)
    except requests.RequestException:
        pass        # one council's site being down doesn't stop the rest; it's asked again next time


def _pause(seconds: float, cancel: threading.Event | None) -> None:
    if seconds and (cancel or threading.Event()).wait(seconds):
        raise Cancelled()


def _read_report(session: requests.Session, council: str, hit: dict, headers: dict) -> Iterator[dict]:
    r = session.get(hit["url"], headers=headers, timeout=TIMEOUT, stream=True)
    if r.status_code != 200 or "pdf" not in r.headers.get("Content-Type", "").lower():
        return
    data = bytearray()
    for chunk in r.iter_content(1 << 16):
        data += chunk
        if len(data) > MAX_REPORT_BYTES:
            return
    try:
        pages = read_pdf(bytes(data))
    except Exception:       # a damaged or unusual PDF: skip it, there are plenty more
        return
    seen = set()
    for s in find_statements(pages):
        ref = s["ref"] or hit["item"] or hit["url"]
        if ref in seen or not (s["site"] or s["grid"]):
            continue
        seen.add(ref)
        # The report's own grid reference where it gives one (to the metre), else the address.
        point = bng_to_wgs84(*s["grid"]) if s["grid"] else locate(session, s["site"], council)
        if not point:
            continue
        yield {**s, "ref": ref, "council": council, "committee": hit["committee"], "meeting": hit["meeting"],
               "url": f"{hit['url']}#page={s['page']}", "lat": point[0], "lng": point[1]}


# Councils whose committee papers are on ModernGov and whose search answers: (name, address). Found in
# October 2026 by asking each of PlanIt's 455 planning authorities' likely ModernGov addresses for the feed
# every ModernGov site serves, then making one search of each. Another 33 councils' sites turn searches
# away (403) and aren't asked.
MODERNGOV_SITES = (
    ("Aberdeen", "https://aberdeen.moderngov.co.uk"),
    ("Anglesey", "https://democracy.anglesey.gov.uk"),
    ("Arun", "https://democracy.arun.gov.uk"),
    ("Ashford", "https://ashford.moderngov.co.uk"),
    ("Barnet", "https://barnet.moderngov.co.uk"),
    ("Bassetlaw", "https://bassetlaw.moderngov.co.uk"),
    ("Bath", "https://democracy.bathnes.gov.uk"),
    ("Bexley", "https://democracy.bexley.gov.uk"),
    ("Blackburn", "https://democracy.blackburn.gov.uk"),
    ("Blackpool", "https://democracy.blackpool.gov.uk"),
    ("Bolsover", "https://committees.bolsover.gov.uk"),
    ("Bolton", "https://bolton.moderngov.co.uk"),
    ("Bracknell", "https://democratic.bracknell-forest.gov.uk"),
    ("Bradford", "https://bradford.moderngov.co.uk"),
    ("Breckland", "https://democracy.breckland.gov.uk"),
    ("Brent", "https://democracy.brent.gov.uk"),
    ("Brentwood", "https://brentwood.moderngov.co.uk"),
    ("Bridgend", "https://democratic.bridgend.gov.uk"),
    ("Brighton", "https://democracy.brighton-hove.gov.uk"),
    ("Bristol", "https://democracy.bristol.gov.uk"),
    ("Bromsgrove", "https://moderngovwebpublic.bromsgrove.gov.uk"),
    ("Broxbourne", "https://broxbourne.moderngov.co.uk"),
    ("Broxtowe", "https://democracy.broxtowe.gov.uk"),
    ("Buckinghamshire", "https://buckinghamshire.moderngov.co.uk"),
    ("Burnley", "https://burnley.moderngov.co.uk"),
    ("Caerphilly", "https://democracy.caerphilly.gov.uk"),
    ("Calderdale", "https://calderdale.moderngov.co.uk"),
    ("Cambridge", "https://democracy.cambridge.gov.uk"),
    ("Camden", "https://camden.moderngov.co.uk"),
    ("Canterbury", "https://democracy.canterbury.gov.uk"),
    ("Central Bedfordshire", "https://centralbeds.moderngov.co.uk"),
    ("Charnwood", "https://charnwood.moderngov.co.uk"),
    ("Cheltenham", "https://democracy.cheltenham.gov.uk"),
    ("Cheshire East", "https://moderngov.cheshireeast.gov.uk"),
    ("Chesterfield", "https://chesterfield.moderngov.co.uk"),
    ("Chichester", "https://chichester.moderngov.co.uk"),
    ("Chorley", "https://democracy.chorley.gov.uk"),
    ("Cornwall", "https://democracy.cornwall.gov.uk"),
    ("Crawley", "https://democracy.crawley.gov.uk"),
    ("Croydon", "https://democracy.croydon.gov.uk"),
    ("Cumberland", "https://cumberland.moderngov.co.uk"),
    ("Dacorum", "https://democracy.dacorum.gov.uk"),
    ("Darlington", "https://democracy.darlington.gov.uk"),
    ("Dartford", "https://dartford.moderngov.co.uk"),
    ("Denbighshire", "https://moderngov.denbighshire.gov.uk"),
    ("Derbyshire", "https://democracy.derbyshire.gov.uk"),
    ("Derbyshire Dales", "https://democracy.derbyshiredales.gov.uk"),
    ("Derry and Strabane", "https://meetings.derrycityandstrabanedistrict.com"),
    ("Doncaster", "https://doncaster.moderngov.co.uk"),
    ("Dover", "https://moderngov.dover.gov.uk"),
    ("Durham", "https://democracy.durham.gov.uk"),
    ("Ealing", "https://ealing.moderngov.co.uk"),
    ("Dorset", "https://moderngov.dorsetcouncil.gov.uk"),
    ("East Dunbartonshire", "https://eastdunbarton.moderngov.co.uk"),
    ("East Hampshire", "https://easthants.moderngov.co.uk"),
    ("East Hertfordshire", "https://democracy.eastherts.gov.uk"),
    ("East Sussex", "https://democracy.eastsussex.gov.uk"),
    ("Eastbourne & Lewes", "https://democracy.lewes-eastbourne.gov.uk"),
    ("Eastleigh", "https://eastleigh.moderngov.co.uk"),
    ("Epsom and Ewell", "https://democracy.epsom-ewell.gov.uk"),
    ("Fareham", "https://moderngov.fareham.gov.uk"),
    ("Forest of Dean", "https://meetings.fdean.gov.uk"),
    ("Gateshead", "https://democracy.gateshead.gov.uk"),
    ("Gloucester", "https://democracy.gloucester.gov.uk"),
    ("Greenwich", "https://greenwich.moderngov.co.uk"),
    ("Guildford", "https://democracy.guildford.gov.uk"),
    ("Hackney", "https://hackney.moderngov.co.uk"),
    ("Halton", "https://moderngov.halton.gov.uk"),
    ("Hampshire", "https://democracy.hants.gov.uk"),
    ("Harlow", "https://harlow.moderngov.co.uk"),
    ("Harrow", "https://moderngov.harrow.gov.uk"),
    ("Hart", "https://hart.moderngov.co.uk"),
    ("Hastings", "https://hastings.moderngov.co.uk"),
    ("Havant", "https://havant.moderngov.co.uk"),
    ("Havering", "https://democracy.havering.gov.uk"),
    ("Hertfordshire", "https://democracy.hertfordshire.gov.uk"),
    ("Hertsmere", "https://hertsmere.moderngov.co.uk"),
    ("High Peak", "https://democracy.highpeak.gov.uk"),
    ("Hinckley and Bosworth", "https://moderngov.hinckley-bosworth.gov.uk"),
    ("Horsham", "https://horsham.moderngov.co.uk"),
    ("Ipswich", "https://democracy.ipswich.gov.uk"),
    ("Isle of Wight", "https://iow.moderngov.co.uk"),
    ("Kent", "https://democracy.kent.gov.uk"),
    ("Kings Lynn", "https://democracy.west-norfolk.gov.uk"),
    ("Kirklees", "https://democracy.kirklees.gov.uk"),
    ("Knowsley", "https://councillors.knowsley.gov.uk"),
    ("Lambeth", "https://moderngov.lambeth.gov.uk"),
    ("Leicester", "https://leicester.moderngov.co.uk"),
    ("Leicestershire", "https://democracy.leics.gov.uk"),
    ("Lewisham", "https://councilmeetings.lewisham.gov.uk"),
    ("Lichfield", "https://democracy.lichfielddc.gov.uk"),
    ("Lincoln", "https://democratic.lincoln.gov.uk"),
    ("Lincolnshire", "https://lincolnshire.moderngov.co.uk"),
    ("Liverpool", "https://councillors.liverpool.gov.uk"),
    ("Maldon", "https://democracy.maldon.gov.uk"),
    ("Manchester", "https://democracy.manchester.gov.uk"),
    ("Mansfield", "https://mansfield.moderngov.co.uk"),
    ("Medway", "https://democracy.medway.gov.uk"),
    ("Merton", "https://democracy.merton.gov.uk"),
    ("Mid Devon", "https://democracy.middevon.gov.uk"),
    ("Mid Sussex", "https://midsussex.moderngov.co.uk"),
    ("Milton Keynes", "https://milton-keynes.moderngov.co.uk"),
    ("New Forest (District)", "https://democracy.newforest.gov.uk"),
    ("Newark and Sherwood", "https://democracy.newark-sherwooddc.gov.uk"),
    ("Newcastle under Lyme", "https://moderngov.newcastle-staffs.gov.uk"),
    ("Newcastle upon Tyne", "https://democracy.newcastle.gov.uk"),
    ("North Devon", "https://democracy.northdevon.gov.uk"),
    ("North East Derbyshire", "https://democracy.ne-derbyshire.gov.uk"),
    ("North Hertfordshire", "https://democracy.north-herts.gov.uk"),
    ("North Kesteven", "https://democracy.n-kesteven.gov.uk"),
    ("North Norfolk", "https://modgov.north-norfolk.gov.uk"),
    ("North Northamptonshire", "https://northnorthants.moderngov.co.uk"),
    ("North Somerset", "https://n-somerset.moderngov.co.uk"),
    ("North Tyneside", "https://democracy.northtyneside.gov.uk"),
    ("North Warwickshire", "https://northwarwickshire.moderngov.co.uk"),
    ("North Yorkshire", "https://edemocracy.northyorks.gov.uk"),
    ("Northumberland (County)", "https://northumberland.moderngov.co.uk"),
    ("Nottingham", "https://committee.nottinghamcity.gov.uk"),
    ("Oadby and Wigston", "https://moderngov.oadby-wigston.gov.uk"),
    ("Oldham", "https://committees.oldham.gov.uk"),
    ("Peterborough", "https://democracy.peterborough.gov.uk"),
    ("Portsmouth", "https://democracy.portsmouth.gov.uk"),
    ("Preston", "https://preston.moderngov.co.uk"),
    ("Reading", "https://democracy.reading.gov.uk"),
    ("Redditch", "https://moderngovwebpublic.redditchbc.gov.uk"),
    ("Reigate", "https://reigate-banstead.moderngov.co.uk"),
    ("Ribble Valley", "https://democracy.ribblevalley.gov.uk"),
    ("Rochdale", "https://democracy.rochdale.gov.uk"),
    ("Rochford", "https://rochford.moderngov.co.uk"),
    ("Rother", "https://rother.moderngov.co.uk"),
    ("Runnymede", "https://democracy.runnymede.gov.uk"),
    ("Rushcliffe", "https://democracy.rushcliffe.gov.uk"),
    ("Salford", "https://salford.moderngov.co.uk"),
    ("Sandwell", "https://sandwell.moderngov.co.uk"),
    ("Scilly Isles", "https://committees.scilly.gov.uk"),
    ("Scottish Borders", "https://scottishborders.moderngov.co.uk"),
    ("Sevenoaks", "https://sevenoaks.moderngov.co.uk"),
    ("Sheffield", "https://democracy.sheffield.gov.uk"),
    ("Shepway", "https://folkestone-hythe.moderngov.co.uk"),
    ("Shropshire", "https://modgov.shropshire.gov.uk"),
    ("Slough", "https://democracy.slough.gov.uk"),
    ("Solihull", "https://democracy.solihull.gov.uk"),
    ("Somerset", "https://somerset.moderngov.co.uk"),        # one council since 2023, its four districts gone
    ("South Cambridgeshire", "https://scambs.moderngov.co.uk"),
    ("South Norfolk Broadland", "https://democracy.southnorfolkandbroadland.gov.uk"),
    ("South Oxfordshire", "https://democratic.southoxon.gov.uk"),
    ("South Ribble", "https://southribble.moderngov.co.uk"),
    ("South West Devon", "https://democracy.swdevon.gov.uk"),
    ("Southend", "https://democracy.southend.gov.uk"),
    ("Southwark", "https://moderngov.southwark.gov.uk"),
    ("Spelthorne", "https://democracy.spelthorne.gov.uk"),
    ("St Albans", "https://stalbans.moderngov.co.uk"),
    ("St Helens", "https://sthelens.moderngov.co.uk"),
    ("Staffordshire", "https://staffordshire.moderngov.co.uk"),
    ("Staffordshire Moorlands", "https://democracy.staffsmoorlands.gov.uk"),
    ("Stevenage", "https://democracy.stevenage.gov.uk"),
    ("Stockport", "https://democracy.stockport.gov.uk"),
    ("Stockton-on-Tees", "https://moderngov.stockton.gov.uk"),
    ("Stoke on Trent", "https://moderngov.stoke.gov.uk"),
    ("Stratford on Avon", "https://democracy.stratford.gov.uk"),
    ("Stroud", "https://stroud.moderngov.co.uk"),
    ("Surrey Heath", "https://surreyheath.moderngov.co.uk"),
    ("Tameside", "https://tameside.moderngov.co.uk"),
    ("Tamworth", "https://tamworth.moderngov.co.uk"),
    ("Tandridge", "https://tandridge.moderngov.co.uk"),
    ("Telford", "https://democracy.telford.gov.uk"),
    ("Test Valley", "https://democracy.testvalley.gov.uk"),
    ("Thanet", "https://democracy.thanet.gov.uk"),
    ("Thurrock", "https://democracy.thurrock.gov.uk"),
    ("Tonbridge", "https://democracy.tmbc.gov.uk"),
    ("Torfaen", "https://torfaen.moderngov.co.uk"),
    ("Trafford", "https://democratic.trafford.gov.uk"),
    ("Uttlesford", "https://uttlesford.moderngov.co.uk"),
    ("Vale of White Horse", "https://democratic.whitehorsedc.gov.uk"),
    ("Wakefield", "https://wakefield.moderngov.co.uk"),
    ("Waltham Forest", "https://democracy.walthamforest.gov.uk"),
    ("Wandsworth", "https://democracy.wandsworth.gov.uk"),
    ("Watford", "https://watford.moderngov.co.uk"),
    ("Waverley", "https://modgov.waverley.gov.uk"),
    ("Wealden", "https://meetings.wealden.gov.uk"),
    ("Welwyn Hatfield", "https://democracy.welhat.gov.uk"),
    ("West Lindsey", "https://democracy.west-lindsey.gov.uk"),
    ("West Northamptonshire", "https://westnorthants.moderngov.co.uk"),
    ("West Suffolk", "https://democracy.westsuffolk.gov.uk"),
    ("West Sussex", "https://westsussex.moderngov.co.uk"),
    ("Westminster", "https://committees.westminster.gov.uk"),
    ("Westmorland and Furness", "https://westmorlandandfurness.moderngov.co.uk"),
    ("Wigan", "https://democracy.wigan.gov.uk"),
    ("Windsor", "https://rbwm.moderngov.co.uk"),
    ("Wirral", "https://democracy.wirral.gov.uk"),
    ("Woking", "https://moderngov.woking.gov.uk"),
    ("Wokingham", "https://wokingham.moderngov.co.uk"),
    ("Wolverhampton", "https://wolverhampton.moderngov.co.uk"),
    ("Worcestershire", "https://worcestershire.moderngov.co.uk"),
    ("Wrexham", "https://moderngov.wrexham.gov.uk"),
    ("Wyre", "https://wyre.moderngov.co.uk"),
    ("York", "https://democracy.york.gov.uk"),
)
