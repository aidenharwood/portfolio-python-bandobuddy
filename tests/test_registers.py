"""Closed care homes and hospitals (CQC), empty NHS sites, closed railways' tunnels and viaducts, and the MOD's
disposals: read from small stand-ins for each register's files."""
import io
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape

from bandobuddy import geocode, opendata, osm, registers
from bandobuddy.config import DB_NAME
from bandobuddy.geo import bng_to_wgs84, grid_ref_to_bng
from bandobuddy.scoring import _condition_from, category_for
from bandobuddy.store import Store


class Resp:
    def __init__(self, content=b"", text=None, status=200):
        self.content, self.status_code = content, status
        self.text = text if text is not None else content.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, size):
        for i in range(0, len(self.content), size):
            yield self.content[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Site:
    """Answers by the end of the URL; anything else is a request the test didn't expect."""

    def __init__(self, pages: dict):
        self.pages, self.asked = pages, []

    def get(self, url, **_kw):
        self.asked.append(url)
        for end, answer in self.pages.items():
            if url.endswith(end):
                return answer if isinstance(answer, Resp) else Resp(text=answer)
        raise AssertionError(f"unexpected request: {url}")


def nothing(*_args):
    pass


def ods(sheets: dict[str, list[list[str]]]) -> bytes:
    """An OpenDocument spreadsheet, with the blank cells "repeated" the way real ones are."""
    tables = []
    for name, rows in sheets.items():
        xml_rows = []
        for row in rows:
            cells = "".join('<table:table-cell table:number-columns-repeated="2"/>' if v is None
                            else f'<table:table-cell office:value-type="string"><text:p>{escape(v)}</text:p></table:table-cell>'
                            for v in row)
            xml_rows.append(f'<table:table-row>{cells}<table:table-cell table:number-columns-repeated="16000"/>'
                            '</table:table-row>')
        tables.append(f'<table:table table:name="{name}">{"".join(xml_rows)}</table:table>')
    content = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content '
               'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:spreadsheet>'
               f'{"".join(tables)}</office:spreadsheet></office:body></office:document-content>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("content.xml", content)
    return out.getvalue()


def xlsx(rows: list[list[str]]) -> bytes:
    """An Excel workbook: text in the shared strings, a gap in one row."""
    shared, xml_rows = [], []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, value in enumerate(row):
            if value == "":
                continue
            shared.append(value)
            cells.append(f'<c r="{chr(65 + c)}{r}" t="s"><v>{len(shared) - 1}</v></c>')
        xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}>{"".join(f"<si><t>{escape(s)}</t></si>" for s in shared)}</sst>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
    return out.getvalue()


class SpreadsheetTests(unittest.TestCase):
    def test_one_sheet_of_an_ods_read_as_it_goes(self):
        data = ods({"Notes": [["About this file"]], "Data": [["ID", "Name"], ["1", None, "Far"], []]})
        self.assertEqual(list(registers.ods_stream(io.BytesIO(data), "Data")), [["ID", "Name"], ["1", "", "", "Far"], []])
        self.assertEqual(registers._first_sheet(data), "Notes")
        self.assertEqual(list(registers._records_stream(registers.ods_stream(io.BytesIO(data), "Data"), "ID")),
                         [{"ID": "1", "Name": ""}])

    def test_xlsx_rows_keep_their_columns(self):
        rows = registers.xlsx_rows(xlsx([["Title of the report"], ["A", "B", "C"], ["1", "", "3"]]))
        self.assertEqual(rows, [["Title of the report"], ["A", "B", "C"], ["1", "", "3"]])
        self.assertEqual(registers._records(rows, "B"), [{"A": "1", "B": "", "C": "3"}])
        with self.assertRaises(RuntimeError):
            registers._records(rows, "Missing")


class GridRefTests(unittest.TestCase):
    def test_letters_and_digits_to_the_middle_of_the_square(self):
        self.assertEqual(grid_ref_to_bng("SN693694"), (269350, 269450))
        self.assertEqual(grid_ref_to_bng("HT 97429 38889"), (397429.5, 1138889.5))
        lat, lng = bng_to_wgs84(*grid_ref_to_bng("HT 97429 38889"))      # the Haa of Foula, Shetland
        self.assertAlmostEqual(lat, 60.14, places=1)
        self.assertAlmostEqual(lng, -2.05, places=1)
        self.assertEqual(grid_ref_to_bng("tq 30 80"), (530500, 180500))     # a kilometre square
        for bad in ("", "SN6936945", "II123456", "12345678", "SN", None):
            self.assertIsNone(grid_ref_to_bng(bad), bad)


