"""Closed schools, Scotland's derelict land, demolition applications (PlanIt), OpenStreetMap's own
demolitions, and the grid references the registers give their positions in."""
import io
import os
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from bandobuddy import opendata, osm
from bandobuddy.geo import bng_to_wgs84, haversine_m
from bandobuddy.sites import build_sites
from bandobuddy.store import Store
from bandobuddy.updater import Updater


class Resp:
    def __init__(self, status=200, json_data=None, content=b"", text="", headers=None):
        self.status_code, self._json, self.content, self.text = status, json_data, content, text
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, size):
        for i in range(0, len(self.content), 7):   # awkward chunks, splitting lines and characters
            yield self.content[i:i + 7]


class Service:
    def __init__(self, answer):
        self.answer = answer          # (url, params) -> Resp
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None, stream=False):
        self.calls.append((url, dict(params or {})))
        return self.answer(url, params or {})


def nothing(*_args):
    pass


class GridTests(unittest.TestCase):
    def test_grid_references_become_gps_positions(self):
        # Ordnance Survey's own positions for postcode centres (via postcodes.io), Cornwall to Shetland.
        for e, n, lat, lng in [(414313, 130075, 51.069824, -1.797089), (325597, 673676, 55.950328, -3.193018),
                               (447657, 1141488, 60.155111, -1.143391), (147604, 30040, 50.116651, -5.532026),
                               (624073, 308352, 52.626674, 1.309363)]:
            got = bng_to_wgs84(e, n)
            self.assertLess(haversine_m(*got, lat, lng), 6, (e, n))


SCHOOLS_CSV = (
    '"URN","EstablishmentName","TypeOfEstablishment (name)","EstablishmentStatus (name)",'
    '"ReasonEstablishmentClosed (name)","CloseDate","PhaseOfEducation (name)","Town","Easting","Northing"\r\n'
    # Shut for good, two years ago: a good lead. (Non-ASCII in the old Windows encoding, too.)
    '"1","St Ædan\'s Church of England School","Voluntary aided school","Closed","Closure","31-08-{recent}","Primary","Salisbury","414313","130075"\r\n'
    # Its infant school on the same site closed earlier: the same place, said once.
    '"2","St Aedan\'s Infants","Community school","Closed","Closure","31-08-2010","Primary","Salisbury","414330","130090"\r\n'
    # Converted to an academy: the school carries on, under a new number.
    '"3","Hill Academy","Community school","Closed","Academy Converter","31-08-2015","Secondary","Bath","374000","165000"\r\n'
    # Merged into the school that's still open beside it: still a school.
    '"4","Old Juniors","Community school","Closed","Result of Amalgamation/Merger","31-08-2019","Primary","Leeds","430000","433000"\r\n'
    '"5","New Primary","Academy converter","Open","","","Primary","Leeds","430040","433030"\r\n'
    # Closed long ago, and nowhere near an open school: kept, but a weak lead.
    '"6","Moor School","Community school","Closed","Closure","31-07-1995","Primary","Moor","390000","500000"\r\n'
    # In Wales, and with no position: not ours.
    '"7","Ysgol","Welsh establishment","Closed","Closure","31-08-2024","Primary","Bala","290000","336000"\r\n'
    '"8","Nowhere","Community school","Closed","Closure","31-08-2024","Primary","","",""\r\n'
)


class SchoolsTests(unittest.TestCase):
    def fetch(self):
        recent = date.today().year - 2
        body = SCHOOLS_CSV.replace("{recent}", str(recent)).encode("cp1252")
        today = date.today().strftime("%Y%m%d")
        # Today's file isn't out yet: yesterday's is used.
        service = Service(lambda url, p: Resp(404) if today in url else Resp(200, content=body))
        rows = list(opendata.SchoolsRegister()(service, nothing))
        return rows, service

    def test_closed_schools_that_are_still_closed(self):
        rows, service = self.fetch()
        self.assertEqual(len(service.calls), 2)
        self.assertEqual(sorted(r["URN"] for r in rows), ["1", "3", "6", "7"])    # judged next
        self.assertTrue(rows[0]["EstablishmentName"].startswith("St Ædan"))
        lat, lng = next((r["lat"], r["lng"]) for r in rows if r["URN"] == "1")
        self.assertLess(haversine_m(lat, lng, 51.069824, -1.797089), 6)

        items = {r["URN"]: opendata._school(r) for r in rows}
        self.assertIsNone(items["3"])            # an academy conversion
        self.assertIsNone(items["7"])            # Wales
        self.assertEqual((items["1"]["weight"], items["1"]["kind"]), (22, "school"))
        self.assertIn(f"closed in {date.today().year - 2}", items["1"]["evidence"])
        self.assertEqual(items["6"]["weight"], 8)
        self.assertIn("/Details/1", items["1"]["url"])

    def test_a_church_school_is_a_school(self):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        rows, _ = self.fetch()
        items = []
        for r in rows:
            item = opendata._school(r)
            if item:
                items.append({**item, "dataset": "schools", "lat": r["lat"], "lng": r["lng"]})
        store.upsert_od(items, "2026-01-01T00:00:00+00:00")
        build_sites(store)
        site = next(s for s in store.full_sites(min_score=0) if s["name"].startswith("St Ædan"))
        self.assertEqual((site["category"], site["strength"]), ("institutional", "good"))
        self.assertEqual(site["condition"], f"Closed {date.today().year - 2}")


