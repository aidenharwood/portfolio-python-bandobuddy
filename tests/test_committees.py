"""Planning committee reports on ModernGov: the search, reading a report, finding the site, and the crawl."""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

from bandobuddy import committees, opendata
from bandobuddy.scoring import _condition_from
from bandobuddy.store import Store
from bandobuddy.updater import Updater


def tiny_pdf(pages: list[list[str]]) -> bytes:
    """A real PDF, a line of Helvetica per string, for pypdf to read."""
    objects = {1: "<< /Type /Catalog /Pages 2 0 R >>", 3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids = []
    for i, lines in enumerate(pages):
        page_id, content_id = 4 + 2 * i, 5 + 2 * i
        kids.append(f"{page_id} 0 R")
        text = " T* ".join("(" + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj"
                           for line in lines)
        stream = f"BT /F1 10 Tf 14 TL 40 800 Td {text} ET"
        objects[page_id] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>")
        objects[content_id] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", {}
    for n in sorted(objects):
        offsets[n] = len(out)
        out += f"{n} 0 obj\n{objects[n]}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{offsets[n]:010d} 00000 n \n" for n in sorted(objects)).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


# Section 2.1 of the officer's report on Golden Hill, Belbins (Test Valley, 22/00362/FULLS), as pypdf reads it.
GOLDEN_HILL = [
    " APPLICATION NO. 22/00362/FULLS \n APPLICATION TYPE FULL APPLICATION - SOUTH \n REGISTERED 11.02.2022 \n"
    " APPLICANT Mr Jose Bernardez \n SITE Golden Hill , Belbins, Romsey, SO51 0PE,   \nROMSEY EXTRA  \n"
    " PROPOSAL Conversion of existing house and garage into 10 flats \n AMENDMENTS Additional letter \n"
    " CASE OFFICER Sarah Barter \n \n1.0 INTRODUCTION \n1.1 This application is presented to committee as this is a "
    "departure.\n2.0 SITE LOCATION AND DESCRIPTION \n2.1 Golden Hill is a large detached dwelling located in Belbins, "
    "Romsey, set within \nextensive grounds. It was built as a substantial, single residence but has not \nbeen "
    "occupied since its construction in 2004. \n \n3.0 PROPOSAL \n3.1 Conversion of existing house and garage.",
    "4.0 HISTORY\n4.1 18/02547/FULLS - Conversion of existing house and garage into ten dwellings.",
]


