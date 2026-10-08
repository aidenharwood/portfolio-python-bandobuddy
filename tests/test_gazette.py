"""The Crown's disclaimers of dissolved companies' land, from The Gazette."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bandobuddy import gazette, opendata
from bandobuddy.scoring import _condition_from

# Two real notices, as their pages read (2026 and 2011).
MAGDALEN = ("NOTICE OF DISCLAIMER UNDER SECTION 1013 OF THE COMPANIES ACT 2006 DISCLAIMER OF WHOLE OF THE PROPERTY "
            "T S ref: COMP26#8744/1 1 In this notice the following shall apply: Company Name: FREEHOLDERS 37 MAGDALEN "
            "ROAD LIMITED Company Number: 09893627 Interest: Freehold Title number: ESX70752 Property: The Property "
            "situated at 37 Magdalen Road, St. Leonards-On-Sea (TN37 6ET) being the land comprised in the above "
            "mentioned title Treasury Solicitor: The Solicitor for the Affairs of Her Majesty's Treasury of 1 Ruskin "
            "Square, Croydon CR0 2WF (DX 325801 Croydon 51). 2 In pursuance of the powers granted by Section 1013 of "
            "the Companies Act 2006, the Treasury Solicitor as nominee for the Crown (in whom the property and rights "
            "of the Company vested when the Company was dissolved) hereby disclaims the Crown's title (if any) in the "
            "property, the vesting of the property having come to his notice on 13 April 2026. Assistant Treasury "
            "Solicitor 5 October 2026")
HOLMES_CHAPEL = ("NOTICE OF DISCLAIMER UNDER SECTION 1013 OF THE COMPANIES ACT 2006 DISCLAIMER OF WHOLE OF THE PROPERTY "
                 "T S Ref: BV21112506/1/GT. 1. In this Notice the following shall apply: Company Name: HARRIS (HOLMES "
                 "CHAPEL) LIMITED . Company Number: 00851903. Interest: Leasehold. Lease: Lease dated 1 January 2010 and "
                 "made between Aus-Bore Estate Limited (1) and Harris (Holmes Chapel) Limited (2). Property: The Property "
                 "known as Unit 2, Manor Business Park, Manor Lane, Holmes Chapel CW4 8AB being the land comprised in and "
                 "demised by the above mentioned Lease. Treasury Solicitor: The Solicitor for the Affairs of Her "
                 "Majesty's Treasury, of One Kemble Street, London WC2B 4TS (DX 123240 Kingsway). 2. In pursuance of the "
                 "powers granted by section 1013 of the Companies Act 2006, the Treasury Solicitor as nominee for the Crown "
                 "(in whom the property and rights of the Company vested when the Company was dissolved) hereby disclaims "
                 "the Crown's title (if any) in the Property, the vesting of the Property having come to his notice on 25 "
                 "October 2011. Assistant Treasury Solicitor 10 November 2011")


def chapel_text(**over):
    fields = {"name": "MONUMENTAL TRUST LIMITED", "number": "04512345", "interest": "Freehold", "title": "LA123456",
              "dissolved": "Dissolution Date: 27 January 2025 ",
              "property": "The Property known as The Old Chapel, Chapel Street, Burnley (BB11 1AB)", **over}
    return (f"NOTICE OF DISCLAIMER UNDER S.1013 OF THE COMPANIES ACT 2006 1. In this Notice the following shall apply: "
            f"Company Name: {fields['name']} Company Number: {fields['number']} {fields['dissolved']}"
            f"Interest: {fields['interest']} Title number: {fields['title']} Property: {fields['property']} being the "
            f"land comprised in the above mentioned title Treasury Solicitor: The Solicitor for the Affairs of His "
            f"Majesty's Treasury of 1 Ruskin Square, Croydon CR0 2WF. 2. In pursuance ... Assistant Treasury Solicitor "
            f"2 May 2026")


def page(text):
    return (f"<html><head><style>p{{}}</style></head><body><div class='content'><p>{text}</p></div>"
            f"<ul><li>Actions</li><li>Save notice to My Gazette</li></ul></body></html>")


class Resp:
    def __init__(self, data=None, text="", status=200):
        self._data, self.text, self.status_code = data, text, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class Session:
    """The Gazette's search, its notice pages, and postcodes.io."""

    def __init__(self, notices, postcodes):
        self.notices, self.postcodes, self.calls = notices, postcodes, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        if url == gazette.SEARCH_URL:
            ids = list(self.notices)
            start = (params["results-page"] - 1) * params["results-page-size"]
            chunk = ids[start:start + params["results-page-size"]]
            return Resp({"f:total": str(len(ids)), "entry": [{"id": f"https://www.thegazette.co.uk/id/notice/{i}",
                                                               "published": "2026-05-04T09:00:00"} for i in chunk]})
        if url.startswith("https://www.thegazette.co.uk/notice/"):
            return Resp(text=page(self.notices[url.rsplit("/", 1)[1]]))
        pc = url.rsplit("/", 1)[1].replace("%20", " ")
        if "/postcodes/" in url and pc in self.postcodes:
            lat, lng = self.postcodes[pc]
            return Resp({"result": {"latitude": lat, "longitude": lng}})
        return Resp(status=404)