def ods(rows):
    """A minimal OpenDocument spreadsheet with a Site_Register sheet."""
    def cell(v):
        return f'<table:table-cell office:value-type="string"><text:p>{v}</text:p></table:table-cell>'
    def cells(r):   # runs of blanks are written once, "repeated", as spreadsheets do
        out, i = "", 0
        while i < len(r):
            j = i
            while j < len(r) and r[j] == "":
                j += 1
            if j > i:
                out += f'<table:table-cell table:number-columns-repeated="{j - i}"/>'
                i = j
            else:
                out += cell(r[i])
                i += 1
        return out
    body = "".join("<table:table-row>" + cells(r)
                   + '<table:table-cell table:number-columns-repeated="16000"/></table:table-row>' for r in rows)
    xml = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content '
           'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
           'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
           'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:spreadsheet>'
           '<table:table table:name="Notes"><table:table-row>' + cell("Read me") + '</table:table-row></table:table>'
           f'<table:table table:name="Site_Register">{body}</table:table></office:spreadsheet></office:body>'
           '</office:document-content>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("content.xml", xml)
    return out.getvalue()


VDL_HEADER = ["Planning Authority", "Site Code", "Site Name (If Supplied)", "Address (If Supplied)", "East", "North",
              "Site Size (Hectares)", "Site Type", "Owner 1", "Period when site became Vacant or Derelict",
              "Previous Use of Site"]


class ScotlandTests(unittest.TestCase):
    def test_the_register_is_found_read_and_judged(self):
        sheet = ods([["Scottish Vacant and Derelict Land Survey"], [], VDL_HEADER,
                     ["Fife", "F-1", "FORMER RAF DEPOT", "", "325597", "673676", "2.1", "Derelict", "Public", "1996-2000", "Defence"],
                     # No name, and a blank owner: two blank cells mid-row mustn't shift the columns.
                     ["Fife", "F-2", "", "12 HIGH STREET, KIRKCALDY", "327000", "692000", "", "Vacant Land and Buildings", "", "2019", "Retailing"],
                     ["Fife", "F-3", "MILTON ROAD", "", "328000", "693000", "0.4", "Vacant Land", "", "", "Agriculture"],
                     ["Fife", "", "No code", "", "1", "1", "", "Derelict", "", "", ""]])
        page = '<a href="/binaries/content/SVDLS_2031_Register.ods">Site register</a>'

        def answer(url, params):
            return Resp(text=page) if url.endswith("site-register/") else Resp(content=sheet)
        service = Service(answer)
        rows = list(opendata.ScotlandDerelictLand()(service, nothing))
        self.assertEqual(service.calls[1][0], "https://www.gov.scot/binaries/content/SVDLS_2031_Register.ods")
        self.assertEqual([r["Site Code"] for r in rows], ["F-1", "F-2", "F-3"])
        self.assertLess(haversine_m(rows[0]["lat"], rows[0]["lng"], 55.950328, -3.193018), 6)

        raf, shop, plot = (opendata._vdl(r) for r in rows)
        self.assertEqual((raf["name"], raf["kind"], raf["weight"], raf["ref"]),
                         ("Former RAF Depot", "military site", 24, "Fife:F-1"))
        self.assertEqual(raf["evidence"], "Scotland's land survey lists it as derelict since 1996-2000, previously defence")
        self.assertEqual((shop["name"], shop["weight"]), ("12 High Street", 22))
        self.assertEqual(plot["weight"], 6)
        self.assertEqual(plot["evidence"], "Scotland's land survey lists it as cleared vacant land, previously agriculture")

        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        store.upsert_od([{**i, "dataset": "scotland_vdl", "lat": r["lat"], "lng": r["lng"]}
                         for i, r in zip((raf, shop, plot), rows)], "2026-01-01T00:00:00+00:00")
        build_sites(store)
        conditions = {s["name"]: (s["condition"], s["strength"]) for s in store.full_sites(min_score=0)}
        # "Former" in its name counts too, as it does for any place.
        self.assertEqual(conditions, {"Former RAF Depot": ("Abandoned", "strong"), "12 High Street": ("Disused", "good"),
                                      "Milton Road": ("Vacant land", "weak")})


