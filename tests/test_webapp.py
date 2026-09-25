import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from bandobuddy import __version__, cli, webapp

from tests.fakes import FakeSession
from tests.test_pipeline import HAVE_OSMIUM, make_updater


@unittest.skipUnless(HAVE_OSMIUM, "pyosmium not installed")
class WebAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.session = FakeSession()
        cls.updater = make_updater(cls.tmp, cls.session)
        cls.updater.run("osm")
        cls.updater.run("wikidata")
        cls.app = webapp.App(cls.updater.store, cls.updater, session_factory=lambda: cls.session)
        cls.server = webapp.make_server(cls.app, port=0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        ctype = resp.getheader("Content-Type") or ""
        payload = json.loads(raw) if ctype.startswith("application/json") else raw
        return resp.status, payload, resp

    def test_page(self):
        status, page, resp = self.request("GET", "/")
        self.assertEqual(status, 200)
        page = page.decode()
        self.assertNotIn("__BOOT__", page)
        self.assertIn('"categories"', page)
        self.assertIn("leaflet", page)
        self.assertNotIn("googleapis", page)  # no Google anywhere
        self.assertIn("watchPosition", page)  # "use my location"
        self.assertNotIn("min_score", page)
        self.assertEqual(resp.getheader("Referrer-Policy"), "strict-origin-when-cross-origin")

    def test_it_installs_as_an_app(self):
        status, page, _ = self.request("GET", "/")
        page = page.decode()
        self.assertIn('rel="manifest"', page)
        self.assertIn('navigator.serviceWorker.register("/sw.js")', page)

        status, raw, resp = self.request("GET", "/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith("application/manifest+json"))
        manifest = json.loads(raw)
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual({i["sizes"] for i in manifest["icons"]}, {"192x192", "512x512"})
        self.assertIn("maskable", [i.get("purpose") for i in manifest["icons"]])

        status, sw, resp = self.request("GET", "/sw.js")
        self.assertEqual(status, 200)
        self.assertNotIn(b"__VERSION__", sw)             # stamped, so a release retires old caches
        self.assertIn(__version__.encode(), sw)
        self.assertEqual(resp.getheader("Cache-Control"), "no-cache")
        self.assertEqual(resp.getheader("Service-Worker-Allowed"), "/")

        for path in ("/static/icon-192.png", "/static/icon-512.png", "/static/icon-maskable-512.png",
                     "/static/apple-touch-icon.png"):
            status, body, resp = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertEqual(resp.getheader("Content-Type"), "image/png", path)
            self.assertTrue(body.startswith(b"\x89PNG"), path)
            self.assertIn("max-age", resp.getheader("Cache-Control"), path)

        # Nothing else is served from the package, however the path is dressed up.
        for path in ("/static/../webapp.py", "/static/sw.js", "/static/nope.png"):
            self.assertEqual(self.request("GET", path)[0], 404, path)

    def test_list_nearest_first_with_filters(self):
        view = "bbox=-0.2,51.4,0.0,51.6"
        status, data, _ = self.request("GET", f"/api/list?{view}&weak=1&near=51.5,-0.12")
        self.assertEqual(status, 200)
        names = [s["name"] for s in data["sites"]]
        self.assertTrue({"Old Mill", "Hillside Quarry", "Hill Tunnel", "St Agnes Hospital", "Corner Bakery"} <= set(names))
        self.assertEqual(data["total"], len(data["sites"]))
        self.assertEqual(names[0], "Unnamed bunker")  # right on the spot
        dists = [s["distance_m"] for s in data["sites"]]
        self.assertEqual(dists, sorted(dists))
        first = data["sites"][0]
        self.assertTrue({"condition", "category", "strength", "summary"} <= set(first))
        self.assertNotIn("tier", first)

        # Weak leads (a closed shop unit here) only when asked for.
        _, data, _ = self.request("GET", f"/api/list?{view}")
        self.assertNotIn("Corner Bakery", {s["name"] for s in data["sites"]})
        _, data, _ = self.request("GET", f"/api/list?{view}&weak=1&sort=evidence")
        scores = [s["score"] for s in data["sites"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertNotIn("distance_m", data["sites"][0])

        _, data, _ = self.request("GET", f"/api/list?{view}&weak=1&categories=military")
        self.assertEqual({s["category"] for s in data["sites"]}, {"military"})
        _, data, _ = self.request("GET", f"/api/list?{view}&weak=1&sources=wikidata")
        self.assertTrue(all("wikidata" in s["sources"] for s in data["sites"]))
        _, data, _ = self.request("GET", "/api/list?bbox=10,10,11,11&weak=1")
        self.assertEqual(data["total"], 0)
        _, data, _ = self.request("GET", "/api/list?weak=1&added_since=2000-01-01T00:00:00Z&limit=0")
        self.assertEqual((data["total"], data["sites"]), (0, []))  # first build: nothing is "new"
        for bad in ("bbox=nonsense", "near=here", "limit=lots"):
            self.assertEqual(self.request("GET", f"/api/list?{bad}")[0], 400, bad)

    def test_map_view(self):
        status, data, _ = self.request("GET", "/api/map?bbox=-0.2,51.4,0.0,51.6&zoom=12&weak=1")
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "sites")
        self.assertEqual(len(data["sites"]), data["total"])
        self.assertEqual(self.request("GET", "/api/map?zoom=12")[0], 400)  # needs a bbox
        self.assertEqual(self.request("GET", "/api/map?bbox=-0.2,51.4,0.0,51.6&zoom=x")[0], 400)

    def test_site_detail(self):
        _, data, _ = self.request("GET", "/api/list?weak=1&q=Old%20Mill")
        key = data["sites"][0]["key"]
        status, site, _ = self.request("GET", f"/api/site/{key}")
        self.assertEqual(status, 200)
        self.assertEqual(site["condition"], "Ruin")
        self.assertEqual(site["detail"]["osm"][0]["osm_id"], "way/100")
        self.assertEqual(site["links"]["osm_element"], "https://www.openstreetmap.org/way/100")
        self.assertIn("streetview", site["links"])
        self.assertEqual(self.request("GET", "/api/site/osm:way/999999")[0], 404)

    def test_photos_and_search(self):
        _, data, _ = self.request("GET", "/api/photos?lat=51.5&lng=-0.12")
        self.assertEqual(data["photos"][0]["id"], "p1")  # facing the site beats merely closer
        self.assertTrue(data["photos"][0]["facing"])
        _, hit, _ = self.request("GET", "/api/search?q=Bradford%20on%20Avon")
        self.assertAlmostEqual(hit["lat"], 51.3467)
        self.assertEqual(self.request("GET", "/api/search?q=nowhere")[0], 404)

    def test_exports(self):
        for fmt, marker in (("csv", b"name,category,condition"), ("kml", b"(Ruin)</name>"),
                            ("gpx", b"<type>Industrial</type>")):
            status, body, resp = self.request("GET", f"/api/export?format={fmt}&bbox=-0.2,51.4,0.0,51.6&weak=1")
            self.assertEqual(status, 200, fmt)
            self.assertIn(marker, body)
            self.assertIn("attachment", resp.getheader("Content-Disposition"))

    def test_status_update_and_settings(self):
        status, st, _ = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(set(st["sources"]), {"osm", "wikidata"})
        self.assertGreater(st["sites"]["total"], 0)
        self.assertIsNotNone(st["sources"]["osm"]["last_update"])

        _, st, _ = self.request("POST", "/api/settings", {"update_days": 3, "auto_update": False})
        self.assertEqual(st["update_days"], 3)
        self.assertFalse(st["auto_update"])
        self.assertEqual(self.request("POST", "/api/settings", {"update_days": "x"})[0], 400)
        self.request("POST", "/api/settings", {"update_days": 7, "auto_update": True})

        _, started, _ = self.request("POST", "/api/update", {"source": "wikidata"})
        self.assertEqual(started["started"], ["wikidata"])
        deadline = time.time() + 10
        while time.time() < deadline and self.updater.state["wikidata"]["running"]:
            time.sleep(0.05)
        self.assertFalse(self.updater.state["wikidata"]["running"])
        self.assertEqual(self.request("POST", "/api/update", {"source": "nope"})[0], 400)

    def test_rejects_foreign_hosts_and_origins(self):
        self.assertEqual(self.request("GET", "/api/status", headers={"Host": "evil.example:80"})[0], 403)
        self.assertEqual(self.request("POST", "/api/update", {"source": "osm"},
                                      headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/update", headers={"Content-Type": "text/plain"})[0], 415)


@unittest.skipUnless(HAVE_OSMIUM, "pyosmium not installed")
class CliTests(unittest.TestCase):
    def test_update_and_export(self):
        tmp = Path(tempfile.mkdtemp())
        up = make_updater(tmp)
        up.run("osm")
        out = tmp / "spots.gpx"
        # The export reads the same database the updater wrote.
        export = ["--data-dir", str(tmp), "export", "--near", "51.5,-0.12", "--radius", "5", "--format", "gpx", "-o", str(out)]
        self.assertEqual(cli.main(export), 0)
        gpx = out.read_text(encoding="utf-8")
        self.assertIn("Hill Tunnel", gpx)
        self.assertNotIn("Corner Bakery", gpx)  # a weak lead
        self.assertEqual(cli.main(export + ["--include-weak"]), 0)
        self.assertIn("Corner Bakery", out.read_text(encoding="utf-8"))

    def test_parser(self):
        args = cli.build_parser().parse_args(["--port", "9000", "update", "--source", "osm"])
        self.assertEqual((args.port, args.command, args.source), (9000, "update", "osm"))


def call(port, method, path, body=None, host=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{port}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    conn.request(method, path, body=data, headers=h)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    is_json = (resp.getheader("Content-Type") or "").startswith("application/json")
    return resp.status, json.loads(raw) if is_json else raw


@unittest.skipUnless(HAVE_OSMIUM, "pyosmium not installed")
class ServingModesTests(unittest.TestCase):
    """How the server behaves in a container / behind an ingress."""

    @classmethod
    def setUpClass(cls):
        cls.session = FakeSession()
        cls.updater = make_updater(Path(tempfile.mkdtemp()), cls.session)
        cls.updater.run("osm")
        cls.servers = []

    @classmethod
    def tearDownClass(cls):
        for server in cls.servers:
            server.shutdown()
            server.server_close()

    def start(self, read_only=False, allowed_hosts=()):
        app = webapp.App(self.updater.store, self.updater, session_factory=lambda: self.session, read_only=read_only)
        server = webapp.make_server(app, 0, allowed_hosts=allowed_hosts)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return server.server_address[1]

    def test_host_names(self):
        port = self.start()
        self.assertEqual(call(port, "GET", "/api/status", host="bandobuddy.example.org")[0], 403)
        self.assertEqual(call(port, "GET", "/api/status", host="localhost:9999")[0], 200)  # any published port
        # A phone on the same network, by IP or machine name, with full control.
        self.assertEqual(call(port, "GET", "/api/status", host="192.168.1.20:8642")[0], 200)
        self.assertEqual(call(port, "GET", "/api/status", host="desktop-4r6u799:8642")[0], 200)
        status, _ = call(port, "POST", "/api/settings", {"update_days": 7}, host="192.168.1.20:8642",
                         headers={"Origin": "http://192.168.1.20:8642"})
        self.assertEqual(status, 200)
        self.assertEqual(call(port, "POST", "/api/settings", {"update_days": 7}, host="192.168.1.20:8642",
                              headers={"Origin": "https://evil.example.org"})[0], 403)
        port = self.start(allowed_hosts=["bandobuddy.example.org"])
        self.assertEqual(call(port, "GET", "/api/status", host="bandobuddy.example.org")[0], 200)
        self.assertEqual(call(port, "GET", "/api/status", host="evil.example.org")[0], 403)
        status, _ = call(port, "POST", "/api/settings", {"update_days": 7}, host="bandobuddy.example.org",
                         headers={"Origin": "https://bandobuddy.example.org"})
        self.assertEqual(status, 200)

    def test_healthz_answers_probes_from_any_host(self):
        port = self.start()
        status, data = call(port, "GET", "/healthz", host="10.42.0.7:8642")  # kubelet uses the pod IP
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertGreater(data["sites"], 0)

    def test_read_only_public_mode(self):
        port = self.start(read_only=True)
        status, data = call(port, "POST", "/api/update", {"source": "osm"})
        self.assertEqual(status, 403)
        self.assertIn("read-only", data["error"])
        self.assertEqual(call(port, "POST", "/api/settings", {"auto_update": False})[0], 403)
        self.assertIn(b'"read_only": true', call(port, "GET", "/")[1])
        _, st = call(port, "GET", "/api/status")
        self.assertIsNone(st["data_dir"])
        self.assertEqual(st["log"], [])
        self.assertEqual(call(port, "GET", "/api/list?weak=1")[0], 200)  # browsing still works

    def test_search_results_are_cached(self):
        port = self.start()
        before = sum("nominatim" in url for _, url in self.session.calls)
        for _ in range(3):
            self.assertEqual(call(port, "GET", "/api/search?q=Bradford%20on%20Avon")[0], 200)
        self.assertEqual(sum("nominatim" in url for _, url in self.session.calls) - before, 1)


class ContainerConfigTests(unittest.TestCase):
    def test_environment_configures_the_server(self):
        env = {"BANDOBUDDY_HOST": "0.0.0.0", "BANDOBUDDY_PORT": "9001", "BANDOBUDDY_PUBLIC": "true",
               "BANDOBUDDY_NO_BROWSER": "1", "BANDOBUDDY_NO_AUTO_UPDATE": "no"}
        with mock.patch.dict(os.environ, env):
            args = cli.build_parser().parse_args([])
        self.assertEqual((args.host, args.port, args.public, args.no_browser, args.no_auto_update),
                         ("0.0.0.0", 9001, True, True, False))

    def test_listens_on_the_whole_network_by_default(self):
        env = {k: v for k, v in os.environ.items() if k != "BANDOBUDDY_HOST"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(cli.build_parser().parse_args([]).host, "0.0.0.0")
            self.assertEqual(cli.build_parser().parse_args(["--host", "127.0.0.1"]).host, "127.0.0.1")

    def test_lan_hosts_are_answered_but_public_names_are_not(self):
        for host in ("192.168.1.20", "10.0.0.5", "[fe80::1]", "desktop-4r6u799", "nas.local", "pi.lan",
                     "box.home.arpa", "localhost"):
            self.assertTrue(webapp.is_lan_host(host), host)
        # Public names could point anywhere: that's how DNS rebinding works (192.168.1.20.nip.io included).
        for host in ("evil.example.org", "192.168.1.20.nip.io", "bandobuddy.example.org", ""):
            self.assertFalse(webapp.is_lan_host(host), host)

    def test_lan_addresses_are_private_ones_only(self):
        infos = [(socket.AF_INET, 0, 0, "", (ip, 0)) for ip in ("127.0.0.1", "169.254.1.2", "8.8.8.8", "10.0.0.5")]
        with mock.patch("socket.socket", side_effect=OSError), \
                mock.patch("socket.getaddrinfo", return_value=infos):
            self.assertEqual(webapp.lan_addresses(), ["10.0.0.5"])
        for ip in webapp.lan_addresses():  # the real machine: whatever it has, it's private
            self.assertTrue(ip.startswith(("10.", "172.", "192.168.")), ip)

    def test_host_only(self):
        self.assertEqual(webapp.host_only("Bandobuddy.Example.org:443"), "bandobuddy.example.org")
        self.assertEqual(webapp.host_only("[::1]:8642"), "[::1]")


@unittest.skipIf(os.name == "nt", "SIGTERM can only be delivered to a Python handler on POSIX")
class ShutdownTests(unittest.TestCase):
    def test_sigterm_stops_the_server_cleanly(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        proc = subprocess.Popen(
            [sys.executable, "-m", "bandobuddy", "--data-dir", tempfile.mkdtemp(), "--port", str(port),
             "--no-browser", "--no-auto-update"],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    if call(port, "GET", "/healthz")[0] == 200:
                        break
                except OSError:
                    time.sleep(0.2)
            proc.send_signal(signal.SIGTERM)
            out, _ = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("Stopping", out)


if __name__ == "__main__":
    unittest.main()