def nothing(*_args):
    pass


class DisclaimerTests(unittest.TestCase):
    def test_reading_a_notice(self):
        self.assertEqual(gazette.notice_text(page(MAGDALEN)), MAGDALEN)
        new = gazette.parse_notice(MAGDALEN)
        self.assertEqual({k: new[k] for k in ("company", "number", "interest", "title", "postcode", "signed")},
                         {"company": "FREEHOLDERS 37 MAGDALEN ROAD LIMITED", "number": "09893627", "interest": "Freehold",
                          "title": "ESX70752", "postcode": "TN37 6ET", "signed": "2026-10-05"})
        self.assertEqual(new["property"], "37 Magdalen Road, St. Leonards-On-Sea (TN37 6ET)")
        old = gazette.parse_notice(HOLMES_CHAPEL)     # 2011: full stops after each field, and a lease
        self.assertEqual((old["company"], old["number"], old["interest"], old["postcode"], old["signed"]),
                         ("HARRIS (HOLMES CHAPEL) LIMITED", "00851903", "Leasehold", "CW4 8AB", "2011-11-10"))
        self.assertEqual(old["property"], "Unit 2, Manor Business Park, Manor Lane, Holmes Chapel CW4 8AB")
        self.assertEqual(gazette.parse_notice(chapel_text())["dissolved"], "2025-01-27")
        self.assertIsNone(gazette.parse_notice(""))
        # The fields in another order, signed by a Duchy's solicitor (2026).
        duchy = gazette.parse_notice(
            "NOTICE OF DISCLAIMER UNDER S. 1013 OF THE COMPANIES ACT 2006 1. In this Notice the following shall apply: "
            "Company Name: Stiled Holdings Ltd Company Number: 14554067 Dissolution Date: 13 May 2026 Property: Unit 1 "
            "Rampart Court Retail Park Rampart Way Telford Title Number: SL260527 Interest: Leasehold Lease: Dated 12 "
            "June 2019 and made between Sheet Anchor Evolve Limited (1) and Tile Choice Limited (2) The Duchy "
            "Solicitor: THE SOLICITOR FOR THE AFFAIRS OF THE DUCHY OF CORNWALL ... 14 July 2026")
        self.assertEqual((duchy["property"], duchy["title"], duchy["interest"], duchy["signed"]),
                         ("Unit 1 Rampart Court Retail Park Rampart Way Telford", "SL260527", "Leasehold", "2026-07-14"))
        # However the property's introduced, it's the property that's kept.
        for said, kept in (('"Lease of public house at Danny Macs Tavern, 1 Oldham Street, Liverpool L1 2SU"',
                            "Danny Macs Tavern, 1 Oldham Street, Liverpool L1 2SU"),
                           ("Interest in Lease relating to The premises known as 21-23 Church Street, Brighton",
                            "21-23 Church Street, Brighton"),
                           ("The Property described as land on the east side of Mill Road, Shiplake",
                            "land on the east side of Mill Road, Shiplake")):
            self.assertEqual(gazette.parse_notice(chapel_text(property=said))["property"], kept)

    def test_what_counts(self):
        judge = lambda text, **row: opendata._disclaimer({**gazette.parse_notice(text), "id": "5143452",   # noqa
                                                          "url": "https://www.thegazette.co.uk/notice/5143452", **row})
        chapel = judge(chapel_text())
        self.assertEqual((chapel["name"], chapel["kind"], chapel["weight"]), ("The Old Chapel", "chapel", 20))
        self.assertIn("The Crown disclaimed it on 2026-05-02: Monumental Trust Limited, which owned the freehold, was "
                      "dissolved in 2025 and nobody took it on, so it's ownerless (The Gazette, title LA123456)",
                      chapel["evidence"])
        self.assertEqual(_condition_from(chapel["evidence"]), "Ownerless")
        self.assertEqual(chapel["dates"], [("dissolved", "2025-01-27"), ("disclaimed", "2026-05-02")])
        self.assertEqual(chapel["aliases"], ["Monumental Trust Limited (04512345)"])
        pub = judge(chapel_text(property="The Red Lion Public House, 3 High Street, Tring (HP23 5AA)"))
        self.assertEqual((pub["name"], pub["kind"]), ("The Red Lion Public House", "pub"))
        lease = judge(chapel_text(interest="Leasehold"))
        self.assertEqual(lease["weight"], 12)                 # the lease ends; the landlord has it back
        self.assertNotEqual(_condition_from(lease["evidence"]), "Ownerless")
        for not_one in ("37 Magdalen Road, St. Leonards-On-Sea (TN37 6ET)",      # a house, flats most likely
                        "12 Church Street, Burnley (BB11 1AB)",                  # the street, not the building
                        "Flat 2, The Old Chapel, Chapel Street, Burnley (BB11 1AB)",
                        "Land adjoining The Old Mill, Mill Lane, Hebden Bridge (HX7 8AB)",
                        "Garage 4, rear of Church Hall, Hall Road, Leeds (LS1 1AA)",
                        "26 Swainby Road, Trimdon, Trimdon Station (TS29 6JY)",    # the village it's in
                        "18 Barn Rise, Wembley, HA9 9NF",
                        "St. Nicholas Place Service Station, St Nicholas Place, Leicester (LE1 5LB)"):
            self.assertIsNone(judge(chapel_text(property=not_one)), not_one)
        self.assertIsNone(opendata._disclaimer({**gazette.parse_notice(HOLMES_CHAPEL)}))   # chapel's in the name only

    def test_reading_them_ten_seconds_apart_and_once(self):
        notices = {"5": chapel_text(), "4": MAGDALEN, "3": chapel_text(property="The Old Mill, Mill Lane (HX7 8AB)"),
                   "2": HOLMES_CHAPEL, "1": chapel_text(property="Former Methodist Church, Leek (ST13 9ZZ)")}
        session = Session(notices, {"BB11 1AB": (53.79, -2.24), "TN37 6ET": (50.86, 0.56), "HX7 8AB": (53.74, -2.01),
                                    "CW4 8AB": (53.2, -2.36)})      # ST13 9ZZ: no such postcode
        tmp = Path(tempfile.mkdtemp())
        waits = []
        fetch = gazette.Disclaimers(page_size=2, per_run=3)
        with mock.patch.object(gazette.Disclaimers, "_wait", lambda self, cancel, s: waits.append(s)):
            rows = list(fetch(session, nothing, data_dir=tmp))
            gazette_asks = [c for c in session.calls if "thegazette" in c[0]]
            self.assertEqual(waits, [10] * len(gazette_asks))     # ten seconds before every request to the Gazette
            listing = [c[1] for c in gazette_asks if c[0] == gazette.SEARCH_URL]
            self.assertEqual([p["results-page"] for p in listing], [1, 2, 3])
            self.assertEqual(listing[0]["noticetypes"], "2603")
            self.assertTrue(listing[0]["text"].startswith('Freehold AND (chapel OR church OR mill OR inn OR "public house"'))
            self.assertEqual([r["id"] for r in rows], ["5", "4", "3"])        # three a run, the newest first
            memory = json.loads((tmp / gazette.MEMORY).read_text(encoding="utf-8"))
            self.assertEqual(sorted(memory["notices"]), ["3", "4", "5"])
            self.assertEqual(memory["notices"]["4"]["text"], MAGDALEN)     # its words: read again however it's read
            # Next run: what was read comes back without asking, and the rest is read.
            session.calls.clear()
            rows = list(fetch(session, nothing, data_dir=tmp))
        read = [c[0].rsplit("/", 1)[1] for c in session.calls if c[0].startswith(gazette.NOTICE_URL[:-2])]
        self.assertEqual(read, ["2", "1"])
        self.assertEqual(sorted(r["id"] for r in rows), ["2", "3", "4", "5"])   # 1's postcode is nowhere: not placed
        self.assertEqual(next(r for r in rows if r["id"] == "5")["lat"], 53.79)
        items = [opendata._disclaimer(r) for r in rows]
        self.assertEqual(sorted(i["name"] for i in items if i), ["The Old Chapel", "The Old Mill"])

    def test_placed_by_its_address_with_no_postcode(self):
        # A building by its name is looked for with Nominatim; land on the side of a road isn't worth the asking.
        found = {"lat": 52.8, "lng": 1.4}
        with mock.patch.object(gazette.geocode, "search", return_value=found) as search:
            places = {}
            where = gazette.Disclaimers._place(None, {"property": "Ebridge Mill, Happisburgh Road, North Walsham"}, places)
            self.assertEqual(where, (52.8, 1.4))
            gazette.Disclaimers._place(None, {"property": "Ebridge Mill, Happisburgh Road, North Walsham"}, places)
            self.assertIsNone(gazette.Disclaimers._place(None, {"property": "land on the south east side of "
                                                                            "Happisburgh Road, North Walsham"}, places))
        self.assertEqual(search.call_count, 1)                   # once, then remembered

    def test_on_by_default(self):
        self.assertTrue(opendata.DATASETS["disclaimers"].enabled())
        from bandobuddy.updater import SOURCES
        self.assertIn("disclaimers", SOURCES)


if __name__ == "__main__":
    unittest.main()
