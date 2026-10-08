"""What visitors found: fixed choices only (access difficulty 1-5, accessible or not, tags), one report per
device per place, a dated history of access marks, and nothing that identifies a device."""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from bandobuddy import reports, webapp
from bandobuddy.config import DB_NAME, REPORTS_PER_HOUR
from bandobuddy.store import Store

from tests.fakes import FakeSession
from tests.test_pipeline import HAVE_OSMIUM, make_updater
from tests.test_webapp import call

DEVICE = "3f2b8c1e-5d4a-4e9b-9c7d-1a2b3c4d5e6f"


class ChoicesTests(unittest.TestCase):
    def test_only_the_fixed_choices(self):
        ok = reports.parse({"key": "osm:way/1", "device": DEVICE, "difficulty": 3, "access": "accessible",
                            "tags": ["trashed", "well-preserved", "trashed"]})
        self.assertEqual((ok["difficulty"], ok["access"], ok["tags"]), (3, "accessible", ["well-preserved", "trashed"]))
        self.assertTrue(reports.parse({"key": "osm:way/1", "device": DEVICE, "clear": True})["clear"])
        for bad in ({"difficulty": 0}, {"difficulty": 6}, {"difficulty": "3"}, {"difficulty": True},
                    {"access": "maybe"}, {"tags": ["haunted"]}, {"tags": "trashed"}, {},
                    {"device": "short", "difficulty": 2}, {"key": "", "difficulty": 2}):
            with self.assertRaises(reports.Invalid, msg=bad):
                reports.parse({"key": "osm:way/1", "device": DEVICE, **bad})

    def test_a_device_looks_different_to_every_place(self):
        a, b = reports.reporter_id(DEVICE, "osm:way/1"), reports.reporter_id(DEVICE, "osm:way/2")
        self.assertNotEqual(a, b)
        self.assertEqual(a, reports.reporter_id(DEVICE, "osm:way/1"))
        self.assertNotIn(DEVICE, a)

    def test_what_they_add_up_to(self):
        rows = [{"difficulty": 2, "access": "accessible", "tags": ["trashed"], "at": "2026-09-01T10:00:00"},
                {"difficulty": 3, "access": "accessible", "tags": ["trashed", "graffiti"], "at": "2026-10-01T10:00:00"},
                {"difficulty": 5, "access": "inaccessible", "tags": ["sealed"], "at": "2026-10-05T10:00:00"},
                {"difficulty": 1, "access": "accessible", "tags": [], "at": "2019-01-01T10:00:00"}]   # too old
        s = reports.summarize(rows, today=date(2026, 10, 7))
        self.assertEqual((s["n"], s["difficulty"], s["levels"]), (3, 3.3, [0, 1, 1, 0, 1]))   # the average
        self.assertEqual((s["accessible"], s["inaccessible"], s["latest"]), (2, 1, "2026-10-05"))
        self.assertEqual(list(s["tags"].items())[0], ("trashed", 2))
        self.assertNotIn("history", s)
        history = [{"at": "2026-03-01T09:00:00", "access": "accessible", "difficulty": 2},
                   {"at": "2026-06-01T09:00:00", "access": "inaccessible", "difficulty": None}]
        s = reports.summarize(rows, history, today=date(2026, 10, 7))
        self.assertEqual([h["on"] for h in s["history"]], ["2026-06-01", "2026-03-01"])   # newest first
        self.assertIsNone(reports.summarize([], today=date(2026, 10, 7)))


class HistoryTests(unittest.TestCase):
    def test_access_marks_are_kept_with_their_day(self):
        store = Store(Path(tempfile.mkdtemp()) / DB_NAME)
        me = reports.reporter_id(DEVICE, "k")
        store.save_report("k", me, 2, "accessible", [], "2026-03-01T09:00:00")
        store.save_report("k", me, 3, "accessible", ["trashed"], "2026-03-01T15:00:00")   # same day, same mind
        store.save_report("k", me, 3, "accessible", ["trashed"], "2026-04-02T09:00:00")   # a new visit
        store.save_report("k", me, None, "inaccessible", ["sealed"], "2026-06-01T09:00:00")   # sealed up since
        store.save_report("k", me, None, None, ["sealed"], "2026-06-02T09:00:00")   # no access mark: not logged
        rows, history = store.reports_for("k")
        self.assertEqual(len(rows), 1)                                               # one report per device
        self.assertEqual([(h["at"][:10], h["access"]) for h in sorted(history, key=lambda h: h["at"])],
                         [("2026-03-01", "accessible"), ("2026-04-02", "accessible"), ("2026-06-01", "inaccessible")])
        other = reports.reporter_id("a-different-device-0001", "k")
        store.save_report("k", other, 4, "inaccessible", [], "2026-06-03T09:00:00")
        store.clear_report("k", me)                      # taken back: a slip shouldn't stay in the history
        rows, history = store.reports_for("k")
        self.assertEqual(([r["reporter"] for r in rows], len(history)), ([other], 1))
        # A rebuild that read the reports before this can tell which places to put right afterwards.
        self.assertEqual(store.reports_touched_since("2000-01-01"), ["k"])
        self.assertEqual(store.reports_touched_since("2999-01-01"), [])


