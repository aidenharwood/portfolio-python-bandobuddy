"""Offline stand-ins for the free services bandobuddy uses, plus a tiny OSM file."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import requests

from bandobuddy.geo import offset

CENTER = (51.5000, -0.1200)
EXTRACT_URL = "https://download.example.org/test-area.osm"

# A hand-made OSM file: two dead places as points, one as a building outline, one as a multipolygon,
# an abandoned railway tunnel, and things that must be ignored.
OSM_XML = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="bandobuddy-tests">
  <node id="1" version="1" timestamp="2024-01-01T00:00:00Z" lat="51.5000" lon="-0.1200">
    <tag k="military" v="bunker"/><tag k="bunker_type" v="pillbox"/></node>
  <node id="2" version="1" timestamp="2024-01-01T00:00:00Z" lat="51.5050" lon="-0.1150">
    <tag k="disused:shop" v="bakery"/><tag k="name" v="Corner Bakery"/></node>
  <node id="3" version="1" timestamp="2024-01-01T00:00:00Z" lat="51.5060" lon="-0.1100">
    <tag k="amenity" v="cafe"/><tag k="name" v="Busy Cafe"/></node>
  <node id="4" version="1" timestamp="2024-01-01T00:00:00Z" lat="51.4950" lon="-0.1250">
    <tag k="building" v="yes"/><tag k="name" v="Derelict Barn"/></node>
  <node id="10" version="1" timestamp="2024-01-01T00:00:00Z" lat="51.5100" lon="-0.1300"/>
  <node id="11" version="1" timestamp="2024-01-01T00:00:00Z" lat="51.5100" lon="-0.1290"/>
  <node id="12" version="1" timestamp="2024-01-01T00:00:00Z" lat="51.5110" lon="-0.1290"/>
  <node id="13" version="1" timestamp="2024-01-01T00:00:00Z" lat="51.5110" lon="-0.1300"/>
  <way id="100" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="10"/><nd ref="11"/><nd ref="12"/><nd ref="13"/><nd ref="10"/>
    <tag k="building" v="ruins"/><tag k="name" v="Old Mill"/></way>
  <node id="20" version="1" timestamp="2024-01-01T00:00:00Z" lat="51.4900" lon="-0.1100"/>
  <node id="21" version="1" timestamp="2024-01-01T00:00:00Z" lat="51.4900" lon="-0.1080"/>
  <node id="22" version="1" timestamp="2024-01-01T00:00:00Z" lat="51.4920" lon="-0.1080"/>
  <way id="101" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="20"/><nd ref="21"/><nd ref="22"/><nd ref="20"/></way>
  <relation id="200" version="1" timestamp="2024-01-01T00:00:00Z">
    <member type="way" ref="101" role="outer"/>
    <tag k="type" v="multipolygon"/><tag k="abandoned:amenity" v="hospital"/><tag k="name" v="St Agnes Hospital"/></relation>
  <node id="30" version="1" timestamp="2024-01-01T00:00:00Z" lat="51.5200" lon="-0.1000"/>
  <node id="31" version="1" timestamp="2024-01-01T00:00:00Z" lat="51.5220" lon="-0.1000"/>
  <way id="102" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="30"/><nd ref="31"/><tag k="railway" v="abandoned"/><tag k="tunnel" v="yes"/><tag k="name" v="Hill Tunnel"/></way>
  <way id="103" version="1" timestamp="2024-01-01T00:00:00Z">
    <nd ref="30"/><nd ref="31"/><tag k="abandoned:railway" v="rail"/></way>
</osm>
"""