class KeptTests(unittest.TestCase):
    def test_kept_while_the_source_is_the_same_and_young_enough(self):
        tmp = Path(tempfile.mkdtemp())
        registers.Kept(tmp, "thing").keep("file-2026-09", [{"ref": "1"}], places={"1": [51, -1]})
        kept = registers.Kept(tmp, "thing")
        self.assertEqual(kept.fresh("file-2026-09"), [{"ref": "1"}])
        self.assertIsNone(kept.fresh("file-2026-10"))
        self.assertEqual(kept.data["places"], {"1": [51, -1]})
        self.assertEqual(kept.fresh(max_age_days=1), [{"ref": "1"}])
        kept.data["at"] = (datetime.now() - timedelta(days=400)).isoformat()
        self.assertIsNone(kept.fresh(max_age_days=365))
        self.assertIsNone(registers.Kept(None, "thing").fresh())


# -- CQC --------------------------------------------------------------------------------------------

CQC_HEAD = ["Location ID", "Location Name", "Care home?", "Care homes beds at point location de-activated",
            "Location Primary Inspection Category", "Location Type/Sector", "Location HSCA End Date",
            "Location Latitude", "Location Longitude", "Location UPRN ID", "Location Postal Code",
            "Location Street Address", "Location Address Line 2", "Location City"]
RECENT = (date.today() - timedelta(days=400)).strftime("%d/%m/%Y")


def cqc_row(ref, name, care_home, beds, category, sector, ended, uprn, postcode, street):
    return [ref, name, care_home, str(beds), category, sector, ended, "51.5", "-1.5", uprn, postcode, street, None, "Town"]


CQC_ROWS = [
    # Closed with 45 beds, and nothing there now: a good lead. Its earlier registration is the same building.
    cqc_row("1-1", "Church View Care Home", "Y", 45, "Residential social care", "Social Care Org", RECENT, "100", "AB1 2CD", "1 Hill Road"),
    cqc_row("1-0", "Church View", "Y", 45, "Residential social care", "Social Care Org", "01/02/2012", "100", "AB1 2CD", "1 Hill Road"),
    # Too small: a house that went back to being a house.
    cqc_row("2-1", "Rose Cottage", "Y", 6, "Residential social care", "Social Care Org", RECENT, "200", "AB1 3CD", "2 Rose Lane"),
    # Under new owners: the same address is registered again in the directory.
    cqc_row("3-1", "Elm Lodge", "Y", 30, "Residential social care", "Social Care Org", RECENT, "300", "AB1 4CD", "Elm Lodge, 3 Elm Road"),
    # An NHS hospital, closed long ago.
    cqc_row("4-1", "St Anne's Hospital", "N", 0, "Hospital - mental health/capacity", "NHS Healthcare Organisation",
            "31/03/2011", "400", "AB2 1AA", "Hospital Lane"),
    # A private clinic in a house: not a hospital building.
    cqc_row("5-1", "Mill House", "N", 0, "Hospital - mental health/capacity", "Independent Healthcare Org", RECENT,
            "500", "AB2 2AA", "5 Mill Lane"),
]
CQC_ACTIVE = "Name,Address,Postcode\r\nElm Lodge Care,\"Elm Lodge, 3 Elm Road, Town\",AB1 4CD\r\nOther,9 Far Road,ZZ9 9ZZ\r\n"


def cqc_site():
    return Site({
        "using-cqc-data": '<a href="/files/30_September_2026_Deactivated_Locations.ods">x</a>'
                          '<a href="/files/30_September_2026_CQC_directory.csv">y</a>',
        "Deactivated_Locations.ods": Resp(ods({"Contents": [["Read me"]],
                                               "Deactivated_Locations": [["Deactivated locations"], CQC_HEAD, *CQC_ROWS]})),
        "CQC_directory.csv": Resp(("Care directory\r\n" + CQC_ACTIVE).encode("utf-8-sig")),
    })