class SearchTests(unittest.TestCase):
    def test_one_search_for_every_phrase_between_two_days(self):
        url = committees.search_url("https://democracy.example.gov.uk", date(2016, 1, 1), date(2027, 2, 1), 3)
        self.assertTrue(url.startswith("https://democracy.example.gov.uk/ieSearchResults2.aspx?SS="))
        query = unquote(url.split("SS=")[1].split("&")[0])
        self.assertEqual(query.count(" OR "), len(committees.PHRASES) - 1)
        self.assertIn('"has not been occupied since" OR "has never been occupied"', query)
        self.assertIn("&SD=01%2F01%2F2016&ED=01%2F02%2F2027&DT=3&ADV=1", url)
        self.assertTrue(url.endswith("&PG=3"))
        one = committees.search_url("https://democracy.example.gov.uk", date(2016, 1, 1), date(2027, 2, 1), 1, "161")
        self.assertIn("&DT=3&CI=161&ADV=1", one)

    FORM = """<select id="CommitteeId" name="CommitteeId"><option value="0" selected>All
      <option value="137">Cabinet<option value="161">Southern Area Planning Committee
      <option value="160">Northern Area Planning Committee<option value="436">Regulatory Committee
      <option value="220">Planning Policy Task Group<option value="195">Local Development Framework Task Group
      <option value="12">Development Control (1998-2003)</select>"""

    def test_which_committees_decide_planning_applications(self):
        self.assertEqual(committees.planning_committees(self.FORM), ["161", "160", "436"])   # not one wound up in 2003
        self.assertIsNone(committees.planning_committees("<p>No advanced search here</p>"))
        self.assertTrue(committees.is_planning("Plans Sub-Committee"))
        self.assertFalse(committees.is_planning("Planning Policy Task Group"))     # writes policy, not decisions
        self.assertFalse(committees.is_planning("Cabinet"))

    RESULTS = """<p>Results 1 to 3 for your search</p>
      <a  href="ieListDocuments.aspx?CId=161&amp;MID=3423"  >13&#47;06&#47;2023 - Southern Area Planning Committee (1)</a>
      <p><a  href="ieListDocuments.aspx?CId=161&amp;MID=3423#AI9001"  >22&#47;00362&#47;FULLS</a> APPLICATION NO...</p>
      <a  href="mgConvert2PDF.aspx?ID=25119&amp;ISATT=1#search=%22has%22"  >22_00362_FULLS SAPC Report 2
        <span class="mgHide">(1.1)</span> PDF 368 KB</a>
      <a  href="ieListDocuments.aspx?CId=137&amp;MID=4000"  >01&#47;02&#47;2024 - Cabinet (2)</a>
      <a  href="documents/s30000/Empty%20Homes%20Strategy.pdf"  >Empty Homes Strategy PDF 200 KB</a>
      <a  href="ieListDocuments.aspx?CId=161&amp;MID=4092"  >07&#47;04&#47;2026 - Southern Area Planning Committee (3)</a>
      <a  href="mgConvert2PDF.aspx?ID=39144&amp;ISATT=1"  >25_02863_FULLS SAPC Report 5 PDF 107 KB</a>
      Result Pages: <strong>1</strong><a  href="ieSearchResults2.aspx?SS=x&amp;DT=3&amp;PG=2">Page 2</a>"""

    def test_reading_the_results(self):
        hits, more = committees.parse_results(self.RESULTS, "https://tv.example", 1)
        self.assertTrue(more)
        self.assertEqual([(h["meeting"], h["committee"]) for h in hits],
                         [("2023-06-13", "Southern Area Planning Committee"), ("2024-02-01", "Cabinet"),
                          ("2026-04-07", "Southern Area Planning Committee")])
        self.assertEqual(hits[0]["url"], "https://tv.example/mgConvert2PDF.aspx?ID=25119&ISATT=1")
        self.assertEqual(hits[0]["item"], "22/00362/FULLS")
        self.assertEqual(hits[1]["url"], "https://tv.example/documents/s30000/Empty%20Homes%20Strategy.pdf")
        self.assertFalse(committees.parse_results(self.RESULTS, "https://tv.example", 2)[1])   # no page 3


