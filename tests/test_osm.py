import tempfile
import threading
import unittest
from pathlib import Path

from bandobuddy.osm import Cancelled, classify, extract_candidates, is_candidate

from tests.fakes import write_osm

try:
    import osmium  # noqa: F401
    HAVE_OSMIUM = True
except ModuleNotFoundError:
    HAVE_OSMIUM = False


class Tag:
    def __init__(self, k, v):
        self.k, self.v = k, v


class ClassifyTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify({"building": "ruins"})[1], 35)
        self.assertEqual(classify({"disused:shop": "supermarket"}), ("OSM: disused:shop=supermarket", 25))
        self.assertIsNone(classify({"abandoned:railway": "rail"}))
        self.assertIsNone(classify({"disused:name": "Foo"}))
        self.assertEqual(classify({"landuse": "brownfield"})[1], 10)
        self.assertEqual(classify({"historic": "ruins", "name": "Great Tower"})[1], 10)
        self.assertEqual(classify({"man_made": "adit"})[1], 25)
        self.assertEqual(classify({"military": "bunker", "bunker_type": "pillbox"}), ("OSM: military bunker (pillbox)", 25))
        self.assertEqual(classify({"natural": "cave_entrance"})[1], 15)
        self.assertEqual(classify({"railway": "abandoned", "tunnel": "yes"})[1], 35)
        self.assertEqual(classify({"abandoned:railway": "rail", "tunnel": "yes"})[1], 35)
        self.assertEqual(classify({"historic": "railway_station", "name": "Midford"})[1], 25)
        self.assertEqual(classify({"building": "yes", "description": "Derelict since 1990", "disused": "yes"}),
                         ("OSM: description says 'derelict'", 25))

    def test_only_lifecycle_tags_on_the_feature_itself_count(self):
        # A pub whose website has lapsed is not a derelict pub.
        self.assertIsNone(classify({"amenity": "pub", "disused:website": "http://example.org"}))
        self.assertIsNone(classify({"shop": "bakery", "disused:phone": "01225 000000", "disused:opening_hours": "Mo-Fr"}))
        # When both are there, the evidence names the pub, not the website.
        evidence, _ = classify({"disused:website": "http://example.org", "disused:amenity": "pub", "disused:building": "yes"})
        self.assertEqual(evidence, "OSM: disused:amenity=pub")

    def test_old_road_alignments_arent_places(self):
        self.assertIsNone(classify({"abandoned:highway": "primary", "name": "A344"}))
        self.assertIsNotNone(classify({"abandoned:highway": "primary", "tunnel": "yes"}))  # a road tunnel still is

    def test_what_a_place_is_in_use_as(self):
        from bandobuddy.osm import in_use_as
        self.assertEqual(in_use_as({"tourism": "museum", "name": "National Mining Museum Scotland"}), "Museum")
        self.assertEqual(in_use_as({"tourism": "attraction"}), "Visitor attraction")
        self.assertEqual(in_use_as({"railway": "station", "usage": "tourism"}), "Heritage railway")
        self.assertEqual(in_use_as({"historic": "ruins", "operator": "English Heritage"}), "Heritage site")
        self.assertIsNone(in_use_as({"railway": "station"}))                 # an ordinary station
        self.assertIsNone(in_use_as({"building": "ruins"}))
        # Kept by the extract even though it's no lead itself.
        self.assertTrue(is_candidate([Tag("tourism", "museum")]))

    def test_heritage_ruins_and_shop_units_count_for_less(self):
        castle = {"building": "ruins", "historic": "castle", "name": "Old Wardour Castle", "operator": "English Heritage"}
        self.assertEqual(classify(castle)[1], 10)
        self.assertEqual(classify({"building": "ruins", "name": "Chapel Ruins"})[1], 35)
        self.assertEqual(classify({"disused:amenity": "bank", "name": "HSBC"}, "node")[1], 15)
        self.assertEqual(classify({"disused:amenity": "bank", "name": "HSBC"}, "way")[1], 25)

    def test_is_candidate_prefilter(self):
        self.assertTrue(is_candidate([Tag("disused:shop", "x")]))
        self.assertTrue(is_candidate([Tag("military", "bunker")]))
        self.assertTrue(is_candidate([Tag("disused:amenity", "pub")]))
        self.assertFalse(is_candidate([Tag("amenity", "pub"), Tag("disused:website", "http://example.org")]))
        self.assertTrue(is_candidate([Tag("name", "The Derelict Barn")]))
        self.assertFalse(is_candidate([Tag("amenity", "cafe"), Tag("name", "Busy Cafe")]))
        self.assertFalse(is_candidate([Tag("disused", "no")]))


@unittest.skipUnless(HAVE_OSMIUM, "pyosmium not installed")
class ExtractTests(unittest.TestCase):
    def test_extract_points_outlines_and_relations(self):
        path = write_osm(Path(tempfile.mkdtemp()) / "area.osm")
        early = []
        items = {i["osm_id"]: i for i in extract_candidates(path, on_nodes=early.append)}
        self.assertEqual(set(items), {"node/1", "node/2", "node/4", "way/100", "way/102", "relation/200"})
        self.assertEqual({n["osm_id"] for n in early[0]}, {"node/1", "node/2", "node/4"})  # points arrive first
        self.assertAlmostEqual(items["way/100"]["lat"], 51.5105, places=4)  # centre of the outline
        self.assertAlmostEqual(items["way/100"]["lng"], -0.1295, places=4)
        self.assertAlmostEqual(items["relation/200"]["lat"], 51.4910, places=4)  # from its member way
        self.assertEqual(items["way/102"]["tags"]["name"], "Hill Tunnel")

    def test_cancel(self):
        path = write_osm(Path(tempfile.mkdtemp()) / "area.osm")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            extract_candidates(path, cancel=cancel)


if __name__ == "__main__":
    unittest.main()