class CqcTests(unittest.TestCase):
    def test_closed_buildings_not_changes_of_owner(self):
        tmp = Path(tempfile.mkdtemp())
        rows = list(registers.CqcClosures()(cqc_site(), nothing, data_dir=tmp))
        self.assertEqual(sorted(r["ref"] for r in rows), ["1-1", "4-1"])
        items = {i["ref"]: i for i in (opendata._cqc(r) for r in rows)}
        home = items["1-1"]
        self.assertEqual((home["kind"], home["weight"]), ("care home", 24))
        self.assertIn("closed in", home["evidence"])
        self.assertIn("45 beds", home["evidence"])
        self.assertEqual(home["url"], "https://www.cqc.org.uk/location/1-1")
        self.assertEqual((items["4-1"]["kind"], items["4-1"]["weight"]), ("hospital", 8))   # 15 years on: faint
        self.assertTrue(_condition_from(home["evidence"]).startswith("Closed 20"))
        # A care home called Church View is a care home.
        self.assertEqual(category_for({"name": "Church View Care Home", "open": [home]}), "institutional")
        # Kept: the same month's files aren't read again.
        again = Site({"using-cqc-data": cqc_site().pages["using-cqc-data"]})
        self.assertEqual(len(list(registers.CqcClosures()(again, nothing, data_dir=tmp))), 2)


# -- NHS estates ----------------------------------------------------------------------------------------

NHS_CSV = ("Site Code,Site Name,Trust Name,Site Type,Post Code,Gross internal floor area (m²),"
           "Internal floor area - unoccupied (m²),Floor area - empty (m²),Age profile - pre 1948 (%)\r\n"
           "RX1,ST AGNES HOSPITAL,NORTH NHS TRUST,General acute,AB1 1AA,\"12,000\",\"12,000\",0,80\r\n"
           "RX2,Hill Clinic,North NHS Trust,Community,AB1 1AB,4000,0,2500,0\r\n"
           "RX3,Busy Clinic,North NHS Trust,Community,AB1 1AC,4000,0,300,0\r\n"
           "RX4,OTHER REPORTABLE SITES,SOUTH NHS TRUST,Unoccupied,AB1 1AD,0,0,0,0\r\n").encode("cp1252")


class NhsTests(unittest.TestCase):
    def test_wholly_and_mostly_empty_sites(self):
        series = "estates-returns-information-collection"
        site = Site({
            series: '<a href="/x/summary-page-and-dataset-for-eric-2024-25">a</a>'
                    '<a href="/x/summary-page-and-dataset-for-eric-2025-26">b</a>',
            "eric-2025-26": "Coming soon",
            "eric-2024-25": '<a href="https://files.digital.nhs.uk/ERIC%20-%202024_25%20-%20Site%20data.csv">site data</a>',
            "Site%20data.csv": Resp(NHS_CSV),
        })
        with mock.patch.object(geocode, "postcode_point", return_value=(52.0, -1.0)):
            rows = list(registers.NhsEstates()(site, nothing, data_dir=Path(tempfile.mkdtemp())))
        self.assertEqual([r["ref"] for r in rows], ["RX1", "RX2", "RX4"])
        items = {i["ref"]: i for i in map(opendata._nhs_estate, rows)}
        self.assertEqual((items["RX1"]["name"], items["RX1"]["kind"], items["RX1"]["weight"]),
                         ("St Agnes Hospital", "hospital", 26))
        self.assertIn("(2024/25): the whole site (12,000 m²) is unoccupied", items["RX1"]["evidence"])
        self.assertEqual(_condition_from(items["RX1"]["evidence"]), "Empty")
        self.assertIn("2,500 of its 4,000 m² stand empty", items["RX2"]["evidence"])
        self.assertEqual(_condition_from(items["RX2"]["evidence"]), "Empty")
        self.assertEqual(items["RX4"]["name"], "Empty site of South NHS Trust")


# -- Historical Railways Estate --------------------------------------------------------------------------

HRE = [["ELR", "ELR.LineName", "RPL or Sustrans?", "StructureType", "Status", "OSReference", "Name", "EPIMRef"],
       ["BGM", "Bath Green Park - Mangotsfield", " ", "Tunnel", "Other", "ST 7547 6385", "Combe Down Tunnel", "E1"],
       ["BGM", "Bath Green Park - Mangotsfield", " ", "Tunnel", "Other", "ST 7547 6385", "Combe Down Tunnel", "E1"],
       ["CKP", "Cockermouth - Penrith", "Railway Path", "Viaduct", "Spans Water", "NY 1234 5678", "", "E2"],
       ["CKP", "Cockermouth - Penrith", " ", "Overbridge", "Public Road", "NY 1200 5600", "Road Bridge", "E3"],
       ["CKP", "Cockermouth - Penrith", " ", "Tunnel", "Other", "", "Lost Tunnel", "E4"]]