@unittest.skipUnless(HAVE_OSMIUM, "pyosmium not installed")
class ReportingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = FakeSession()
        cls.updater = make_updater(Path(tempfile.mkdtemp()), cls.session)
        cls.updater.run("osm")
        cls.app = webapp.App(cls.updater.store, cls.updater, session_factory=lambda: cls.session, read_only=True)
        cls.server = webapp.make_server(cls.app, 0)
        import threading
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]
        cls.key = cls.updater.store.full_sites(min_score=0)[0]["key"]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_the_public_copy_takes_reports_and_nothing_else(self):
        status, data = call(self.port, "POST", "/api/report", {"key": self.key, "device": DEVICE, "difficulty": 4,
                                                              "access": "inaccessible", "tags": ["sealed", "cameras"]})
        self.assertEqual(status, 200, data)
        self.assertEqual((data["summary"]["n"], data["summary"]["difficulty"]), (1, 4))
        self.assertEqual(data["mine"]["tags"], ["cameras", "sealed"])
        self.assertEqual([h["access"] for h in data["summary"]["history"]], ["inaccessible"])
        self.assertEqual(call(self.port, "POST", "/api/update", {"source": "osm"})[0], 403)   # still read-only
        # Straight onto the map, for the hover; and at the next rebuild, the newest word on the place.
        self.assertEqual(self.updater.store.get_site(self.key)["report"]["inaccessible"], 1)
        self.updater.rebuild(force=True)
        site = self.updater.store.get_site(self.key)
        self.assertEqual((site["report"]["inaccessible"], site["reported_by"], site["reported_as"]),
                         (1, "reports", "visitor report"))
        # Asked for again, by this device and by someone else.
        _, again = call(self.port, "GET", f"/api/report?key={self.key}&device={DEVICE}")
        self.assertEqual(again["mine"]["difficulty"], 4)
        _, theirs = call(self.port, "GET", f"/api/report?key={self.key}&device=someone-else-entirely-0001")
        self.assertIsNone(theirs["mine"])
        # Which places this device has reported on: for the marks on its map. Only its own id tells.
        self.assertEqual(call(self.port, "GET", f"/api/report/mine?device={DEVICE}")[1], {"keys": [self.key]})
        self.assertEqual(call(self.port, "GET", "/api/report/mine?device=someone-else-entirely-0001")[1], {"keys": []})
        self.assertEqual(call(self.port, "GET", "/api/report/mine?device=short")[0], 400)
        # Nothing stored says which device it was.
        with self.updater.store.connect() as db:
            stored = " ".join(str(v) for row in db.execute("SELECT * FROM reports") for v in row)
        self.assertNotIn(DEVICE, stored)
        # Taken back.
        _, gone = call(self.port, "POST", "/api/report", {"key": self.key, "device": DEVICE, "clear": True})
        self.assertIsNone(gone["mine"])
        self.assertEqual(call(self.port, "GET", f"/api/report/mine?device={DEVICE}")[1], {"keys": []})

    def test_an_older_copy_rebuilding_the_map_doesnt_break_this_one(self):
        # A second, older bandobuddy on the same data swaps in a map without the newer columns.
        store = self.updater.store
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            kept = [r[1] for r in db.execute("PRAGMA table_info(sites)") if r[1] not in ("report", "reported")]
            db.execute(f"CREATE TABLE sites_old AS SELECT {', '.join(kept)} FROM sites")
            db.execute("DROP TABLE sites")
            db.execute("ALTER TABLE sites_old RENAME TO sites")
        status, data = call(self.port, "GET", "/api/list?weak=1")
        self.assertEqual(status, 200, data)              # the columns are put back, and the request carries on
        status, data = call(self.port, "POST", "/api/report", {"key": self.key, "device": DEVICE, "difficulty": 2})
        self.assertEqual(status, 200, data)
        call(self.port, "POST", "/api/report", {"key": self.key, "device": DEVICE, "clear": True})

    def test_wrong_choices_and_unknown_places(self):
        self.assertEqual(call(self.port, "POST", "/api/report",
                              {"key": self.key, "device": DEVICE, "difficulty": 9})[0], 400)
        self.assertEqual(call(self.port, "POST", "/api/report",
                              {"key": "nowhere:1", "device": DEVICE, "difficulty": 2})[0], 404)

    def test_one_address_cant_swamp_a_place(self):
        app = webapp.App(self.updater.store, self.updater, session_factory=lambda: self.session, read_only=True)
        with mock.patch.object(webapp, "REPORTS_PER_HOUR", 3):
            for _ in range(3):
                app._limit_reports("203.0.113.9")
            with self.assertRaises(webapp.ApiError) as raised:
                app._limit_reports("203.0.113.9")
            self.assertEqual(raised.exception.status, 429)
            app._limit_reports("198.51.100.4")        # someone else is fine
        self.assertGreater(REPORTS_PER_HOUR, 3)
