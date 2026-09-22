import unittest

from bandobuddy.wikidata import (
    ServiceBusy,
    TileTooBig,
    build_tile_query,
    classify,
    evaluate,
    fetch_intros,
    fetch_tile,
    judge_intro,
)

from tests.fakes import FakeSession


class ClassifyTests(unittest.TestCase):
    """Cases taken from a real query around Bradford-on-Avon."""

    def test_keeps_bando_material(self):
        self.assertEqual(classify("Gripwood Quarry", ["protected area", "quarry"], [], None),
                         ("Wikidata: old quarry", 25, "quarry"))
        self.assertEqual(classify("Winsley Mines", [], [], None)[1], 25)
        self.assertEqual(classify("Midford Viaduct (B&NSR)", ["railway viaduct"], ["abandoned"], None)[1], 30)
        self.assertEqual(classify("Fitzmaurice Grammar School", ["grammar school"], [], "1980-01-01T00:00:00Z"),
                         ("Wikidata: closed in 1980", 20, "grammar school"))
        self.assertEqual(classify("Former Baptist Chapel", ["chapel"], [], None)[1], 10)

    def test_old_stations_count_for_less(self):
        self.assertEqual(classify("Limpley Stoke railway station", ["railway station"], ["decommissioned"], None)[1], 20)
        self.assertEqual(classify("Midford Halt railway station", ["railway station"], ["decommissioned"], None)[1], 10)

    def test_drops_clutter_and_things_that_are_gone(self):
        self.assertIsNone(classify("Wall And Gate To North Of Former Wesleyan Methodist Church", ["wall"], [], None))
        self.assertIsNone(classify("Former Coach House, Stables And Barn", ["barn", "stable"], [], None))
        self.assertIsNone(classify("Bradford-on-Avon railway station", ["railway station"], ["in use"], None))
        self.assertIsNone(classify("Regal Cinema", ["destroyed building or structure", "movie theater"],
                                   ["permanently closed"], None))
        self.assertIsNone(classify("Bradford Without", ["former administrative territorial entity", "civil parish"],
                                   [], "1934-04-01"))
        self.assertIsNone(classify("Tucking Mill", ["village"], [], None))
        self.assertIsNone(classify("Brewery House", ["listed building", "house"], [], None))


class IntroTests(unittest.TestCase):
    def test_intro_judgements(self):
        delta, _, snippet = judge_intro(
            "Combe Down and Bathampton Down Quarries make up a 6.22 hectare SSSI. "
            "The disused quarries date from the 17th and 18th centuries.")
        self.assertEqual(delta, 15)
        self.assertTrue(snippet.startswith("The disused quarries"))
        self.assertEqual(judge_intro("Limpley Stoke railway station is a former railway station. "
                                     "The station closed in 1966, and the building is now in private hands.")[0], -15)
        self.assertEqual(judge_intro("It was demolished in 1972.")[0], -999)
        self.assertEqual(judge_intro("Foo Quarry is a working quarry operated by Tarmac.")[0], -20)

    def test_evaluate_combines_wikidata_and_wikipedia(self):
        row = {"qid": "Q1", "label": "Hillside Quarry", "lat": 51.5, "lng": -0.12, "types": ["quarry"],
               "states": [], "ended": None, "wiki": "https://en.wikipedia.org/wiki/Hillside_Quarry"}
        self.assertEqual(evaluate(row, None)["weight"], 25)
        ev = evaluate(row, "Hillside Quarry is a disused limestone mine.")
        self.assertEqual(ev["weight"], 40)
        self.assertIn("disused limestone mine", ev["snippet"])
        self.assertIsNone(evaluate(row, "It was demolished in 1972."))


class FetchTests(unittest.TestCase):
    def test_tile_query_is_uk_only_and_boxed(self):
        q = build_tile_query(51.0, -3.0, 51.5, -2.5)
        self.assertIn("wdt:P17 wd:Q145", q)
        self.assertIn('cornerSouthWest "Point(-3.000000 51.000000)"', q)
        self.assertIn('cornerNorthEast "Point(-2.500000 51.500000)"', q)

    def test_fetch_tile_rows_and_errors(self):
        session = FakeSession(split_wider_than=1.0)
        rows = {r["qid"]: r for r in fetch_tile(51.49, -0.14, 51.52, -0.10, session)}
        self.assertIn("Q1", rows)
        self.assertEqual(rows["Q1"]["types"], ["quarry", "protected area"])
        self.assertTrue(rows["Q1"]["wiki"].endswith("Hillside_Quarry"))
        with self.assertRaises(TileTooBig):
            fetch_tile(50.0, -3.0, 52.0, 0.0, session)  # wider than the fake's limit: "times out"
        session.busy_next = 1
        with self.assertRaises(ServiceBusy):
            fetch_tile(51.49, -0.14, 51.52, -0.10, session)

    def test_fetch_intros(self):
        got = fetch_intros(["Hillside Quarry", "Unknown Page"], FakeSession())
        self.assertIn("disused limestone mine", got["Hillside Quarry"])
        self.assertEqual(got["Unknown Page"], "")


if __name__ == "__main__":
    unittest.main()