class RailwayEstateTests(unittest.TestCase):
    def test_tunnels_and_viaducts(self):
        site = Site({"HRE+structures.xlsx": Resp(xlsx(HRE))})
        rows = list(registers.RailwayEstate()(site, nothing, data_dir=None))
        self.assertEqual([r["ref"] for r in rows], ["E1", "E2"])
        tunnel, viaduct = map(opendata._railway_estate, rows)
        self.assertEqual((tunnel["kind"], tunnel["weight"]), ("railway tunnel", 26))
        self.assertIn("a tunnel on a closed railway (Bath Green Park - Mangotsfield)", tunnel["evidence"])
        self.assertEqual(_condition_from(tunnel["evidence"]), "Disused")
        self.assertEqual((viaduct["name"], viaduct["weight"]), ("Railway viaduct (Cockermouth - Penrith)", 10))
        self.assertEqual(_condition_from(viaduct["evidence"]), "Reused")         # a path now
        # A tunnel under Colliery Lane is a tunnel.
        self.assertEqual(category_for({"name": "Colliery Lane Tunnel", "open": [tunnel]}), "tunnels")


# -- MOD disposals ---------------------------------------------------------------------------------------

MOD_HEAD = ["ID", "Status", "Primary Establishment Name", "Primary Parcel Name", "Disposal From", "Address", "Town",
            "County", "Total Area (ha)"]
MOD_ROWS = [
    ["1", "Assessment", "ALANBROOKE BARRACKS", "ALANBROOKE BARRACKS", "2025", "Topcliffe", "Thirsk", "N Yorks", "150"],
    ["2", "Assessment", "CATTERICK TRAINING AREA", "LAND AT HARLEY HILL", "2025", "", "Catterick", "N Yorks", "43"],
    ["3", "Delivery", "RAF HENLOW", "NORTH SITE", "2030", "", "Henlow", "Beds", "201"],
    ["4", "Delivery", "RAF HENLOW", "NORTH SITE", "2030", "", "Henlow", "Beds", "0"],
    ["5", "Assessment", "HARDEN BARRACKS", "PINHILL MESSES, HARDEN BARRACKS", "2025", "", "Catterick", "N Yorks", "2"],
]


class ModTests(unittest.TestCase):
    def test_sites_found_once_and_judged(self):
        tmp = Path(tempfile.mkdtemp())
        site = lambda: Site({"disposal-database-house-of-commons-report": '<a href="/media/House_of_Commons_Report_.ods">r</a>',  # noqa: E731
                             "House_of_Commons_Report_.ods": Resp(ods({"Report": [MOD_HEAD, *MOD_ROWS]}))})
        with mock.patch.object(geocode, "search", return_value={"lat": 54.2, "lng": -1.3}) as search:
            rows = list(registers.MoDisposals()(site(), nothing, data_dir=tmp))
        self.assertEqual([r["ref"] for r in rows], ["1", "3", "5"])      # not the field, nor the same parcel twice
        self.assertEqual(search.call_count, 3)
        with mock.patch.object(geocode, "search", side_effect=AssertionError("looked up again")):
            self.assertEqual(len(list(registers.MoDisposals()(site(), nothing, data_dir=tmp))), 3)
        items = {i["ref"]: i for i in map(opendata._mod, rows)}
        with mock.patch("bandobuddy.opendata.date") as when:
            when.today.return_value = date(2026, 10, 1)
            items = {i["ref"]: i for i in map(opendata._mod, rows)}
        self.assertEqual((items["1"]["name"], items["1"]["kind"], items["1"]["weight"]),
                         ("Alanbrooke Barracks", "barracks", 18))
        self.assertEqual(_condition_from(items["1"]["evidence"]), "Disused")
        self.assertEqual((items["3"]["name"], items["3"]["kind"], items["3"]["weight"]),
                         ("North Site, RAF Henlow", "airfield", 8))
        self.assertEqual(_condition_from(items["3"]["evidence"]), "Closing")
        self.assertEqual((items["5"]["name"], items["5"]["kind"]), ("Pinhill Messes, Harden Barracks", "officers' mess"))
        self.assertEqual(category_for({"name": items["5"]["name"], "open": [items["5"]]}), "military")


    def test_a_refused_search_is_tried_again_next_time(self):
        tmp = Path(tempfile.mkdtemp())
        site = lambda: Site({"disposal-database-house-of-commons-report": '<a href="/media/House_of_Commons_Report_.ods">r</a>',  # noqa: E731
                             "House_of_Commons_Report_.ods": Resp(ods({"Report": [MOD_HEAD, *MOD_ROWS]}))})
        answers = [{"lat": 54.2, "lng": -1.3}, geocode.Busy("pause")]
        with mock.patch.object(geocode, "search", side_effect=answers) as search:
            rows = list(registers.MoDisposals()(site(), nothing, data_dir=tmp))
        self.assertEqual([r["ref"] for r in rows], ["1"])
        self.assertEqual(search.call_count, 2)                      # and no more once Nominatim's said stop
        self.assertEqual(set(registers.Kept(tmp, "mod_disposals").data["places"]), {"1"})
        with mock.patch.object(geocode, "search", side_effect=[None, None, None, {"lat": 54.4, "lng": -1.7}]) as search:
            rows = list(registers.MoDisposals()(site(), nothing, data_dir=tmp))
        self.assertEqual([r["ref"] for r in rows], ["1", "5"])
        self.assertEqual(search.call_count, 4)                      # RAF Henlow three ways, then Pinhill Messes
        self.assertIsNone(registers.Kept(tmp, "mod_disposals").data["places"]["3"])   # found nothing: remembered


