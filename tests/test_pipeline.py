import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bandobuddy import extract
from bandobuddy.config import DB_NAME
from bandobuddy.geo import grid_boxes
from bandobuddy.sites import build_sites
from bandobuddy.store import Store
from bandobuddy.updater import Updater

from tests.fakes import EXTRACT_URL, OSM_XML, FakeSession

try:
    import osmium  # noqa: F401
    HAVE_OSMIUM = True
except ImportError:
    HAVE_OSMIUM = False

AREA = (51.3, -0.4, 51.8, 0.1)  # a small "UK" for the tests: one 0.5 degree box, too big for one query


def make_updater(tmp: Path, session: FakeSession | None = None) -> Updater:
    session = session or FakeSession()
    store = Store(tmp / DB_NAME)
    up = Updater(store, tmp, session_factory=lambda: session, log=lambda m: None, extract_url=EXTRACT_URL, uk_bbox=AREA)
    up.polite_delay = up.intro_delay = 0
    return up


class GeoTests(unittest.TestCase):
    def test_grid_boxes_cover_area(self):
        boxes = grid_boxes((49.8, -8.7, 60.95, 1.9), 0.5)
        self.assertEqual(len(boxes), 23 * 22)
        self.assertEqual(boxes[0][:2], (49.8, -8.7))
        self.assertEqual(max(b[2] for b in boxes), 60.95)
        self.assertEqual(max(b[3] for b in boxes), 1.9)


class DownloadTests(unittest.TestCase):
    def test_download_resumes_and_checks_md5(self):
        tmp = Path(tempfile.mkdtemp())
        dest = tmp / "test-area.osm"
        session = FakeSession()
        part = tmp / "test-area.osm.part"
        part.write_bytes(OSM_XML.encode()[:100])  # a previous, interrupted download
        (tmp / "test-area.osm.part.json").write_text('{"last_modified": "Mon, 21 Sep 2026 23:00:00 GMT"}')
        extract.download(EXTRACT_URL, dest, session)
        self.assertEqual(dest.read_text(encoding="utf-8"), OSM_XML)
        self.assertFalse(part.exists())

    def test_corrupt_download_is_thrown_away(self):
        tmp = Path(tempfile.mkdtemp())
        session = FakeSession()
        part = tmp / "test-area.osm.part"
        part.write_bytes(b"garbage garbage garbage")
        (tmp / "test-area.osm.part.json").write_text('{"last_modified": "x"}')
        with self.assertRaises(RuntimeError):
            extract.download(EXTRACT_URL, tmp / "test-area.osm", session)
        self.assertFalse(part.exists())