def write_osm(path: Path, text: str = OSM_XML) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class FakeResp:
    def __init__(self, status: int = 200, json_data=None, content: bytes = b"", text: str = "", headers=None):
        self.status_code = status
        self._json = json_data
        self.content = content
        self.text = text or (str(json_data) if json_data is not None else "")
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, size):
        for i in range(0, len(self.content), size):
            yield self.content[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def wd_item(qid, label, north, east, types="", states="", ended=None, wiki=False, at=None):
    lat, lng = at or offset(*CENTER, north, east)
    return {"qid": qid, "label": label, "lat": lat, "lng": lng, "types": types, "states": states,
            "ended": ended, "wiki": ("https://en.wikipedia.org/wiki/" + label.replace(" ", "_")) if wiki else None}


class FakeSession:
    """Answers like Wikidata, Wikipedia, Panoramax, Nominatim and a Geofabrik-style download server."""

    def __init__(self, osm_xml: str = OSM_XML, split_wider_than: float = 0.3):
        self.calls: list[tuple[str, str]] = []
        self.osm_bytes = osm_xml.encode("utf-8")
        self.split_wider_than = split_wider_than  # Wikidata boxes wider than this "time out"
        self.busy_next = 0                        # answer the next N Wikidata queries with HTTP 429
        self.wikidata = [
            wd_item("Q1", "Hillside Quarry", 600, 0, "quarry|protected area", wiki=True),
            wd_item("Q2", "Garden Wall At Foo House", 650, 50, "wall"),
            wd_item("Q3", "Old Town railway station", 700, 0, "railway station", states="in use"),
            wd_item("Q4", "Old Mill", 0, 0, "mill", ended="1950-01-01T00:00:00Z", at=(51.5104, -0.1296)),  # OSM way 100
            wd_item("Q5", "Parkside Tunnel", -700, 300, "railway tunnel", states="decommissioned", wiki=True),
            wd_item("Q8", "Knocked Down Mill", 300, -600, "mill", wiki=True),
        ]
        self.intros = {
            "Hillside Quarry": "Hillside Quarry is a disused limestone mine near the town. It is a bat roost.",
            "Parkside Tunnel": "Parkside Tunnel is on the now-closed railway. It reopened in 2013 as a cycle path.",
            "Knocked Down Mill": "Knocked Down Mill was a woollen mill. It was demolished in 1972.",
        }

    # -- helpers --------------------------------------------------------------------------------
    def _wikidata(self, query: str) -> FakeResp:
        if self.busy_next:
            self.busy_next -= 1
            return FakeResp(429, text="Too Many Requests", headers={"Retry-After": "0"})
        sw = re.search(r'cornerSouthWest "Point\(([-\d.]+) ([-\d.]+)\)"', query)
        ne = re.search(r'cornerNorthEast "Point\(([-\d.]+) ([-\d.]+)\)"', query)
        w, s = float(sw.group(1)), float(sw.group(2))
        e, n = float(ne.group(1)), float(ne.group(2))
        if e - w > self.split_wider_than:
            return FakeResp(500, text="java.util.concurrent.TimeoutException")
        bindings = []
        for it in self.wikidata:
            if s <= it["lat"] < n and w <= it["lng"] < e:
                b = {"item": {"value": f"http://www.wikidata.org/entity/{it['qid']}"}, "label": {"value": it["label"]},
                     "coord": {"value": f"Point({it['lng']} {it['lat']})"}, "types": {"value": it["types"]},
                     "states": {"value": it["states"]}}
                if it["ended"]:
                    b["ended"] = {"value": it["ended"]}
                if it["wiki"]:
                    b["wiki"] = {"value": it["wiki"]}
                bindings.append(b)
        return FakeResp(200, {"results": {"bindings": bindings}})

    def get(self, url, params=None, headers=None, timeout=None, stream=False):
        self.calls.append(("GET", url))
        if url.endswith("query.wikidata.org/sparql"):
            return self._wikidata(params["query"])
        if "wikipedia.org/w/api.php" in url:
            pages = {str(i): {"title": t, "extract": self.intros.get(t, "")}
                     for i, t in enumerate(params["titles"].split("|"))}
            return FakeResp(200, {"query": {"pages": pages}})
        if url == EXTRACT_URL:
            body = self.osm_bytes
            start = 0
            rng = (headers or {}).get("Range")
            if rng:
                start = int(rng.split("=")[1].rstrip("-"))
                return FakeResp(206, content=body[start:], headers={"Content-Length": str(len(body) - start)})
            return FakeResp(200, content=body, headers={"Content-Length": str(len(body)),
                                                        "Last-Modified": "Mon, 21 Sep 2026 23:00:00 GMT"})
        if url == EXTRACT_URL + ".md5":
            return FakeResp(200, text=f"{hashlib.md5(self.osm_bytes).hexdigest()}  test-area.osm")
        if "panoramax" in url:
            lat, lng = CENTER
            return FakeResp(200, {"features": [
                {"id": "p1", "geometry": {"type": "Point", "coordinates": [lng, lat - 0.0003]},
                 "properties": {"datetime": "2024-02-17T12:00:00+00:00", "view:azimuth": 0,
                                "geovisio:producer": "someone", "license": "CC-BY-SA-4.0"},
                 "assets": {"thumb": {"href": "https://img.example/p1/thumb.jpg"}, "sd": {"href": "https://img.example/p1/sd.jpg"}}},
                {"id": "p2", "geometry": {"type": "Point", "coordinates": [lng, lat - 0.0001]},
                 "properties": {"datetime": "2023-05-01T12:00:00+00:00", "view:azimuth": 180},
                 "assets": {"thumb": {"href": "https://img.example/p2/thumb.jpg"}}},
            ]})
        if "nominatim" in url:
            if params["q"] == "nowhere":
                return FakeResp(200, [])
            return FakeResp(200, [{"display_name": "Bradford on Avon, Wiltshire", "lat": "51.3467", "lon": "-2.2504",
                                   "boundingbox": ["51.33", "51.36", "-2.27", "-2.23"]}])
        return FakeResp(404, text="unknown")