class PlanItTests(unittest.TestCase):
    def record(self, n, description="Demolition of derelict former mill building", **extra):
        return {"name": f"Area/{n}", "description": description, "address": f"{n} Mill Lane, Town, AB1 2CD",
                "postcode": "AB1 2CD", "app_state": "Undecided", "start_date": "2026-09-01", "decided_date": None,
                "location_x": -1.8, "location_y": 51.07, "link": f"https://www.planit.org.uk/planapplic/Area/{n}/",
                "url": f"https://council/{n}", **extra}

    def test_a_page_a_minute_and_slowing_down_when_asked(self):
        pages = [Resp(200, {"records": [self.record(1), self.record(2, location_x=None)], "total": 3}),
                 Resp(429, headers={"Retry-After": "90"}),
                 Resp(200, {"records": [self.record(3)], "total": 3}),
                 Resp(200, {"records": [self.record(4, app_state="Permitted")], "total": 1})]
        service = Service(lambda url, params: pages.pop(0))
        waits = []
        fetch = opendata.PlanIt(batch=2)
        with mock.patch.object(opendata.PlanIt, "_wait", lambda self, cancel, s: waits.append(s)):
            rows = list(fetch(service, nothing))
        self.assertEqual([r["name"] for r in rows], ["Area/1", "Area/3", "Area/4"])   # one had no position
        self.assertEqual(waits, [61, 90, 61])                     # a minute between requests; longer when asked
        asked = [c[1] for c in service.calls]
        # New applications in the last fortnight, then decisions in it, never "everything that changed".
        self.assertEqual([(a.get("recent"), a.get("decided"), a["page"]) for a in asked],
                         [(14, None, 1), (14, None, 2), (14, None, 2), (None, 14, 1)])
        self.assertNotIn("different", asked[0])
        self.assertIn("demolition derelict or demolish derelict", asked[0]["search"])
        self.assertIn('demolish "fire damaged"', asked[0]["search"])

    def test_what_counts_as_a_lead(self):
        judge = opendata._planit
        mill = judge(self.record(1))
        self.assertEqual((mill["kind"], mill["weight"], mill["name"]), ("industrial building", 23, "1 Mill Lane, Town"))
        self.assertIn("no decision yet", mill["evidence"])
        self.assertEqual(mill["url"], "https://council/1")
        pub = judge(self.record(2, "Demolition of former public house and erection of six flats"))
        self.assertEqual((pub["kind"], pub["weight"]), ("pub", 12))
        for small in ("Demolition of existing single storey rear extension",
                      "Demolition of existing dwelling and erection of replacement dwelling",
                      "Demolition of existing garage and erection of two storey side extension",
                      "Change of use of derelict barn to dwelling"):     # nothing knocked down
            self.assertIsNone(judge(self.record(3, small)), small)
        recent = (date.today() - timedelta(days=30)).isoformat()
        approved = judge(self.record(4, app_state="Permitted", decided_date=recent))
        self.assertIn(f"demolition approved on {recent}", approved["evidence"])
        old = judge(self.record(5, app_state="Conditions", decided_date="2023-01-10"))
        self.assertEqual(old["weight"], 5)
        self.assertIn("may well be gone", old["evidence"])
        # A follow-up to an earlier application: that demolition's approved and the work's getting going.
        follow = judge(self.record(6, "DISCHARGE OF CONDITION 8 FROM PLANNING PERMISSION 21/01856/O - Outline "
                                      "Application: Demolition of existing disused bus depot and construction of homes"))
        self.assertEqual(follow["weight"], 5)
        self.assertIn("demolition approved earlier", follow["evidence"])
        for nothing_decided in ("Pre-application advice for demolition of the former Wesleyan school",
                                "Certificate of lawfulness for proposed demolition of redundant farm buildings"):
            self.assertIsNone(judge(self.record(7, nothing_decided)), nothing_decided)

    def test_off_unless_switched_on_and_never_forgets_older_ones(self):
        self.assertFalse(opendata.DATASETS["planit"].enabled())
        with mock.patch.dict(os.environ, {"BANDOBUDDY_PLANIT": "1"}):
            self.assertTrue(opendata.DATASETS["planit"].enabled())
        tmp = Path(tempfile.mkdtemp())
        store = Store(tmp / "t.db")
        batches = [[self.record(1)], [self.record(2, location_x=-1.81)]]
        dataset = opendata.Dataset(**{**opendata.DATASETS["planit"].__dict__,
                                      "fetch": lambda session, progress, cancel=None: iter(
                                          [{**r, "lat": r["location_y"], "lng": r["location_x"]}
                                           for r in batches.pop(0)])})
        with mock.patch.dict(opendata.DATASETS, {"planit": dataset}):
            up = Updater(store, tmp, session_factory=lambda: None, log=lambda m: None, sources=("planit",))
            up.run("planit")
            up.run("planit")       # this week's sweep doesn't mention last week's application
        self.assertEqual(sorted(r["ref"] for r in store.active_od()), ["Area/1", "Area/2"])

    def test_stopping_part_way_keeps_what_arrived(self):
        tmp = Path(tempfile.mkdtemp())
        store = Store(tmp / "t.db")

        def fetch(session, progress, cancel=None):
            yield {**self.record(1), "lat": 51.07, "lng": -1.8}
            raise opendata.Cancelled()          # stopped while waiting a minute for the next page
        dataset = opendata.Dataset(**{**opendata.DATASETS["planit"].__dict__, "fetch": fetch})
        with mock.patch.dict(opendata.DATASETS, {"planit": dataset}):
            Updater(store, tmp, session_factory=lambda: None, log=lambda m: None, sources=("planit",)).run("planit")
        self.assertEqual([r["ref"] for r in store.active_od()], ["Area/1"])


