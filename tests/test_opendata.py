import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bandobuddy import opendata
from bandobuddy.scoring import _condition_from
from bandobuddy.sites import build_sites
from bandobuddy.store import Store
from bandobuddy.updater import Updater


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeService:
    """Answers the handful of requests the fetchers make, and remembers them."""

    def __init__(self, pages):
        self.pages = pages          # list of payloads, in the order they're asked for
        self.calls: list[tuple[str, dict]] = []

    def _next(self, url, params):
        self.calls.append((url, dict(params)))
        return FakeResp(self.pages.pop(0) if self.pages else {})

    def get(self, url, params=None, headers=None, timeout=None):
        return self._next(url, params or {})

    def post(self, url, data=None, headers=None, timeout=None):
        return self._next(url, data or {})


def points(*rows):
    return {"features": [{"attributes": a, "geometry": {"x": lng, "y": lat}} for a, lat, lng in rows]}


def nothing(*_args):
    pass


class FetcherTests(unittest.TestCase):
    def test_arcgis_pages_until_a_short_page(self):
        layer = opendata.ArcGIS("https://x/0", where="A=1", fields="NAME", batch=2)
        service = FakeService([
            {"count": 3},
            points(({"NAME": "one"}, 51.0, -1.0), ({"NAME": "two"}, 51.1, -1.1)),
            points(({"NAME": "three"}, 51.2, -1.2)),
        ])
        seen = []
        rows = list(layer(service, lambda stage, done, total: seen.append((done, total))))
        self.assertEqual([r["NAME"] for r in rows], ["one", "two", "three"])
        self.assertEqual(rows[0]["lat"], 51.0)
        self.assertEqual(seen[-1], (3, 3))
        self.assertEqual([c[1].get("resultOffset") for c in service.calls[1:]], [0, 2])

    def test_arcgis_by_ids_asks_per_term_then_fetches_batches(self):
        layer = opendata.ArcGISByIds("https://x/0", ["T LIKE '%A%'", "T LIKE '%B%'"], batch=2)
        service = FakeService([
            {"objectIds": [5, 9]},
            {"objectIds": [9, 1]},          # the overlap is only fetched once
            points(({"ID": 1}, 51.0, -1.0), ({"ID": 5}, 51.1, -1.1)),
            {"features": [{"attributes": {"ID": 9}, "geometry": {"points": [[-1.2, 51.2]]}}]},
        ])
        rows = list(layer(service, nothing))
        self.assertEqual([r["ID"] for r in rows], [1, 5, 9])
        self.assertEqual(rows[2]["lng"], -1.2)  # a multipoint record (Canmore) still has a place
        self.assertEqual([c[1].get("objectIds") for c in service.calls[2:]], ["1,5", "9"])

    def test_wfs_and_planning_data_paging(self):
        wfs = opendata.WFS("https://wales/wfs", "layer", batch=2)
        service = FakeService([
            {"totalFeatures": 3, "features": [{"properties": {"nprn": "1", "lat": "51.5", "long": "-3.1"}},
                                              {"properties": {"nprn": "2", "lat": "#VALUE!", "long": ""}}]},
            {"features": [{"properties": {"nprn": "3", "lat": "51.7", "long": "-3.3"}}]},
        ])
        rows = list(wfs(service, nothing))
        self.assertEqual([r["nprn"] for r in rows], ["1", "3"])  # one had no position
        self.assertEqual([c[1]["startIndex"] for c in service.calls], [0, 2])
        only = opendata.WFS("https://wales/wfs", "layer", fields="nprn,lat,long")
        service = FakeService([{"features": []}])
        list(only(service, nothing))
        self.assertEqual(service.calls[0][1]["propertyName"], "nprn,lat,long")   # just the columns read

    def test_old_os_map_labels(self):
        # GB1900's labels, asked for by what they say, with the position under its own names.
        fetch = opendata.DATASETS["old_maps"].fetch
        label = lambda pin, text, lat, lng: {"properties": {"pin_id": pin, "final_text": text, "latitude": lat,  # noqa
                                                            "longitude": lng, "parish": "BRADFORD ON AVON"}}
        service = FakeService([
            {"numberMatched": 1, "features": [label("p1", "Butserhill Lime Works", 50.97931, -0.96454)]},
            # Then quarries and shafts: Bethel Quarry by Frome Road, with air shafts across the hill; a quarry with
            # none near is a hole in the ground.
            {"numberMatched": 7, "features": [label("p2", "Quarry", 51.339, -2.2501), label("p3", "Air Shaft", 51.339, -2.2559),
                                              label("p4", "Air Shaft", 51.3392, -2.2584), label("p5", "Quarry", 51.36, -2.30),
                                              # ...and one in a lead field, where the air shafts are the mine's
                                              label("p6", "Quarry", 54.7, -2.2), label("p7", "Air Shaft", 54.701, -2.2),
                                              label("p8", "Old Shafts", 54.7, -2.203)]},
        ])
        rows = list(fetch(service, nothing))
        asked = service.calls[0][1]
        self.assertEqual((asked["sortBy"], asked["typeName"]), ("pin_id", "nls:gb1900_21_December"))
        self.assertIn("final_text_lower LIKE '%kiln%'", asked["CQL_FILTER"])
        self.assertIn("'quarry'", service.calls[1][1]["CQL_FILTER"])
        self.assertEqual((rows[0]["lat"], rows[0]["lng"]), (50.97931, -0.96454))
        self.assertEqual([(r["pin_id"], r.get("air_shafts")) for r in rows], [("p1", None), ("p2", 1)])  # 400 m; 580 m
        bethel = opendata._old_map(rows[1])
        self.assertEqual((bethel["name"], bethel["kind"], bethel["weight"]),
                         ("Underground quarry, Bradford On Avon", "underground quarry", 20))
        self.assertIn('marks "Quarry" here, with 1 air shaft within 500 m: worked underground', bethel["evidence"])
        self.assertEqual(_condition_from(bethel["evidence"]), "Underground")
        judge = lambda text: opendata._old_map({"pin_id": "1", "final_text": text, "lat": 51.0, "lng": -1.0})  # noqa
        butser = judge("Butserhill Lime Works")         # at work then, but lime works leave lasting remains
        self.assertEqual((butser["name"], butser["kind"], butser["weight"]), ("Butserhill Lime Works", "lime works", 6))
        self.assertIn('six-inch map of 1888-1913 marks "Butserhill Lime Works" here', butser["evidence"])
        self.assertEqual(butser["dates"], [("on the map by", "1913")])
        kiln = judge("Old Limekiln")
        self.assertEqual((kiln["kind"], kiln["weight"], _condition_from(kiln["evidence"])), ("lime kiln", 10, "Disused"))
        chapel = judge("Chapel (In Ruins)")
        self.assertEqual((chapel["name"], chapel["kind"], _condition_from(chapel["evidence"])), ("Chapel", "chapel", "Ruin"))
        self.assertEqual((judge("Corn Mill (Disused)")["kind"], judge("Engine House (Pumping)")["kind"],
                          judge("Level (Disused)")["kind"], judge("brick works (disused)")["name"]),
                         ("mill", "engine house", "adit", "Brick Works"))
        for not_one in ("Gas Works", "Brick Works", "Corn Mill",            # at work in 1900: long gone, or still going
                        "Kiln Lane", "Limekiln Wood", "Old Mill Pond", "Old School House",   # named after one
                        "Quarry (Disused)", "Burial Ground (Disused)", "Allt a' Chaoruinn", "Earthworks"):
            self.assertIsNone(judge(not_one), not_one)
        # The transcription's finished: asked for again each season, not each week.
        from datetime import datetime, timedelta, timezone
        tmp = Path(tempfile.mkdtemp())
        up = Updater(Store(tmp / "t.db"), tmp, session_factory=lambda: None, log=lambda m: None, sources=("old_maps",))
        crawl = up.store.start_crawl("old_maps")
        up.store.set_crawl_status(crawl["id"], "done")
        for days, due in ((30, []), (91, ["old_maps"])):
            when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
            with up.store.connect() as db:
                db.execute("UPDATE crawls SET finished_at = ?", (when,))
            self.assertEqual(up.due_sources(), due)

    def test_planning_data_in_one_file(self):
        # The whole register as one CSV, read as it arrives; entries taken off the register are left out.
        csv_text = ("\ufeffentity,point,end-date,site-address\r\n"
                    "1,POINT(-2.5 51.3),,1 High Street\r\n"
                    "2,,,No position\r\n"
                    "3,POINT(-2.6 51.4),,Mill Lane\r\n"
                    "4,POINT(-2.7 51.5),2024-01-01,Taken off\r\n")

        class Stream:
            def get(self, url, headers=None, timeout=None, stream=False):
                self.url = url
                resp = mock.Mock(status_code=200)
                resp.raise_for_status = lambda: None
                body = csv_text.encode("utf-8")
                resp.iter_content = lambda size: (body[i:i + 7] for i in range(0, len(body), 7))
                return resp
        session = Stream()
        rows = list(opendata.PlanningData("brownfield-land")(session, nothing))
        self.assertEqual([(r["entity"], r["lat"], r["lng"]) for r in rows], [("1", 51.3, -2.5), ("3", 51.4, -2.6)])
        self.assertTrue(session.url.endswith("/dataset/brownfield-land.csv"))