@unittest.skipUnless(HAVE_OSMIUM, "pyosmium not installed")
class UpdaterTests(unittest.TestCase):
    def test_full_build(self):
        tmp = Path(tempfile.mkdtemp())
        session = FakeSession()
        up = make_updater(tmp, session)
        self.assertEqual(up.due_sources(), ["osm", "wikidata"])
        up.run("osm")
        up.run("wikidata")
        self.assertEqual(up.due_sources(), [])

        store = up.store
        sites = {s["name"]: s for s in store.full_sites(min_score=0)}
        # OSM places, including the multipolygon hospital and the abandoned tunnel.
        self.assertIn("St Agnes Hospital", sites)
        self.assertEqual(sites["Hill Tunnel"]["category"], "underground")
        self.assertEqual(sites["Unnamed bunker"]["category"], "military")
        self.assertEqual(sites["Derelict Barn"]["score"], 40)  # name says derelict: 25 + 15
        self.assertNotIn("Busy Cafe", sites)
        # Wikidata: joins the OSM mill rather than duplicating it; stands alone for the quarry.
        mill = sites["Old Mill"]
        self.assertEqual(mill["sources"], "osm+wikidata")
        self.assertEqual(mill["key"], "osm:way/100")
        self.assertEqual(sites["Hillside Quarry"]["sources"], "wikidata")
        self.assertTrue(any("disused limestone mine" in r for r in sites["Hillside Quarry"]["reasons"]))
        for gone in ("Garden Wall At Foo House", "Old Town railway station", "Knocked Down Mill"):
            self.assertNotIn(gone, sites)
        # The Wikidata box was too big for one query, so it was split (and remembered).
        tiles = store.tile_counts("wikidata")
        self.assertEqual(tiles, {"split": 1, "done": 4})
        # Nothing is "new" on the first build.
        self.assertTrue(all(s["added"] is None for s in sites.values()))

    def test_new_and_gone_on_the_next_update(self):
        tmp = Path(tempfile.mkdtemp())
        session = FakeSession()
        up = make_updater(tmp, session)
        up.run("osm")
        up.run("wikidata")

        # A week later: the mill has been removed from OSM, a new pillbox mapped, a new Wikidata item added.
        session.osm_bytes = OSM_XML.replace('<tag k="building" v="ruins"/><tag k="name" v="Old Mill"/>', "").replace(
            "</osm>", '<node id="5" version="1" timestamp="2024-01-01T00:00:00Z" lat="51.45" lon="-0.15">'
                      '<tag k="historic" v="pillbox"/><tag k="name" v="New Pillbox"/></node></osm>').encode()
        from tests.fakes import wd_item
        session.wikidata.append(wd_item("Q9", "Lost Colliery", -900, -900, "colliery"))
        (tmp / "test-area.osm").unlink()  # no update information in a test file: forces a fresh download
        up.run("osm")
        up.run("wikidata")

        sites = {s["name"]: s for s in up.store.full_sites(min_score=0)}
        self.assertIsNotNone(sites["New Pillbox"]["added"])
        self.assertIsNotNone(sites["Lost Colliery"]["added"])
        self.assertIsNone(sites["Hillside Quarry"]["added"])
        # The OSM mill outline is gone; the Wikidata entry for it remains as its own (weaker) site.
        self.assertEqual(sites["Old Mill"]["sources"], "wikidata")
        # Reused the split boxes from last time: no timeouts, no re-splitting.
        self.assertEqual(up.store.tile_counts("wikidata"), {"done": 4})

    def test_wikidata_resumes_after_pause(self):
        tmp = Path(tempfile.mkdtemp())
        session = FakeSession()
        up = make_updater(tmp, session)
        calls = {"n": 0}
        original = up.store.finish_tile

        def pause_after_two(*a, **kw):
            original(*a, **kw)
            calls["n"] += 1
            if calls["n"] == 2:
                up.pause("wikidata")

        up.store.finish_tile = pause_after_two
        up.run("wikidata")  # returns quietly when paused
        self.assertIsNotNone(up.store.unfinished_crawl("wikidata"))
        self.assertEqual(up.store.tile_counts("wikidata").get("pending"), 2)
        self.assertEqual(up.due_sources(), ["osm"])  # paused by the user: not auto-restarted

        up.store.finish_tile = original
        up.start("wikidata", by_user=True)
        up.join(10)
        self.assertIsNone(up.store.unfinished_crawl("wikidata"))
        self.assertEqual(up.store.tile_counts("wikidata"), {"split": 1, "done": 4})

    def test_busy_service_backs_off_and_retries(self):
        tmp = Path(tempfile.mkdtemp())
        session = FakeSession()
        session.busy_next = 2
        up = make_updater(tmp, session)
        up.run("wikidata")
        self.assertEqual(up.store.tile_counts("wikidata"), {"split": 1, "done": 4})

    def test_schedule(self):
        tmp = Path(tempfile.mkdtemp())
        up = make_updater(tmp)
        up.run("osm")
        up.run("wikidata")
        self.assertEqual(up.due_sources(), [])
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
        with up.store.connect() as db:
            db.execute("UPDATE crawls SET finished_at = ? WHERE source = 'osm'", (old,))
        self.assertEqual(up.due_sources(), ["osm"])
        up.store.set_setting("update_days", 10)
        self.assertEqual(up.due_sources(), [])
        up.store.set_setting("auto_update", False)
        up.store.set_setting("update_days", 1)
        self.assertEqual(up.due_sources(), [])


class SitesTests(unittest.TestCase):
    def test_osm_twins_merge_but_differently_named_neighbours_dont(self):
        store = Store(Path(tempfile.mkdtemp()) / "t.db")
        now = "2026-01-01T00:00:00+00:00"
        store.upsert_osm([
            {"osm_id": "way/1", "lat": 51.5, "lng": -0.12, "tags": {"building": "ruins", "name": "Chapel"}},
            {"osm_id": "node/2", "lat": 51.50005, "lng": -0.12, "tags": {"historic": "ruins"}},  # ~6 m, unnamed
            {"osm_id": "node/3", "lat": 51.50010, "lng": -0.12, "tags": {"disused:shop": "x", "name": "Shop"}},
        ], now)
        build_sites(store)
        sites = {s["name"]: s for s in store.full_sites(min_score=0)}
        self.assertEqual(len(sites["Chapel"]["detail"]["osm"]), 2)
        self.assertIn("Shop", sites)


if __name__ == "__main__":
    unittest.main()
