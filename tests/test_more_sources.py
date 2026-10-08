"""Closed schools, Scotland's derelict land, demolition applications (PlanIt), OpenStreetMap's own
demolitions, and the grid references the registers give their positions in."""
import io
import json
import os
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from bandobuddy import opendata, osm
from bandobuddy.scoring import _condition_from
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
                 Resp(200, {"records": [self.record(4, app_state="Permitted")], "total": 1}),
                 Resp(200, {"records": [self.record(5, "Completion of partially built dwelling")], "total": 1}),
                 Resp(200, {"records": [self.record(6, "Siting of a caravan whilst the house is renovated")], "total": 1})]
        service = Service(lambda url, params: pages.pop(0))
        waits = []
        fetch = opendata.PlanIt(batch=2, conversions="")
        with mock.patch.object(opendata.PlanIt, "_wait", lambda self, cancel, s: waits.append(s)):
            rows = list(fetch(service, nothing))
        self.assertEqual([r["name"] for r in rows], ["Area/1", "Area/3", "Area/4", "Area/5", "Area/6"])   # one had no position
        self.assertEqual(waits, [61, 90, 61, 61, 61])             # a minute between requests; longer when asked
        asked = [c[1] for c in service.calls]
        # New applications in the last fortnight, then decisions in it, never "everything that changed";
        # then every unfinished or unlivable house there's ever been, a page of them, and every one being done up.
        self.assertEqual([(a.get("recent"), a.get("decided"), a["page"]) for a in asked],
                         [(14, None, 1), (14, None, 2), (14, None, 2), (None, 14, 1), (None, None, 1), (None, None, 1)])
        self.assertNotIn("different", asked[0])
        self.assertIn("demolition derelict or demolish derelict", asked[0]["search"])
        self.assertIn('demolish "fire damaged"', asked[0]["search"])
        self.assertIn(' or derelict or dilapidated or ruinous or "fire damaged"', asked[0]["search"])   # demolition or not
        self.assertIn('"partially built dwelling" or ', asked[4]["search"])
        self.assertIn(' or uninhabitable or "unfit for habitation"', asked[4]["search"])
        self.assertNotIn("demolition", asked[4]["search"])
        self.assertIn('caravan renovated or caravan renovation', asked[5]["search"])

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
                      "Change of use of vacant shop to cafe",                    # empty, not falling down
                      "Emergency Tree Works: T1, T2 Oak: Fell fire damaged tree.",
                      "Replacement of 3 no. very dilapidated sash and case windows",
                      "Removal of abandoned vehicles and caravans from the site"):
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
        # Falling down, with no demolition in sight: still standing, for now.
        stables = judge(self.record(8, "Restoration and conversion of derelict stables to form estate shoot lodge"))
        self.assertEqual((stables["kind"], stables["weight"]), ("building", 20))
        self.assertIn("applied for on 2026-09-01, no decision yet", stables["evidence"])
        self.assertEqual(_condition_from(stables["evidence"]), "Abandoned")
        barn = judge(self.record(9, "Change of use of derelict barn to dwelling"))
        self.assertEqual((barn["kind"], barn["weight"]), ("farm buildings", 8))     # a great many of those
        done = judge(self.record(10, "Conversion of dilapidated chapel to dwelling", app_state="Conditions",
                                 decided_date="2022-05-01"))
        self.assertEqual(done["weight"], 5)
        self.assertIn("approved on 2022-05-01, so the work may well be done", done["evidence"])
        self.assertNotIn("demolition", done["evidence"])
        # Begun and never finished, or finished and never lived in (Golden Hill, near Romsey).
        shell = judge(self.record(11, "Completion of partially built dwelling for use as Holiday let",
                                  app_state="Rejected", decided_date="2014-05-01"))
        self.assertEqual((shell["kind"], shell["weight"]), ("unfinished house", 14))
        self.assertEqual(_condition_from(shell["evidence"]), "Unfinished")
        never = judge(self.record(12, "Change of use from Gym and Creche facility (Unit never occupied) to retail"))
        self.assertEqual((never["kind"], _condition_from(never["evidence"])), ("building", "Empty"))
        long_one = judge(self.record(13, "A FULL APPLICATION FOR: (1) DEMOLITION OF BUILDINGS AT PLOTS 3 & 4 (2) REMOVAL "
                                         "OF CONTAINERS AND CARAVAN; (3) ERECTION OF NEW DWELLINGS AT PLOTS 3 & 4; (4) "
                                         "OPERATIONAL WORKS AND RETENTION OF PARTLY BUILT DWELLING AT PLOT 2"))
        self.assertIn("PARTLY BUILT DWELLING", long_one["evidence"])       # quoted around what matters
        self.assertTrue(long_one["evidence"].split('"')[1].startswith("…"))
        # A house that can't be lived in, said of the house.
        unfit = judge(self.record(14, "Renovation of semi derelict and uninhabitable cottage to provide a single dwelling"))
        self.assertEqual((unfit["kind"], unfit["weight"]), ("house", 18))
        gone = judge(self.record(15, "Demolish existing uninhabitable house and outbuildings and make ground good",
                                 app_state="Permitted", decided_date="2023-08-01"))
        self.assertEqual((gone["weight"], _condition_from(gone["evidence"])), (5, "Demolition approved"))
        self.assertEqual(_condition_from(judge(self.record(16, "Demolition of existing house (condemned as unfit for "
                                                               "habitation due to damp)"))["evidence"]), "Empty")
        for part in ("Conversion of an existing uninhabitable loft space into a habitable bedroom",
                     "Demolish uninhabitable annex/garage, single storey side and rear extension",
                     "Renovation works to Annex which is currently uninhabitable",
                     "Fell T1 Lime: secretion making the whole area uninhabitable"):
            self.assertIsNone(judge(self.record(17, part)), part)
        # Living in a caravan on the plot while the house is done up (3 Segensworth Road, Titchfield, in 2018:
        # still empty years later). Most get finished, so it fades like any approval.
        caravan = judge(self.record(18, "Siting of residential caravan to enable the refurbishment of the dwellinghouse"))
        self.assertEqual((caravan["kind"], caravan["weight"]), ("house", 12))
        self.assertIn("so it couldn't be lived in then", caravan["evidence"])
        self.assertEqual(_condition_from(caravan["evidence"]), "Empty")
        segensworth = judge(self.record(19, "Change Of Use Of Land For A Period Of Two Years For The Siting Of A Static "
                                            "Caravan To Be Used As Part Of The Residential Use Of 3 Segensworth Road "
                                            "Whilst The Property Is Being Renovated", app_state="Permitted",
                                        decided_date="2019-01-20"))
        self.assertEqual(segensworth["weight"], 5)
        self.assertIn("so the work may well be done", segensworth["evidence"])
        self.assertTrue(judge(self.record(20, "Lawful development certificate for proposed stationing of caravan "
                                              "whilst the dwelling is being renovated")))       # says so all the same
        for not_a_house in ("Reconfiguration of pitches; refurbishment of the holiday park's static caravans",
                            "Change of use of land for one pitch including one static caravan, refurbishment of "
                            "hardstanding to form a residential Gypsy/Traveller site",
                            "Demolition of existing dwelling and erection of replacement dwelling, retention of static "
                            "mobile home during reconstruction"):
            self.assertIsNone(judge(self.record(21, not_a_house)), not_a_house)
        for nothing_decided in ("Pre-application advice for demolition of the former Wesleyan school",
                                "Certificate of lawfulness for proposed demolition of redundant farm buildings"):
            self.assertIsNone(judge(self.record(7, nothing_decided)), nothing_decided)

    def test_a_house_done_up_long_ago_is_looked_up_again(self):
        # 3 Segensworth Road, Titchfield: a caravan on the plot while it was renovated in 2018, then in 2023 an
        # application to knock the house down. Looked up again by where it is, and only the same house counts.
        caravan = self.record(3, "Siting of a static caravan whilst the property is being renovated",
                              address="3 Segensworth Road Titchfield Fareham PO15 5DY", app_state="Permitted",
                              start_date="2018-11-27", decided_date="2019-01-02")
        lately = self.record(4, "Siting of a caravan whilst the house is renovated")      # too soon to tell
        nearby = [caravan,
                  self.record(5, "Demolition Of Existing House And Construction Of 2 New Houses",
                              address="3 Segensworth Road Titchfield Fareham PO15 5DY", app_state="Permitted",
                              start_date="2023-05-23", decided_date="2024-06-28"),
                  self.record(6, "Demolition of existing bungalow", address="13 Segensworth Road, Titchfield"),
                  self.record(7, "Single storey rear extension", address="3 Segensworth Road, Titchfield",
                              start_date="2020-03-01"),
                  self.record(8, "Log cabin in the rear garden", address="Oak Cottage Mill Lane Titchfield")]
        answers = [Resp(200, {"records": [caravan, lately], "total": 2})] + [Resp(200, {"records": [], "total": 0})] * 3 \
            + [Resp(200, {"records": nearby, "total": 5})]
        service = Service(lambda url, params: answers.pop(0))
        tmp = Path(tempfile.mkdtemp())
        waits = []
        quick = mock.patch.object(opendata.PlanIt, "_wait", lambda self, cancel, s: waits.append(s))
        with quick:
            rows = list(opendata.PlanIt(conversions="")(service, nothing, data_dir=tmp))
        self.assertEqual(len(service.calls), 5)
        self.assertEqual(waits, [61] * 4)                         # a minute between requests, as ever
        around = service.calls[4][1]
        self.assertEqual((around["lat"], around["lng"], around["krad"], around["start_date"]),
                         (51.07, -1.8, 0.1, "2018-11-27"))
        self.assertEqual([r["name"] for r in rows], ["Area/3", "Area/4", "Area/3"])   # again, with what came since
        self.assertEqual([r["name"] for r in rows[2]["later"]], ["Area/5"])
        house = opendata._planit(rows[2])
        self.assertEqual(house["weight"], 20)
        self.assertIn("(approved on 2019-01-02)", house["evidence"])
        self.assertIn('then, in 2023, another application here (5, approved on 2024-06-28): "Demolition Of Existing '
                      'House And Construction Of 2 New Houses", so it seems the work was never finished',
                      house["evidence"])
        self.assertEqual(_condition_from(house["evidence"]), "Empty")
        self.assertIn(("applied again", "2023-05-23"), house["dates"])
        self.assertEqual(opendata._planit(rows[0])["weight"], 5)    # the first time round: may well be done
        # Next week: what was found is remembered, and nobody's asked again for six months.
        answers[:] = [Resp(200, {"records": [caravan], "total": 1})] + [Resp(200, {"records": [], "total": 0})] * 3
        with quick:
            rows = list(opendata.PlanIt(conversions="")(service, nothing, data_dir=tmp))
        self.assertEqual(len(service.calls), 9)
        self.assertEqual(opendata._planit(rows[0])["weight"], 20)
        # Knocking it down approved long enough ago to have lapsed: it may be gone.
        old = opendata._planit({**caravan, "later": [{**nearby[1], "decided_date": "2020-01-01"}]})
        self.assertEqual(old["weight"], 12)
        self.assertIn("it may since have gone", old["evidence"])

    def test_which_later_applications_count(self):
        same = opendata._same_house
        self.assertTrue(same("3 Segensworth Road Titchfield Fareham PO15 5DY", "Land at 3 Segensworth Road, Titchfield"))
        self.assertTrue(same("Land to the rear of 5 Mill Lane, Town", "5 Mill Lane Town AB1 2CD"))
        self.assertFalse(same("3 Segensworth Road Titchfield", "13 Segensworth Road Titchfield"))
        self.assertFalse(same("River Bank House Mill Lane Titchfield", "Oak Cottage Mill Lane Titchfield"))
        self.assertFalse(same("", "3 Segensworth Road"))
        gave_up = lambda said, on="2024-01-01": opendata._gave_up_on({"description": said, "start_date": on},  # noqa
                                                                    "2018-01-01")
        self.assertFalse(gave_up("Retention of a mobile home during renovation of the dwelling", "2018-09-01"))  # resent
        self.assertTrue(gave_up("Demolition of the existing dwelling", "2018-09-01"))
        # Not followed up when it was to be knocked down anyway: applying again to knock it down says nothing new.
        old = {"description": "Demolish existing uninhabitable house and outbuildings", "start_date": "2020-01-01"}
        self.assertFalse(opendata._worth_a_second_look(old, date.today()))
        self.assertTrue(opendata._worth_a_second_look({**old, "description": "Renovation of uninhabitable cottage"},
                                                      date.today()))
        again = {"name": "Area/2", "description": "Demolition of the existing dwelling", "start_date": "2023-12-22"}
        self.assertEqual(opendata._planit({**self.record(1, old["description"], start_date="2020-01-01"),
                                           "later": [again]})["weight"], 18)       # as it was, no later news
        for yes in ("Demolition of existing dwelling and erection of replacement dwelling",
                    "Demolish existing bungalow and erect two houses",
                    "Siting of a mobile home whilst the house is refurbished",
                    "Completion of partially built dwelling"):
            self.assertTrue(gave_up(yes), yes)
        for no in ("Demolition of existing garage and erection of two storey side extension",
                   "Single storey rear extension", "Details pursuant to condition 3 of P/23/0734/FP: demolition of "
                   "existing house", "Discharge of condition 2: replacement dwelling"):
            self.assertFalse(gave_up(no), no)

    def test_a_house_converted_into_flats_again_and_again(self):
        # Golden Hill, Belbins: built in 2004 and never lived in; approved for conversion into flats in 2019 and
        # 2022, and applied for again in 2025 (UK PlanIt's own records).
        gh = "Golden Hill Belbins Romsey Hampshire SO51 0PE"
        tv = lambda ref, said, **kw: self.record(ref, said, name=f"TestValley/{ref}", **kw)   # noqa: E731
        apps = [tv("18/02547/FULLS", "Conversion of existing house and garage into ten dwellings", address=gh,
                   app_state="Conditions", start_date="2018-09-27", decided_date="2019-01-08"),
                tv("19/00531/FULLS", "Conversion of existing house and garage into 11 dwellings",
                   address="Golden Hill Belbins Romsey Romsey Extra", app_state="Withdrawn", start_date="2019-03-01"),
                tv("22/00362/FULLS", "Conversion of existing house and garage into 10 flats", address=gh,
                   app_state="Conditions", start_date="2022-02-10", decided_date="2022-06-01"),
                tv("22/01999/DIS", "Discharge of condition 3 of 22/00362/FULLS: conversion of existing house into 10 "
                                   "flats", address=gh, start_date="2023-01-05"),
                tv("25/00599/FULLS", "Conversion of house into 8 flats, erection of 2 houses, and installation of "
                                     "package treatment plant", address=gh, start_date="2025-03-13"),
                # Split in two, again and again: ordinary.
                self.record("Area/2", "Conversion of existing house into two flats", address="2 Mill Lane, Town",
                            app_state="Permitted", start_date="2015-01-01", decided_date="2015-03-01"),
                self.record("Area/3", "Conversion of existing house into two flats", address="2 Mill Lane, Town",
                            start_date="2020-01-01"),
                # Applied for again a few months on: a revised scheme, not a conversion that never happened.
                self.record("Area/4", "Conversion of house into 6 flats", address="Oak House, Lea Road, Town",
                            app_state="Permitted", start_date="2023-01-01", decided_date="2023-03-01"),
                self.record("Area/5", "Conversion of house into 7 flats", address="Oak House, Lea Road, Town",
                            start_date="2023-09-01")]
        for app in apps:
            app["name"] = "TestValley/" + app["name"].split("/", 1)[-1] if "FULLS" in app["name"] or "DIS" in app["name"] \
                else app["name"]
        leads = list(opendata._converted_again(apps))
        self.assertEqual(len(leads), 1)
        house = opendata._planit(leads[0])
        self.assertEqual((house["kind"], house["weight"], house["name"]),
                         ("house", 16, "Golden Hill Belbins Romsey Hampshire"))
        self.assertIn("Planning applications to convert it into flats: approved in 2019 and 2022, and applied for "
                      "again in 2025, so neither was carried out", house["evidence"])
        self.assertEqual(house["ref"], "conversions/TestValley|golden hill belbins")
        self.assertEqual(house["dates"], [("applied for", "2025-03-13"), ("decided", None)])

    def test_conversions_asked_for_once_in_full_then_only_whats_new(self):
        conv = lambda n, **kw: self.record(n, "Conversion of existing house into ten flats",   # noqa: E731
                                           address="The Grange, Lea Road, Town", **kw)
        answers = [Resp(200, {"records": [], "total": 0}),                       # the fortnight's demolitions
                   Resp(200, {"records": [conv(1)], "total": 5}),                 # too many for one search: split
                   Resp(200, {"records": [conv(1, app_state="Permitted", start_date="2015-01-01",
                                               decided_date="2015-04-01"), conv(2)], "total": 3}),
                   Resp(200, {"records": [conv(3)], "total": 3}),
                   Resp(200, {"records": [conv(4, start_date="2020-06-01"), conv(5)], "total": 2})]
        service = Service(lambda url, params: answers.pop(0))
        tmp = Path(tempfile.mkdtemp())
        waits = []
        fetch = opendata.PlanIt(batch=2, slice_max=3, windows=("recent",), stalled="", caravans="")
        with mock.patch.object(opendata.PlanIt, "_wait", lambda self, cancel, s: waits.append(s)):
            rows = list(fetch(service, nothing, data_dir=tmp))
            self.assertEqual(waits, [61] * 4)                       # a minute between requests, as ever
            whole, first, first_next, second = [(c[1]["start_date"], c[1]["end_date"], c[1]["page"])
                                                for c in service.calls[1:]]
            today = date.today().isoformat()
            self.assertEqual(whole, ("2000-01-01", today, 1))       # too many: the dates split in two
            self.assertEqual((first[0], first_next[:2], first_next[2]), ("2000-01-01", first[:2], 2))
            self.assertLess(first[1], second[0])
            self.assertEqual((second[1], second[2]), (today, 1))
            self.assertEqual([r["name"] for r in rows if r.get("conversions")], ["Area/5"])   # approved 2015, again 2020
            memory = json.loads((tmp / "planit_conversions.json").read_text(encoding="utf-8"))
            self.assertTrue(memory["backfilled"])
            self.assertEqual(sorted(memory["apps"]), ["Area/1", "Area/2", "Area/3", "Area/4", "Area/5"])
            # Next week: only what's been applied for lately, and the house again, from what's kept.
            answers[:] = [Resp(200, {"records": [], "total": 0}), Resp(200, {"records": [], "total": 0})]
            service.calls.clear()
            rows = list(fetch(service, nothing, data_dir=tmp))
        self.assertEqual([c[1].get("recent") for c in service.calls], [14, 14])
        self.assertNotIn("start_date", service.calls[1][1])
        self.assertEqual([r["name"] for r in rows if r.get("conversions")], ["Area/5"])

    def test_off_unless_switched_on_and_never_forgets_older_ones(self):
        self.assertFalse(opendata.DATASETS["planit"].enabled())
        with mock.patch.dict(os.environ, {"BANDOBUDDY_PLANIT": "1"}):
            self.assertTrue(opendata.DATASETS["planit"].enabled())
        tmp = Path(tempfile.mkdtemp())
        store = Store(tmp / "t.db")
        batches = [[self.record(1)], [self.record(2, location_x=-1.81)]]
        dataset = opendata.Dataset(**{**opendata.DATASETS["planit"].__dict__,
                                      "fetch": lambda session, progress, cancel=None, data_dir=None: iter(
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

        def fetch(session, progress, cancel=None, data_dir=None):
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