class JudgingTests(unittest.TestCase):
    def test_what_counts_as_a_lead(self):
        self.assertEqual(opendata.judge_record("OBSERVATION POST")[1], "observation post")
        self.assertEqual(opendata.judge_record("PILLBOX")[1], "military structure")
        self.assertEqual(opendata.judge_record("COLLIERY")[1], "old workings")
        self.assertIsNone(opendata.judge_record("EARTHWORK"))     # not ironworks
        self.assertIsNone(opendata.judge_record("CHAMBERED CAIRN"))
        self.assertIsNone(opendata.judge_record("CHURCH"))        # a register entry says nothing about use
        # Already a wreck by its own description: worth a little more.
        self.assertGreater(opendata.judge_record("QUARRY (DISUSED)")[0], opendata.judge_record("QUARRY")[0])
        # But gone is gone: nothing to find, so not a lead at all.
        self.assertIsNone(opendata.judge_record("MILL (SITE OF)"))
        self.assertIsNone(opendata.judge_record("BRICKWORKS", "Glasgow, Garrowhill Brickworks (Site Of)"))
        self.assertIsNone(opendata.judge_record("MILL", "Site of Trepuscodling Mill, Great House"))
        self.assertIsNone(opendata.judge_record("COLLIERY (DEMOLISHED)"))
        # "Site" alone is a place, not an absence.
        self.assertIsNotNone(opendata.judge_record("MINE", "Wanlockhead, Bay Mine Site"))
        self.assertIsNotNone(opendata.judge_record("BATTERY", "Burrow Head, Anti-Aircraft Battery And Domestic Site"))

    def test_names_lead_with_the_thing(self):
        self.assertEqual(opendata.tidy_name("Coetgae, Abertillery, Former Opencast Mine"),
                         "Former Opencast Mine, Abertillery")
        self.assertEqual(opendata.tidy_name("Lluest Colliery [Disused] , Pont-y-Rhyl"), "Lluest Colliery, Pont-y-Rhyl")
        self.assertEqual(opendata.tidy_name("Disused Quarry, Cwm Llwyd,"), "Disused Quarry, Cwm Llwyd")
        self.assertEqual(opendata.tidy_name("LOCH OF BRECK, NORSE MILL II", shouting=True), "Norse Mill II, Loch of Breck")
        self.assertEqual(opendata.tidy_name("ST MARY'S ROC POST", shouting=True), "St Mary's ROC Post")
        self.assertEqual(opendata.tidy_name("PONT-Y-RHYL, FETLAR", shouting=True), "Pont-y-Rhyl, Fetlar")

    def test_the_register_describes_the_place_not_its_name(self):
        # "Manod Quarries, Track II" is a trackway, whatever its name says.
        self.assertIsNone(opendata.judge_segments("TRACKWAY", "Manod Granite Quarries, Track II"))
        verdict = opendata.judge_segments("FARMSTEAD (18TH CENTURY), OBSERVATION POST (20TH CENTURY)", "Vord Hill")
        self.assertEqual(verdict[1:], ("observation post", "observation post"))
        # The register's brackets count: Canmore marks a vanished building "(SITE OF)".
        self.assertIsNone(opendata.judge_segments("WINDMILL (SITE OF)", "Kirkwall"))

    def test_records_become_items(self):
        har = opendata.DATASETS["heritage_at_risk"].judge(
            {"HeritageCa": "Listed Building", "List_Entry": 123, "EntryName": "Old Mill", "URL": "https://he/1"})
        self.assertEqual((har["ref"], har["name"], har["weight"]), ("123", "Old Mill", 25))
        self.assertIn("Heritage at Risk", har["evidence"])
        self.assertIsNone(opendata.DATASETS["heritage_at_risk"].judge({"HeritageCa": "Conservation Area"}))

        canmore = opendata.DATASETS["canmore"].judge(
            {"CANMOREID": 9218, "NMRSNAME": "ACKERGILL, OBSERVATION TOWER",
             "SITETYPE": "OBSERVATION POST (20TH CENTURY)", "URL": "https://trove/9218"})
        self.assertEqual(canmore["ref"], "9218")
        self.assertEqual(canmore["name"], "Observation Tower, Ackergill")  # the thing first, then where
        self.assertEqual(canmore["evidence"], "Canmore records an observation post here")

        coflein = opendata.DATASETS["coflein"].judge(
            {"nprn": "41054", "name": "Pandy Bach", "site_type": "WOOLLEN MILL; MILL", "url": "https://coflein/41054"})
        self.assertEqual(coflein["evidence"], "Coflein records a woollen mill here")

        brownfield = opendata.DATASETS["brownfield"].judge(
            {"entity": 7, "site-address": "Rosemary Avenue, Newton Abbot", "notes": "Cleared, awaiting permission"})
        self.assertEqual((brownfield["ref"], brownfield["name"], brownfield["weight"]), ("7", "Rosemary Avenue", 10))
        # Sites stay on the register after they're built: those aren't leads.
        self.assertIsNone(opendata.DATASETS["brownfield"].judge({"entity": 8, "notes": "Site completed"}))

    def test_collect_keeps_only_judged_records(self):
        dataset = opendata.Dataset(
            key="test", label="Test", licence="OGL", attribution="x", home="https://x",
            fetch=lambda session, progress, cancel=None: iter([
                {"SITETYPE": "PILLBOX", "NMRSNAME": "", "CANMOREID": 1, "lat": 51.0, "lng": -1.0},
                {"SITETYPE": "CHAMBERED CAIRN", "NMRSNAME": "Old Cairn", "CANMOREID": 2, "lat": 51.1, "lng": -1.1},
            ]),
            judge=opendata.DATASETS["canmore"].judge,
        )
        items = list(opendata.collect(dataset, None, nothing))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Unnamed military structure")  # nameless records still get one
        self.assertEqual(items[0]["dataset"], "test")