class NominatimTests(unittest.TestCase):
    def tearDown(self):
        geocode._paused_until = geocode._last = 0.0

    def test_a_429_pauses_searching(self):
        class Asked:
            status_code, headers, calls = 429, {"Retry-After": "120"}, 0

            def get(self, *_a, **_kw):
                self.calls += 1
                return self

        session = Asked()
        with self.assertRaises(geocode.Busy):
            geocode.search("Clive Barracks, Market Drayton", session)
        with self.assertRaises(geocode.Busy):
            geocode.search("Dale Barracks, Chester", session)
        self.assertEqual(session.calls, 1)                          # the second never asked
        self.assertGreater(geocode._paused_until - geocode._last, 100)


# -- Best spots, and things that are never places ---------------------------------------------------------

class BestSpotsTests(unittest.TestCase):
    def site(self, key, condition="Abandoned", kind="hospital", score=30, category="institutional", name=None):
        return {"key": key, "name": name or key, "lat": 51.0, "lng": -1.0, "score": score, "strength": "strong",
                "category": category, "condition": condition, "kind": kind, "sources": "osm", "reasons": ["x"],
                "detail": {"osm": [], "wikidata": [], "open": []}, "first_seen": "2026-01-01", "added": None}

    def test_only_standing_empty_places_worth_the_trip(self):
        store = Store(Path(tempfile.mkdtemp()) / DB_NAME)
        store.replace_sites([
            self.site("asylum"),
            self.site("closed home", condition="Closed 2024", kind="care home"),
            self.site("weak", score=12),
            self.site("field", condition="Brownfield", kind="brownfield land", category="industrial"),
            self.site("shop", condition="Disused", kind="bakery", category="leisure"),
            self.site("shaft", condition="Old workings", kind="mine shaft", category="mines"),
            self.site("hole", condition="Disused", kind="quarry", category="mines"),
            self.site("museum", condition="Heritage site", kind="castle", category="historic"),
            self.site("u1", name="Unnamed building", condition="Abandoned", kind="building", category="buildings"),
            self.site("u2", name="Unnamed bunker", condition="Old military", kind="bunker", category="bunkers"),
        ])
        best = {s["key"] for s in store.full_sites(min_score=0, best=True)}
        self.assertEqual(best, {"asylum", "closed home", "u2"})
        self.assertEqual(len(store.full_sites(min_score=0)), 10)

    def test_phone_boxes_and_post_boxes_are_not_places(self):
        self.assertTrue(osm.not_a_place({"disused:amenity": "telephone"}))
        self.assertTrue(osm.not_a_place({"amenity": "public_bookcase", "disused": "yes"}))
        self.assertFalse(osm.not_a_place({"disused:amenity": "hospital"}))
        self.assertIsNone(osm.classify({"abandoned:man_made": "petroleum_well", "abandoned": "yes"}))


class DatasetTests(unittest.TestCase):
    def test_registered_and_remembering(self):
        for key in ("care_closures", "nhs_estates", "railway_estate", "mod_disposals"):
            d = opendata.DATASETS[key]
            self.assertTrue(d.remembers, key)
            self.assertEqual(d.opt_in, key == "nhs_estates", key)      # its file host turns robots away
            self.assertTrue(d.home.startswith("https://"), key)


if __name__ == "__main__":
    unittest.main()
