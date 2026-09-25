import unittest

from bandobuddy import access, webapp
from bandobuddy.geo import bearing_deg, compass

SITE = (51.3415, -2.2569)


def way(tags, *points, id_=1):
    return {"type": "way", "id": id_, "tags": tags, "geometry": [{"lat": a, "lon": b} for a, b in points]}


def node(tags, lat, lng, id_=1):
    return {"type": "node", "id": id_, "tags": tags, "lat": lat, "lon": lng}


class SummaryTests(unittest.TestCase):
    def test_nearest_right_of_way_parking_and_private_things(self):
        elements = [
            # A public footpath running east-west ~120 m north of the site.
            way({"highway": "footway", "designation": "public_footpath", "prow_ref": "BRAD27"},
                (51.3426, -2.2600), (51.3426, -2.2540)),
            # A farther bridleway: not the nearest right of way.
            way({"highway": "bridleway", "designation": "public_bridleway"}, (51.3440, -2.2600), (51.3440, -2.2540), id_=2),
            # A track that's farther than the footpath: not worth mentioning separately.
            way({"highway": "track", "foot": "yes"}, (51.3400, -2.2600), (51.3400, -2.2540), id_=3),
            # A private track right by the site: not a way in, but worth knowing about.
            way({"highway": "track", "access": "private"}, (51.34155, -2.2575), (51.34155, -2.2560), id_=4),
            node({"barrier": "gate", "access": "private"}, 51.3416, -2.2570, id_=5),
            # Car parks: a customers-only one is skipped; the public one is reported with its fee.
            node({"amenity": "parking", "access": "customers"}, 51.3417, -2.2569, id_=6),
            node({"amenity": "parking", "fee": "yes", "name": "Barton Farm"}, 51.3430, -2.2547, id_=7),
        ]
        out = access.summarise(*SITE, elements)
        self.assertEqual(out["right_of_way"]["kind"], "Public footpath")
        self.assertEqual(out["right_of_way"]["ref"], "BRAD27")
        self.assertEqual(out["right_of_way"]["direction"], "N")
        self.assertAlmostEqual(out["right_of_way"]["distance_m"], 122, delta=3)
        self.assertIsNone(out["path"])           # the footpath is nearer than any other path
        self.assertEqual(out["parking"]["name"], "Barton Farm")
        self.assertTrue(out["parking"]["fee"])
        self.assertEqual(out["private"], ["a track", "a gate"])  # nearest first

    def test_a_nearer_path_is_mentioned_when_it_isnt_a_right_of_way(self):
        elements = [
            way({"highway": "footway", "designation": "public_footpath"}, (51.3450, -2.2600), (51.3450, -2.2540)),
            way({"highway": "path", "foot": "permissive"}, (51.3417, -2.2600), (51.3417, -2.2540), id_=2),
        ]
        out = access.summarise(*SITE, elements)
        self.assertEqual(out["path"]["kind"], "Path")
        self.assertTrue(out["path"]["permissive"])
        self.assertLess(out["path"]["distance_m"], out["right_of_way"]["distance_m"])

    def test_nothing_nearby(self):
        self.assertEqual(access.summarise(*SITE, []),
                         {"right_of_way": None, "path": None, "parking": None, "private": []})

    def test_bearings(self):
        self.assertEqual(compass(bearing_deg(51.0, -2.0, 51.1, -2.0)), "N")
        self.assertEqual(compass(bearing_deg(51.0, -2.0, 51.0, -1.9)), "E")
        self.assertEqual(compass(bearing_deg(51.0, -2.0, 50.9, -2.1)), "SW")


class FakeOverpass:
    def __init__(self):
        self.calls = 0

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls += 1
        self.query = data["data"]

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"elements": [node({"amenity": "parking"}, 51.3420, -2.2569)]}

        return Resp()


class AroundTests(unittest.TestCase):
    def test_asks_overpass_about_a_small_area(self):
        fake = FakeOverpass()
        out = access.around(*SITE, fake)
        self.assertEqual(fake.calls, 1)
        self.assertIn("around:400,51.3415,-2.2569", fake.query)
        self.assertEqual(out["parking"]["direction"], "N")

    def test_the_web_app_asks_once_per_spot(self):
        fake = FakeOverpass()
        app = webapp.App(None, None, session_factory=lambda: fake)
        first = app.access({"lat": ["51.3415"], "lng": ["-2.2569"]})
        again = app.access({"lat": ["51.34151"], "lng": ["-2.25691"]})   # the same spot, a metre off
        self.assertEqual(first, again)
        self.assertEqual(fake.calls, 1)
        with self.assertRaises(webapp.ApiError):
            app.access({"lat": ["north"]})


if __name__ == "__main__":
    unittest.main()