def od(ref, lat, lng, name="Alderbury ROC Post", weight=25, kind="observation post",
       evidence="Canmore records an observation post here", dataset="canmore"):
    return {"dataset": dataset, "ref": ref, "name": name, "lat": lat, "lng": lng, "kind": kind,
            "evidence": evidence, "weight": weight, "url": f"https://register/{ref}"}


class StoreAndSitesTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "t.db")
        self.now = "2026-01-01T00:00:00.000000+00:00"

    def test_records_round_trip_and_can_go_away(self):
        self.store.upsert_od([od("1", 51.5, -0.12), od("2", 52.0, -1.0, dataset="coflein")], self.now)
        self.assertEqual(len(self.store.active_od()), 2)
        self.assertEqual([r["ref"] for r in self.store.active_od("coflein")], ["2"])
        self.assertEqual(self.store.count_items()["canmore"], 1)
        # A later crawl no longer lists the first one.
        later = "2026-02-01T00:00:00.000000+00:00"
        self.store.upsert_od([od("2", 52.0, -1.0, dataset="coflein")], later)
        self.assertEqual(self.store.mark_gone("coflein", later), 0)
        self.assertEqual(self.store.mark_gone("canmore", later), 1)
        self.assertEqual([r["ref"] for r in self.store.active_od()], ["2"])

    def test_a_register_record_joins_the_site_already_there(self):
        self.store.upsert_osm([{"osm_id": "node/1", "lat": 51.5, "lng": -0.12,
                                "tags": {"military": "bunker", "name": "Alderbury ROC Post"}}], self.now)
        self.store.upsert_od([od("1", 51.5001, -0.12)], self.now)  # ~11 m away
        build_sites(self.store)
        sites = self.store.full_sites(min_score=0)
        self.assertEqual(len(sites), 1)
        site = sites[0]
        self.assertEqual(site["sources"], "osm+canmore")
        self.assertEqual(site["category"], "bunkers")
        self.assertEqual(site["condition"], "Old military")
        self.assertIn("Canmore records an observation post here", site["reasons"])
        self.assertEqual(len(site["detail"]["open"]), 1)

    def test_a_quarry_worked_underground_is_a_best_spot(self):
        # Gripwood Quarry: Wikidata says "old quarry" (a hole in the ground, for best spots), the 1900 map shows its
        # air shafts. Galleries to walk, so it's one of the best spots.
        self.store.upsert_wd([{"qid": "Q4249906", "label": "Gripwood Quarry", "lat": 51.3415, "lng": -2.25693,
                               "types": ["protected area", "quarry"], "states": [], "ended": None,
                               "wiki": None}], self.now)
        best = lambda: [s["name"] for s in self.store.full_sites(min_score=0, best=True)]   # noqa: E731
        build_sites(self.store)
        self.assertEqual(best(), [])
        self.store.upsert_od([od("p1", 51.3413, -2.257, name="Underground quarry, Bradford On Avon", weight=20,
                                 kind="underground quarry", dataset="old_maps",
                                 evidence='The Ordnance Survey six-inch map of 1888-1913 marks "Quarry" here, with 2 '
                                          'air shafts within 500 m: worked underground')], self.now)
        build_sites(self.store)
        site = self.store.full_sites(min_score=0)[0]
        self.assertEqual((site["name"], site["kind"], site["condition"], site["category"]),
                         ("Gripwood Quarry", "underground quarry", "Underground", "mines"))
        self.assertEqual(best(), ["Gripwood Quarry"])
        # Galleries under a village aren't the closed school on top of them, 40 m away.
        self.store.upsert_od([od("s1", 51.3870, -2.2830, name="Monkton Farleigh School", weight=8, kind="school",
                                 dataset="schools", evidence="Closed in 1990"),
                              od("p2", 51.38736, -2.2830, name="Underground quarry, Monkton Farleigh", weight=20,
                                 kind="underground quarry", dataset="old_maps",
                                 evidence="... with 1 air shaft within 500 m: worked underground")], self.now)
        build_sites(self.store)
        conditions = {s["name"]: s["condition"] for s in self.store.full_sites(min_score=0)}
        self.assertEqual((conditions["Monkton Farleigh School"], conditions["Underground quarry, Monkton Farleigh"]),
                         ("Closed 1990", "Underground"))

    def test_names_are_found_spaces_and_hyphens_aside(self):
        # The old map runs Butser Hill together; Bradford-on-Avon comes with or without its hyphens.
        self.store.upsert_od([od("p1", 50.9793, -0.9645, name="Butserhill Lime Works", weight=6, kind="lime works",
                                 dataset="old_maps", evidence="marks it"),
                              od("p2", 51.339, -2.2501, name="Underground quarry, Bradford-On-Avon", weight=20,
                                 kind="underground quarry", dataset="old_maps", evidence="worked underground")],
                             self.now)
        build_sites(self.store)
        found = lambda q: [s["name"] for s in self.store.full_sites(min_score=0, q=q)]   # noqa: E731
        self.assertEqual(found("butser hill"), ["Butserhill Lime Works"])
        self.assertEqual(found("bradford on avon"), ["Underground quarry, Bradford-On-Avon"])
        self.assertEqual(found("lime works"), ["Butserhill Lime Works"])

    def test_a_register_record_can_stand_alone(self):
        self.store.upsert_od([od("42", 51.05, -1.72)], self.now)
        build_sites(self.store)
        site = self.store.full_sites(min_score=0)[0]
        self.assertEqual(site["key"], "canmore:42")
        self.assertEqual((site["sources"], site["score"], site["strength"]), ("canmore", 25, "good"))
        self.assertEqual(site["category"], "bunkers")

    def test_two_registers_agreeing_are_said_once(self):
        self.store.upsert_od([od("1", 51.5, -0.12), od("2", 51.5, -0.12, dataset="coflein",
                                                       evidence="Coflein records a pillbox here")], self.now)
        build_sites(self.store)
        site = self.store.full_sites(min_score=0)[0]
        self.assertEqual(site["sources"], "canmore+coflein")
        self.assertIn("Also recorded by Coflein (Wales)", site["reasons"])

    def test_three_sources_agreeing_count_for_more_than_two(self):
        self.store.upsert_osm([{"osm_id": "node/1", "lat": 51.5, "lng": -0.12,
                                "tags": {"military": "bunker", "name": "Alderbury ROC Post"}}], self.now)
        self.store.upsert_od([od("1", 51.5, -0.12)], self.now)
        build_sites(self.store)
        two = self.store.full_sites(min_score=0)[0]["score"]
        self.store.upsert_od([od("2", 51.5, -0.12, dataset="coflein", evidence="Coflein records a bunker here")],
                             self.now)
        build_sites(self.store)
        self.assertEqual(self.store.full_sites(min_score=0)[0]["score"], two + 5)

    def test_the_parts_of_a_complex_are_one_place(self):
        parts = [od("1", 52.9850, -3.9268, name="Track II, Manod Granite Quarries", kind="old workings",
                    evidence="Coflein records a trackway here", dataset="coflein"),
                 od("2", 52.9855, -3.9272, name="Incline III, Manod Granite Quarries", kind="old workings",
                    evidence="Coflein records an incline here", dataset="coflein"),
                 od("3", 52.9105, -3.8144, name="Quarry I, Bwlch y Bi", kind="old workings", dataset="coflein",
                    evidence="Coflein records a quarry here"),
                 od("4", 52.9110, -3.8150, name="Quarry II, Bwlch y Bi", kind="old workings", dataset="coflein",
                    evidence="Coflein records a quarry here"),
                 # Two pillboxes in the same village are two places, not one.
                 od("5", 51.4900, -3.2200, name="Pillbox, Llandaff", kind="military structure", dataset="coflein",
                    evidence="Coflein records a pillbox here"),
                 od("6", 51.4905, -3.2210, name="Pillbox, Llandaff", kind="military structure", dataset="coflein",
                    evidence="Coflein records a pillbox here")]
        self.store.upsert_od(parts, self.now)
        build_sites(self.store)
        names = sorted(s["name"] for s in self.store.full_sites(min_score=0))
        self.assertEqual(names, ["Manod Granite Quarries", "Pillbox, Llandaff", "Pillbox, Llandaff", "Quarry, Bwlch y Bi"])
        manod = next(s for s in self.store.full_sites(min_score=0) if s["name"] == "Manod Granite Quarries")
        self.assertIn("Coflein (Wales) records 1 more part of it", manod["reasons"])

    def test_museums_and_attractions_are_weak_leads(self):
        # Lady Victoria Colliery: a Canmore colliery inside the museum that's there now.
        self.store.upsert_od([od("1", 55.8727, -3.0569, name="Lady Victoria Colliery, Newtongrange", weight=22,
                                 kind="old workings", evidence="Canmore records a colliery here")], self.now)
        self.store.upsert_osm([{"osm_id": "way/1", "lat": 55.8730, "lng": -3.0575, "extent_m": 150,
                                "tags": {"tourism": "museum", "name": "National Mining Museum Scotland"}},
                               # A point-mapped attraction 200 m from another colliery: a different place.
                               {"osm_id": "node/2", "lat": 55.9000, "lng": -3.1000,
                                "tags": {"tourism": "attraction", "name": "Viewpoint Sculpture"}},
                               # A country park tagged as an attraction is too big to say anything.
                               {"osm_id": "way/3", "lat": 55.95, "lng": -3.20, "extent_m": 4000,
                                "tags": {"tourism": "attraction", "name": "Big Country Park"}}], self.now)
        self.store.upsert_od([od("2", 55.9018, -3.1000, name="Old Colliery", weight=22, kind="old workings",
                                 evidence="Canmore records a colliery here"),
                              od("3", 55.9500, -3.2010, name="Park Colliery", weight=22, kind="old workings",
                                 evidence="Canmore records a colliery here")], self.now)
        build_sites(self.store)
        sites = {s["name"]: s for s in self.store.full_sites(min_score=0)}
        museum = sites["Lady Victoria Colliery, Newtongrange"]
        self.assertEqual((museum["condition"], museum["strength"], museum["category"]), ("Museum", "weak", "mines"))
        self.assertEqual(museum["reasons"][0],
                         "OpenStreetMap maps National Mining Museum Scotland as a museum, open to visitors")
        self.assertEqual(sites["Old Colliery"]["strength"], "good")        # 200 m from a point: not it
        self.assertEqual(sites["Park Colliery"]["strength"], "good")       # the park's outline is too big to trust

    def test_a_gallery_on_the_high_street_doesnt_demote_the_pub_next_door(self):
        self.store.upsert_osm([
            {"osm_id": "node/10", "lat": 51.4580, "lng": -2.1160, "tags": {"disused:amenity": "pub", "name": "The Bear"}},
            # Chippenham Museum, mapped as a point, 30 m along the street: a different building.
            {"osm_id": "node/11", "lat": 51.4582, "lng": -2.1164,
             "tags": {"tourism": "museum", "name": "Chippenham Museum & Heritage Centre"}},
            # A disused mill with a museum inside it, mapped as a point on the same spot, does get demoted...
            {"osm_id": "node/12", "lat": 51.0800, "lng": -1.8600, "tags": {"disused:man_made": "works", "name": "Carpet Factory"}},
            {"osm_id": "node/13", "lat": 51.08005, "lng": -1.86005,
             "tags": {"tourism": "museum", "name": "Wilton Royal Carpet Factory Museum"}},
            # ...and so does a neighbour that shares its name, a little further off.
            {"osm_id": "node/14", "lat": 51.0803, "lng": -1.8603, "tags": {"disused:man_made": "works", "name": "Royal Carpet Weaving Shed"}},
        ], self.now)
        build_sites(self.store)
        sites = {s["name"]: s for s in self.store.full_sites(min_score=0)}
        self.assertEqual(sites["The Bear"]["condition"], "Closed")   # still a shut pub, not a museum
        self.assertEqual(sites["Carpet Factory"]["condition"], "Museum")
        self.assertEqual(sites["Royal Carpet Weaving Shed"]["condition"], "Museum")

    def test_a_wikidata_museum_demotes_itself(self):
        from bandobuddy.wikidata import in_use_as
        self.assertEqual(in_use_as(["colliery", "museum"]), "Museum")
        self.assertEqual(in_use_as(["railway station", "heritage railway station"]), "Heritage railway")
        self.assertIsNone(in_use_as(["colliery"]))

    def _notes(self, *rows):
        """Your own import: (name, kind, metres north, metres east, other names) around Gripwood."""
        from bandobuddy.geo import offset
        items = []
        for name, kind, north, east, aka in rows:
            lat, lng = offset(51.3415, -2.2569, north, east)
            items.append({"dataset": "imported", "ref": f"notes:{name}:{north}:{east}", "name": name, "lat": lat,
                          "lng": lng, "kind": kind, "evidence": f'From your import "notes": {kind}', "weight": 20,
                          "url": None, "aliases": aka})
        self.store.upsert_od(items, self.now)

    def test_entrances_join_the_place_they_belong_to(self):
        self._notes(
            ("Gripwood Quarry", "quarry", 0, 0, []),
            ("Bethel Quarry", "quarry", 30, 20, ["Gripwood Quarry"]),        # the same quarry, by its other name
            ("Gripwood Quarry Main Entrance", "cave entrance", -150, 40, []),
            ("Gripwood Quarry Entrance 1", "cave entrance", -180, -60, []),
            ("Bethel Quarry Air Shaft 1", "air shaft", 250, 120, []),      # joins through "Bethel"
            ("Frome Road Tunnel Portal", "portal", 200, -100, []),        # shares no name: a different place
            ("Unnamed cave entrance", "cave entrance", 300, 300, []),     # unnamed, and too far to assume
        )
        self.store.upsert_osm([{"osm_id": "node/77", "lat": 51.3415 + 0.0005, "lng": -2.2569,   # ~55 m, unnamed
                                "tags": {"man_made": "mineshaft"}}], self.now)
        build_sites(self.store)
        sites = {s["name"]: s for s in self.store.full_sites(min_score=0)}
        gripwood = sites["Gripwood Quarry"]
        self.assertEqual(gripwood["aliases"], ["Bethel Quarry"])
        self.assertEqual([e["name"] or e["kind"] for e in gripwood["entrances"]],
                         ["Bethel Quarry Air Shaft 1", "Gripwood Quarry Entrance 1", "Gripwood Quarry Main Entrance",
                          "Shaft"])
        self.assertEqual({e["kind"] for e in gripwood["entrances"]}, {"Air shaft", "Entrance", "Shaft"})
        self.assertIn("Frome Road Tunnel Portal", sites)
        self.assertIn("Unnamed cave entrance", sites)
        # Its other name finds it.
        total, rows = self.store.list_sites(q="Bethel")
        self.assertEqual([r["name"] for r in rows], ["Gripwood Quarry"])
        self.assertEqual(rows[0]["entrance_count"], 4)

    def test_entrances_with_no_place_become_one(self):
        self._notes(("Gripwood Quarry Main Entrance", "cave entrance", 0, 0, []),
                    ("Gripwood Quarry Entrance 1", "cave entrance", -40, 90, []))
        build_sites(self.store)
        sites = self.store.full_sites(min_score=0)
        self.assertEqual([s["name"] for s in sites], ["Gripwood Quarry"])
        self.assertEqual(len(sites[0]["entrances"]), 2)

    def test_a_real_name_beats_a_brownfield_address(self):
        self.store.upsert_od([od("b1", 50.64, -4.36, name="22-23 High Street", weight=10, kind="brownfield land",
                                 evidence="On the council's brownfield land register", dataset="brownfield"),
                              od("h1", 50.64, -4.36, name="The Guildhall, High Street", weight=25, kind="listed building",
                                 evidence="Historic England has it on the Heritage at Risk register (listed building)",
                                 dataset="heritage_at_risk")], self.now)
        build_sites(self.store)
        site = self.store.full_sites(min_score=0)[0]
        self.assertEqual((site["name"], site["aliases"]), ("The Guildhall, High Street", []))

    def test_a_lone_entrance_is_just_a_place(self):
        self._notes(("Swildon's Hole", "cave entrance", 0, 0, []))
        build_sites(self.store)
        self.assertEqual(self.store.full_sites(min_score=0)[0]["entrances"], [])

    def test_exports_carry_each_entrance(self):
        from bandobuddy import export
        self._notes(("Gripwood Quarry", "quarry", 0, 0, ["Bethel Quarry"]),
                    ("Air shaft", "air shaft", 100, 0, []),
                    ("Gripwood Quarry Main Entrance", "cave entrance", -150, 40, []))
        # the unnamed-ish "Air shaft" is 100 m off: named "Air shaft", which shares no name, so it stays apart
        build_sites(self.store)
        site = next(s for s in self.store.full_sites(min_score=0) if s["name"] == "Gripwood Quarry")
        gpx = export.to_gpx([site])
        self.assertEqual(gpx.count("<wpt"), 2)                                   # the quarry and its entrance
        self.assertIn("<name>Gripwood Quarry Main Entrance</name>", gpx)
        self.assertIn("Bethel Quarry", export.to_csv([site]))

    def test_a_weak_lead_on_top_of_a_site_is_that_site(self):
        self.store.upsert_osm([{"osm_id": "way/1", "lat": 51.5, "lng": -0.12,
                                "tags": {"building": "ruins", "name": "Old Engine House"}}], self.now)
        self.store.upsert_od([od("9", 51.5004, -0.12, name="Land at Mill Lane", weight=10, kind="brownfield land",
                                 evidence="On the council's brownfield land register", dataset="brownfield")],
                             self.now)  # ~45 m away, nothing alike in the name
        build_sites(self.store)
        sites = self.store.full_sites(min_score=0)
        self.assertEqual([(s["name"], s["sources"]) for s in sites], [("Old Engine House", "osm+brownfield")])