class ReadingTests(unittest.TestCase):
    def test_a_real_pdf(self):
        pages = committees.read_pdf(tiny_pdf([["APPLICATION NO. 22/00362/FULLS", "SITE Golden Hill, Romsey (SO51 0PE)"],
                                              ["Page two"]]))
        self.assertEqual(len(pages), 2)
        self.assertIn("22/00362/FULLS", pages[0])
        self.assertIn("Golden Hill, Romsey (SO51 0PE)", pages[0])

    def test_the_officers_own_sentence_and_the_site_its_about(self):
        [found] = committees.find_statements(GOLDEN_HILL)
        self.assertEqual(found["sentence"], "It was built as a substantial, single residence but has not been "
                                            "occupied since its construction in 2004.")
        self.assertEqual((found["ref"], found["page"], found["phrase"]),
                         ("22/00362/FULLS", 1, "has not been occupied since"))
        self.assertEqual(found["site"], "Golden Hill , Belbins, Romsey, SO51 0PE, ROMSEY EXTRA")
        self.assertEqual(found["proposal"], "Conversion of existing house and garage into 10 flats")

    def test_not_about_the_site_or_not_about_being_empty(self):
        for said in ("The adjoining property has been vacant since 2015.",
                     "The dwelling has not been occupied since 2001 by a person employed in agriculture.",
                     "No evidence has been given that the barn has been vacant for ten years.",
                     "It is unclear whether the unit has remained vacant.",
                     # the words of a policy or a rule, not of the site
                     "Proposals that result in the loss of shops and services will only be permitted where it can be "
                     "demonstrated that the premises has been vacant for 12 months.",
                     "Development not permitted by Class MA unless the building has been vacant for a continuous "
                     "period of at least 3 months.",
                     # gone already, or a commenter asking
                     "The site has been vacant since 2013 when the former buildings on site were demolished.",
                     "Why is a second bungalow needed when the existing bungalow has never been occupied?"):
            self.assertEqual(committees.find_statements([f"APPLICATION NO. 24/00001/FUL\nSITE 1 High St, AB1 2CD\n{said}"]),
                             [], said)

    def test_where_a_sentence_starts_and_stops(self):
        page = ("APPLICATION NO. 24/00003/FUL\nSITE 11-15 Strand Road, Derry\n\nPROPOSAL Hotel\n\n"
                "(ix) there is no adverse impact from litter; The former bank has been vacant since approx. 2010. "
                "It is listed.")
        [found] = committees.find_statements([page])
        self.assertEqual(found["sentence"], "The former bank has been vacant since approx. 2010.")
        self.assertEqual((found["site"], found["proposal"]), ("11-15 Strand Road, Derry", "Hotel"))
        lead = opendata._committee({**JudgingTests.ROW, **found, "phrase": found["phrase"]})
        self.assertEqual(lead["kind"], "bank")                     # what the officer says, before the proposal
        land = opendata._committee({**JudgingTests.ROW, "phrase": "vacant and derelict",
                                    "sentence": "The scheme brings a vacant and derelict site back into use."})
        self.assertEqual((land["kind"], land["weight"]), ("vacant land", 8))

    # Chichester sets the reference on a line of its own, and gives the site's grid reference.
    BURNES = [" \nParish: \nBosham \n \nWard: \nHarbour Villages \nBO/21/00620/FUL \n \n"
              "Proposal  Development comprising the demolition of existing B2 use shipyard \nbuildings and structures "
              "and the erection of 3no. replacement C3 \ndwellings with access, parking, landscaping and associated works. "
              "\n \nSite Burnes Shipyard  Westbrook Field Bosham PO18 8JN   \n \nMap Ref (E) 480388 (N) 104217 \n \n"
              "Applicant Paul Peta Properties Ltd Agent Mr Paul White \n \n",
              "2.0   The Site and Surroundings \n2.1  The application site, known as Burnes Shipyard is located to the "
              "north of Windward Road. \n2.2  The site is occupied by a variety of commercial buildings. The site has "
              "been redundant for more than twenty years, with the buildings in a poor state of repair with the site "
              "enclosed with safety fencing. Vehicle access to the site is via Windward Road."]

    def test_a_reference_on_its_own_line_and_a_grid_reference(self):
        found = committees.find_statements(self.BURNES)
        self.assertEqual({(f["ref"], f["site"], tuple(f["grid"])) for f in found},
                         {("BO/21/00620/FUL", "Burnes Shipyard Westbrook Field Bosham PO18 8JN", (480388, 104217))})
        self.assertEqual(found[0]["sentence"], "The site has been redundant for more than twenty years, with the "
                                               "buildings in a poor state of repair with the site enclosed with safety "
                                               "fencing.")
        lead = opendata._committee({**JudgingTests.ROW, **found[0], "council": "Chichester"})
        self.assertEqual((lead["kind"], lead["weight"]), ("shipyard", 22))      # what's there, not the new houses
        self.assertEqual(_condition_from(lead["evidence"]), "Abandoned")

    def test_a_paper_covering_several_applications(self):
        pages = ["APPLICATION NO. 24/00001/FUL\nSITE Mill House, Lower Road, AB1 2CD\nPROPOSAL Extension\n"
                 "The house is lived in.",
                 "APPLICATION NO. 24/00002/FUL\nSITE The Old Chapel, Church Lane, AB1 3EF\nPROPOSAL Conversion to flats\n"
                 "2.1 The chapel closed in 2009. Since then the building has stood empty and has fallen into disrepair, "
                 "e.g. No. 4 window is boarded up."]
        found = committees.find_statements(pages)
        self.assertEqual({f["ref"] for f in found}, {"24/00002/FUL"})
        self.assertEqual({f["page"] for f in found}, {2})
        self.assertEqual(found[0]["site"], "The Old Chapel, Church Lane, AB1 3EF")
        self.assertTrue(found[0]["sentence"].startswith("Since then the building has stood empty"))   # "e.g." and "No." go on

    def test_addresses(self):
        self.assertEqual(committees.tidy_address("Golden Hill , Belbins, Romsey, SO51 0PE, ROMSEY EXTRA"),
                         "Golden Hill, Belbins, Romsey")
        self.assertEqual(committees.tidy_address("THE OLD MILL, MILL LANE, ROMSEY"), "THE OLD MILL, MILL LANE, ROMSEY")
        self.assertEqual(committees.postcode_of("Golden Hill, Belbins, Romsey, so51 0pe"), "SO51 0PE")
        self.assertIsNone(committees.postcode_of("Land east of Church Lane"))