class ConditionTests(unittest.TestCase):
    def test_conditions_read_from_the_evidence(self):
        from bandobuddy.scoring import _condition_from
        self.assertEqual(_condition_from("Canmore records a ROC post here"), "Old military")
        self.assertEqual(_condition_from('Planning application (demolition refused): "Demolition of redundant buildings"'),
                         "Disused")


class NamedDisusedTests(unittest.TestCase):
    def test_the_dive_test_facility_at_alverstoke(self):
        # Mapped only as a fenced military area whose name says it's disused (OSM way 24597207).
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        store.upsert_osm([{"osm_id": "way/24597207", "lat": 50.77922, "lng": -1.14228, "extent_m": 120,
                           "tags": {"landuse": "military", "barrier": "fence", "name": "Dive Test Facility (Disused)"}}],
                         "2026-10-05T00:00:00+00:00")
        build_sites(store)
        site = store.full_sites(min_score=0)[0]
        self.assertEqual((site["name"], site["condition"], site["category"], site["strength"]),
                         ("Dive Test Facility", "Disused", "military", "strong"))   # the state shown once, as the condition
        self.assertIn("Its name says 'disused'", site["reasons"])


class DemolishedTests(unittest.TestCase):
    def test_osm_demolitions_and_building_sites(self):
        self.assertEqual(osm.gone_as({"demolished:building": "yes"}), "Demolished")
        self.assertEqual(osm.gone_as({"razed:railway": "station"}), "Demolished")
        self.assertEqual(osm.gone_as({"landuse": "construction"}), "Building site")
        self.assertIsNone(osm.gone_as({"demolished:building": "no", "building": "yes"}))
        self.assertTrue(osm.is_candidate([type("T", (), {"k": "demolished:building", "v": "yes"})()]))

        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        now = "2026-01-01T00:00:00+00:00"
        store.upsert_osm([
            # A disused mill, and the same spot mapped as demolished since.
            {"osm_id": "way/1", "lat": 51.10, "lng": -1.80, "extent_m": 30,
             "tags": {"disused:man_made": "works", "name": "Town Mill"}},
            {"osm_id": "way/2", "lat": 51.1001, "lng": -1.8001, "extent_m": 40,
             "tags": {"demolished:building": "industrial", "name": "Town Mill"}},
            # A closed hospital inside a big building site.
            {"osm_id": "node/3", "lat": 51.20, "lng": -1.90, "tags": {"abandoned:amenity": "hospital",
                                                                       "name": "County Asylum"}},
            {"osm_id": "way/4", "lat": 51.201, "lng": -1.901, "extent_m": 300, "tags": {"landuse": "construction"}},
            # A pub 200 m from a demolished point: a different building.
            {"osm_id": "node/5", "lat": 51.30, "lng": -2.00, "tags": {"disused:amenity": "pub", "name": "The Bell"}},
            {"osm_id": "node/6", "lat": 51.3018, "lng": -2.00, "tags": {"demolished:building": "yes"}},
        ], now)
        build_sites(store)
        sites = {s["name"]: s for s in store.full_sites(min_score=0)}
        self.assertEqual(len(sites), 3)              # the markers aren't places themselves
        mill, asylum, pub = sites["Town Mill"], sites["County Asylum"], sites["The Bell"]
        self.assertEqual((mill["condition"], mill["strength"]), ("Demolished", "weak"))
        self.assertEqual(mill["reasons"][0], "OpenStreetMap maps it as demolished")
        self.assertEqual((asylum["condition"], asylum["strength"]), ("Building site", "weak"))
        self.assertEqual(asylum["reasons"][0], "OpenStreetMap maps a building site here now")
        self.assertNotEqual(pub["condition"], "Demolished")


if __name__ == "__main__":
    unittest.main()