class UpdaterTests(unittest.TestCase):
    def test_running_a_register_stores_its_records(self):
        tmp = Path(tempfile.mkdtemp())
        store = Store(tmp / "t.db")
        dataset = opendata.Dataset(
            key="canmore", label="Canmore", licence="OGL", attribution="x", home="https://x",
            fetch=lambda session, progress, cancel=None: iter([
                {"CANMOREID": 1, "NMRSNAME": "BRATTON ROC POST", "SITETYPE": "OBSERVATION POST",
                 "lat": 51.27, "lng": -2.12, "URL": "https://trove/1"}]),
            judge=opendata.DATASETS["canmore"].judge,
        )
        with mock.patch.dict(opendata.DATASETS, {"canmore": dataset}):
            up = Updater(store, tmp, session_factory=lambda: None, log=lambda m: None, sources=("canmore",))
            up.run("canmore")
        self.assertEqual([r["name"] for r in store.active_od()], ["Bratton ROC Post"])
        self.assertEqual(store.last_finished("canmore")["status"], "done")
        self.assertIsNotNone(store.get_setting("baseline_canmore"))  # nothing counts as "new" first time
        self.assertEqual(json.loads(json.dumps(store.full_sites(min_score=0)[0]["detail"]))["open"][0]["ref"], "1")


if __name__ == "__main__":
    unittest.main()