class Answer:
    def __init__(self, status=200, text="", content=b"", json_data=None, kind="text/html"):
        self.status_code, self.text, self.content, self._json = status, text, content, json_data
        self.headers = {"Content-Type": kind}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, size):
        for i in range(0, len(self.content), size):
            yield self.content[i:i + size]


class Web:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def get(self, url, params=None, headers=None, timeout=None, stream=False):
        self.calls.append(url)
        return self.answer(url)


class LocatingTests(unittest.TestCase):
    def setUp(self):
        self.web = Web(lambda url: Answer(json_data={"result": {"latitude": 51.0100, "longitude": -1.4890}})
                       if "/postcodes/" in url else Answer(404))

    def test_the_house_itself_when_openstreetmap_knows_it(self):
        with mock.patch("bandobuddy.geocode.search", return_value={"lat": 51.00963, "lng": -1.48995}) as found:
            point = committees.locate(self.web, "Golden Hill , Belbins, Romsey, SO51 0PE, ROMSEY EXTRA", "Test Valley")
        self.assertEqual(point, (51.00963, -1.48995))
        self.assertEqual(found.call_args[0][0], "Golden Hill, Belbins, Romsey, SO51 0PE")
        self.assertIn("https://api.postcodes.io/postcodes/SO51%200PE", self.web.calls)

    def test_the_postcode_when_the_address_match_is_somewhere_else(self):
        with mock.patch("bandobuddy.geocode.search", return_value={"lat": 52.5, "lng": -1.9}):
            self.assertEqual(committees.locate(self.web, "Golden Hill, Belbins, SO51 0PE", "Test Valley"), (51.01, -1.489))
        with mock.patch("bandobuddy.geocode.search", return_value=None):
            self.assertEqual(committees.locate(self.web, "Golden Hill, Belbins, SO51 0PE", "Test Valley"), (51.01, -1.489))
        with mock.patch("bandobuddy.geocode.search", return_value={"lat": 51.2, "lng": -1.5}) as found:   # no postcode
            self.assertEqual(committees.locate(self.web, "Land east of Church Lane, Awbridge", "Test Valley"), (51.2, -1.5))
        self.assertEqual(found.call_args[0][0], "Land east of Church Lane, Awbridge, Test Valley")


class JudgingTests(unittest.TestCase):
    ROW = {"council": "Test Valley", "ref": "22/00362/FULLS", "committee": "Southern Area Planning Committee",
           "meeting": (date.today() - timedelta(days=900)).isoformat(), "phrase": "has not been occupied since",
           "sentence": "It was built as a substantial, single residence but has not been occupied since its "
                       "construction in 2004.",
           "site": "Golden Hill , Belbins, Romsey, SO51 0PE, ROMSEY EXTRA",
           "proposal": "Conversion of existing house and garage into 10 flats",
           "url": "https://tv.example/mgConvert2PDF.aspx?ID=25119&ISATT=1#page=1", "lat": 51.0, "lng": -1.5}

    def test_golden_hill(self):
        lead = opendata._committee(self.ROW)
        self.assertEqual((lead["name"], lead["kind"], lead["weight"]), ("Golden Hill, Belbins", "house", 22))
        self.assertEqual(lead["ref"], "Test Valley:22/00362/FULLS")
        self.assertTrue(lead["evidence"].startswith(
            "Test Valley's planning report (22/00362/FULLS, Southern Area Planning Committee, "))
        self.assertIn('"It was built as a substantial, single residence but has not been occupied', lead["evidence"])
        self.assertEqual(_condition_from(lead["evidence"]), "Empty")
        self.assertEqual(lead["url"], self.ROW["url"])

    def test_how_much_it_counts(self):
        judge = lambda **change: opendata._committee({**self.ROW, **change})   # noqa: E731
        self.assertEqual(judge(meeting=(date.today() - timedelta(days=7 * 365)).isoformat())["weight"], 14)
        self.assertEqual(judge(meeting="2012-03-01")["weight"], 6)     # may well be done up, or gone, by now
        shop = judge(sentence="The ground floor shop unit has been vacant since 2019.", proposal="Change of use to cafe")
        self.assertEqual((shop["kind"], shop["weight"]), ("building", 8))
        chapel = judge(sentence="The chapel has stood empty since 2009 and is boarded up.", proposal="Conversion to flats")
        self.assertEqual((chapel["kind"], chapel["weight"], _condition_from(chapel["evidence"])), ("chapel", 22, "Abandoned"))
        shell = judge(phrase="partially constructed dwelling", sentence="The site contains a partially constructed dwelling.")
        self.assertEqual((shell["kind"], shell["weight"], _condition_from(shell["evidence"])),
                         ("unfinished house", 16, "Unfinished"))


