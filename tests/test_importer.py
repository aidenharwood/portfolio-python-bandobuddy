import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from bandobuddy import cli, importer
from bandobuddy.config import DB_NAME
from bandobuddy.sites import build_sites
from bandobuddy.store import Store

CSV = """Name,Type,Latitude,Longitude,Notes,URL
Alderbury ROC Post,ROC Monitoring Post,51.04944,-1.72583,Hatch open; all surface features intact,https://example.org/1
Britford Pillbox,Pillbox,51.05500,-1.79000,Type 24,
Nowhere,,,,no position,
"""

GPX = """<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <wpt lat="51.04944" lon="-1.72583"><name>Alderbury ROC Post</name><desc>Underground monitoring post</desc></wpt>
  <wpt><name>No position</name></wpt>
</gpx>
"""

KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
  <Placemark><name>Alderbury ROC Post</name><description>ROC underground monitoring post</description>
    <Point><coordinates>-1.72583,51.04944,0</coordinates></Point></Placemark>
</Document></kml>
"""

GEOJSON = """{"type": "FeatureCollection", "features": [
  {"type": "Feature", "properties": {"name": "Alderbury ROC Post", "type": "observation post"},
   "geometry": {"type": "Point", "coordinates": [-1.72583, 51.04944]}},
  {"type": "Feature", "properties": {"name": "A line"}, "geometry": {"type": "LineString", "coordinates": []}}
]}"""


def write(tmp: Path, name: str, text: str) -> Path:
    path = tmp / name
    path.write_text(text, encoding="utf-8")
    return path


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_every_format_finds_the_same_place(self):
        files = {"sites.csv": CSV, "sites.gpx": GPX, "sites.kml": KML, "sites.geojson": GEOJSON}
        for name, text in files.items():
            places = importer.read_file(write(self.tmp, name, text))
            first = places[0]
            self.assertEqual(first["name"], "Alderbury ROC Post", name)
            self.assertAlmostEqual(first["lat"], 51.04944, msg=name)
            self.assertAlmostEqual(first["lng"], -1.72583, msg=name)
            self.assertTrue(all(p["lat"] and p["lng"] for p in places), name)  # rows without a position are dropped
        self.assertEqual(len(importer.read_file(self.tmp / "sites.csv")), 2)

    def test_kmz_is_a_zipped_kml(self):
        path = self.tmp / "dob.kmz"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("doc.kml", KML)
        path.write_bytes(buf.getvalue())
        self.assertEqual(importer.read_file(path)[0]["name"], "Alderbury ROC Post")

    def test_unreadable_files_say_why(self):
        for name, text, message in (("sites.txt", "hello", "Don't know how to read"),
                                    ("sites.geojson", "{oops", "Couldn't read"),
                                    ("sites.kml", "<kml", "Couldn't read")):
            with self.assertRaises(importer.BadFile) as caught:
                importer.read_file(write(self.tmp, name, text))
            self.assertIn(message, str(caught.exception))

    def test_records_are_judged_like_a_register(self):
        items = importer.to_items(importer.read_file(write(self.tmp, "sites.csv", CSV)), "defence-of-britain")
        roc, pillbox = items
        self.assertEqual(roc["kind"], "observation post")     # "ROC Monitoring Post"
        self.assertEqual(roc["evidence"], 'From your import "defence-of-britain": ROC Monitoring Post')
        self.assertEqual(roc["url"], "https://example.org/1")
        self.assertEqual(pillbox["kind"], "military structure")
        self.assertTrue(all(i["dataset"] == "imported" for i in items))
        # Your notes saying it's gone keep the place, but only as a weak lead.
        gone = importer.to_items([importer._place("Old Mill", 51.0, -1.0, "mill", "demolished in 2003")], "notes")
        self.assertEqual(gone[0]["weight"], importer.GONE_WEIGHT)
        # Importing the same file again updates the same places rather than doubling them up.
        again = importer.to_items(importer.read_file(self.tmp / "sites.csv"), "defence-of-britain")
        self.assertEqual([i["ref"] for i in items], [i["ref"] for i in again])


class ImportCommandTests(unittest.TestCase):
    def test_import_list_and_forget(self):
        tmp = Path(tempfile.mkdtemp())
        path = write(tmp, "defence of britain.csv", CSV)
        self.assertEqual(cli.main(["--data-dir", str(tmp), "import", str(path)]), 0)

        store = Store(tmp / DB_NAME)
        self.assertEqual(store.import_labels(), {"defence-of-britain": 2})
        sites = {s["name"]: s for s in store.full_sites(min_score=0)}
        roc = sites["Alderbury ROC Post"]
        self.assertEqual((roc["sources"], roc["category"], roc["condition"]), ("imported", "bunkers", "Old military"))
        self.assertEqual(roc["reasons"], ['From your import "defence-of-britain": ROC Monitoring Post'])

        self.assertEqual(cli.main(["--data-dir", str(tmp), "import", "--list"]), 0)
        self.assertEqual(cli.main(["--data-dir", str(tmp), "import", "--forget", "defence-of-britain"]), 0)
        self.assertEqual(store.import_labels(), {})
        build_sites(store)
        self.assertEqual(store.full_sites(min_score=0), [])

    def test_a_missing_or_odd_file_is_reported(self):
        tmp = Path(tempfile.mkdtemp())
        self.assertEqual(cli.main(["--data-dir", str(tmp), "import", str(tmp / "nope.csv")]), 1)
        self.assertEqual(cli.main(["--data-dir", str(tmp), "import"]), 2)  # nothing to do


if __name__ == "__main__":
    unittest.main()