class CrawlTests(unittest.TestCase):
    PAGE_2 = """<p>Results 11 to 11</p>
      <a  href="ieListDocuments.aspx?CId=160&amp;MID=500"  >02&#47;03&#47;2025 - Northern Area Planning Committee (11)</a>
      <a  href="mgConvert2PDF.aspx?ID=777&amp;ISATT=1"  >24_00002_FULLN NAPC Report 1 PDF 90 KB</a>"""

    def setUp(self):
        self.pdfs = {
            "ID=25119": tiny_pdf([["APPLICATION NO. 22/00362/FULLS", "SITE Golden Hill , Belbins, Romsey, SO51 0PE",
                                   "PROPOSAL Conversion of existing house and garage into 10 flats", "",
                                   "2.1 It was built as a substantial, single residence but has not been occupied "
                                   "since its construction in 2004."]]),
            "ID=39144": tiny_pdf([["APPLICATION NO. 25/02863/FULLS", "SITE 28 Sycamore Close, Romsey, SO51 5SB",
                                   "The neighbouring house has been vacant since 2020."]]),
            "ID=777": tiny_pdf([["APPLICATION NO. 24/00002/FULLN", "SITE The Old Chapel, Church Lane, Andover, SP10 1AA",
                                 "PROPOSAL Conversion to flats", "", "The chapel has stood empty since 2009."]]),
        }

        def answer(url):
            if url.startswith("https://down.example"):
                return Answer(403, "Forbidden")
            if "ieDocSearch.aspx" in url:
                return Answer(text=SearchTests.FORM.replace('value="436"', 'value="0436"'))
            if "ieSearchResults2" in url:
                if "CI=161" not in url:
                    return Answer(text="<p>No results found for your query</p>")
                return Answer(text=SearchTests.RESULTS if url.endswith("PG=1") else self.PAGE_2)
            for key, pdf in self.pdfs.items():
                if key in url:
                    return Answer(content=pdf, kind="application/pdf")
            return Answer(404)
        self.web = Web(answer)
        self.sites = (("Somewhere Down", "https://down.example"), ("Test Valley", "https://tv.example"))

    def run_crawl(self, memory, today=date(2026, 10, 7)):
        with mock.patch.object(committees, "locate", lambda session, site, council: (51.0, -1.5)):
            return list(committees.crawl(self.web, self.sites, lambda *a: None, memory=memory, gap_s=0, today=today))

    def test_reads_each_planning_report_once(self):
        path = Path(tempfile.mkdtemp()) / "committee_reports.json"
        rows = self.run_crawl(committees.Memory(path))
        self.assertEqual([(r["council"], r["ref"], r["meeting"]) for r in rows],
                         [("Test Valley", "22/00362/FULLS", "2023-06-13"), ("Test Valley", "24/00002/FULLN", "2025-03-02")])
        self.assertEqual(rows[0]["url"], "https://tv.example/mgConvert2PDF.aspx?ID=25119&ISATT=1#page=1")
        self.assertIn("has not been occupied since its construction in 2004", rows[0]["sentence"])
        reports = [u for u in self.web.calls if "ieSearchResults2" not in u and "ieDocSearch" not in u]
        self.assertFalse(any("Empty%20Homes" in u for u in reports))         # a Cabinet paper isn't read
        self.assertEqual(len(reports), 3)                                     # the three planning reports
        searches = [u for u in self.web.calls if "ieSearchResults2" in u and "tv.example" in u]
        # A search per planning committee (the policy group's papers aren't asked for), a page at a time.
        self.assertEqual([(u.split("CI=")[1].split("&")[0], u.rsplit("PG=", 1)[1]) for u in searches],
                         [("161", "1"), ("161", "2"), ("160", "1"), ("0436", "1")])
        self.assertIn("SD=01%2F01%2F2016", searches[0])                       # the first look goes back ten years
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["asked"], {"https://tv.example": "2026-10-07"})   # the council that turned us away isn't
        self.assertEqual(saved["committees"]["https://tv.example"]["ids"], ["161", "160", "0436"])
        self.assertEqual(len(saved["read"]), 3)

        self.web.calls.clear()
        again = self.run_crawl(committees.Memory(path), today=date(2026, 10, 14))
        self.assertEqual(again, [])
        asked = [u for u in self.web.calls if "tv.example" in u]
        self.assertTrue(asked and all("ieSearchResults2" in u for u in asked))   # nothing read twice, nor the form
        self.assertIn("SD=02%2F09%2F2026", [u for u in self.web.calls if "tv.example" in u][0])   # a week on, from 5 weeks back

    def test_reads_everything_again_when_the_reader_changes(self):
        path = Path(tempfile.mkdtemp()) / "committee_reports.json"
        path.write_text(json.dumps({"read": {"https://tv.example/x.pdf": "2026-10-01"},
                                    "asked": {"https://tv.example": "2026-10-01"},
                                    "committees": {"https://tv.example": {"ids": ["161"], "at": "2026-10-01"}}}))
        memory = committees.Memory(path)          # written before the reader had a version
        self.assertFalse(memory.read("https://tv.example/x.pdf"))
        self.assertIsNone(memory.asked("https://tv.example"))
        self.assertEqual(memory.committees("https://tv.example", date(2026, 10, 7)), ["161"])   # still known
        memory.mark_read("https://tv.example/x.pdf")
        memory.save()
        self.assertTrue(committees.Memory(path).read("https://tv.example/x.pdf"))   # same reader: kept

    def test_placed_by_the_reports_own_grid_reference(self):
        pdf = tiny_pdf([line for line in page.split("\n") if line.strip()] for page in ReadingTests.BURNES)
        web = Web(lambda url: Answer(content=pdf, kind="application/pdf"))
        hit = {"url": "https://chi.example/mgConvert2PDF.aspx?ID=22451&ISATT=1", "item": "", "committee": "Planning Committee",
               "meeting": "2022-01-12"}
        with mock.patch.object(committees, "locate", side_effect=AssertionError("no need to look the address up")):
            [row] = list(committees._read_report(web, "Chichester", hit, {}))
        self.assertAlmostEqual(row["lat"], 50.83191, places=4)
        self.assertAlmostEqual(row["lng"], -0.85988, places=4)
        self.assertEqual(row["ref"], "BO/21/00620/FUL")

    def test_several_councils_at_once_and_stopping_them_all(self):
        import threading
        together = threading.Barrier(3, timeout=5)      # opens only when three councils are being asked at once

        def answer(url):
            if "ieDocSearch.aspx" in url:
                together.wait()
                return Answer(text=SearchTests.FORM)
            return Answer(text="<p>No results found for your query</p>")
        web = Web(answer)
        sites = tuple((f"Council {n}", f"https://c{n}.example") for n in range(3))
        self.assertEqual(list(committees.crawl(web, sites, lambda *a: None, gap_s=0, at_once=3)), [])
        self.assertEqual(sum(1 for u in web.calls if "ieDocSearch" in u), 3)

        stop = threading.Event()
        slow = Web(lambda url: (stop.set(), Answer(text=SearchTests.FORM))[1])   # cancelled while it's asking
        with self.assertRaises(committees.Cancelled):
            list(committees.crawl(slow, sites, lambda *a: None, cancel=stop, gap_s=0.5, at_once=3))

    def test_a_bug_isnt_mistaken_for_a_council_being_down(self):
        broken = Web(lambda url: Answer(text=None))      # parsing fails: that's ours to fix, so it stops
        with self.assertRaises(TypeError):
            list(committees.crawl(broken, (("Council", "https://c.example"),), lambda *a: None, gap_s=0))

    def test_off_unless_switched_on_and_given_the_data_folder(self):
        self.assertFalse(opendata.DATASETS["committees"].enabled())
        tmp = Path(tempfile.mkdtemp())
        store = Store(tmp / "t.db")
        given = []

        def fetch(session, progress, cancel=None, data_dir=None):
            given.append(data_dir)
            yield {**JudgingTests.ROW}
        dataset = opendata.Dataset(**{**opendata.DATASETS["committees"].__dict__, "fetch": fetch})
        with mock.patch.dict(opendata.DATASETS, {"committees": dataset}):
            Updater(store, tmp, session_factory=lambda: None, log=lambda m: None, sources=("committees",)).run("committees")
        self.assertEqual(given, [tmp])
        [kept] = store.active_od()
        self.assertEqual((kept["ref"], kept["name"]), ("Test Valley:22/00362/FULLS", "Golden Hill, Belbins"))


if __name__ == "__main__":
    unittest.main()
