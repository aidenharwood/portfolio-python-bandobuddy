"""Welsh LiDAR tiles: read a cloud-optimised GeoTIFF a range at a time, shade it, serve map tiles."""
import array
import math
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from bandobuddy import lidar
from bandobuddy.geo import bng_to_wgs84, haversine_m, wgs84_to_bng

WEST, NORTH = 300000.0, 250000.0     # mid Wales
SIZE = 512                           # metres (and pixels) a side
HILL = (WEST + 256, NORTH - 256)     # a hill in the middle, 40 m high and 150 m across


def height(e, n):
    d = math.hypot(e - HILL[0], n - HILL[1])
    return 100 + max(0.0, 40 * (1 - d / 150))


def cog_bytes() -> bytes:
    """A little GeoTIFF like the Welsh one: float32 heights in deflated 256-pixel tiles, 1 m pixels,
    a zoomed-out copy at 2 m, and a no-data corner."""
    def level(scale):
        pixels = SIZE // scale
        tiles = []
        for ty in range(-(-pixels // 256)):
            for tx in range(-(-pixels // 256)):
                values = array.array("f")
                for r in range(256):
                    for c in range(256):
                        col, row = tx * 256 + c, ty * 256 + r
                        e, n = WEST + (col + 0.5) * scale, NORTH - (row + 0.5) * scale
                        corner = e > WEST + 448 and n > NORTH - 64
                        values.append(-9999.0 if corner or col >= pixels or row >= pixels else height(e, n))
                tiles.append(zlib.compress(values.tobytes()))
        return pixels, tiles
    levels = [level(1), level(2)]

    # Layout: header, one IFD per level, the tag values too big for their entries, then the tiles.
    def build(tiles_at: int) -> bytes:
        def entries_for(i, ext, offsets, pixels, tiles):
            short = lambda v: struct.pack("<HH", v, 0)
            longs = lambda v: struct.pack("<I", v)
            entries = [(254, 4, 1, longs(1 if i else 0)), (256, 4, 1, longs(pixels)), (257, 4, 1, longs(pixels)),
                       (258, 3, 1, short(32)), (259, 3, 1, short(8)), (262, 3, 1, short(1)), (277, 3, 1, short(1)),
                       (284, 3, 1, short(1)), (317, 3, 1, short(1)), (322, 3, 1, short(256)), (323, 3, 1, short(256)),
                       (339, 3, 1, short(3)), (42113, 2, 6, longs(ext(b"-9999\0")))]
            if len(tiles) == 1:     # one tile: its offset and size sit in the entry itself
                entries += [(324, 4, 1, longs(offsets[0])), (325, 4, 1, longs(len(tiles[0])))]
            else:
                entries += [(324, 4, len(tiles), longs(ext(struct.pack(f"<{len(tiles)}I", *offsets)))),
                            (325, 4, len(tiles), longs(ext(struct.pack(f"<{len(tiles)}I", *map(len, tiles)))))]
            if i == 0:
                entries += [(33550, 12, 3, longs(ext(struct.pack("<3d", 1.0, 1.0, 0.0)))),
                            (33922, 12, 6, longs(ext(struct.pack("<6d", 0, 0, 0, WEST, NORTH, 0))))]
            return sorted(entries)

        counts = [len(entries_for(i, lambda b: 0, [0] * len(t), px, t)) for i, (px, t) in enumerate(levels)]
        ifds_at = [8]
        for n in counts[:-1]:
            ifds_at.append(ifds_at[-1] + 2 + 12 * n + 4)
        extra_at = ifds_at[-1] + 2 + 12 * counts[-1] + 4
        extra = bytearray()

        def ext(blob):
            where = extra_at + len(extra)
            extra.extend(blob + (b"\0" if len(blob) % 2 else b""))
            return where
        out = bytearray(b"II*\0" + struct.pack("<I", 8))
        at = tiles_at
        for i, (pixels, tiles) in enumerate(levels):
            offsets = []
            for t in tiles:
                offsets.append(at)
                at += len(t)
            entries = entries_for(i, ext, offsets, pixels, tiles)
            out += struct.pack("<H", len(entries))
            for tag, typ, count, val in entries:
                out += struct.pack("<HHI", tag, typ, count) + val
            out += struct.pack("<I", ifds_at[i + 1] if i + 1 < len(levels) else 0)
        return bytes(out + extra)

    head = build(len(build(0)))
    return head + b"".join(t for _, tiles in levels for t in tiles)


class Ranges:
    """An HTTP server holding one file, answering range requests, counting them."""
    def __init__(self, data):
        self.data, self.calls = data, 0

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        start, end = (int(v) for v in headers["Range"].split("=")[1].split("-"))
        return type("R", (), {"status_code": 206, "content": self.data[start:end + 1]})()


def decode_png(png: bytes):
    """(grey, alpha) rows of a greyscale-and-alpha PNG with no row filters, as written here."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    at, idat, width = 8, b"", None
    while at < len(png):
        n = struct.unpack(">I", png[at:at + 4])[0]
        kind, data = png[at + 4:at + 8], png[at + 8:at + 8 + n]
        if kind == b"IHDR":
            width = struct.unpack(">I", data[:4])[0]
        elif kind == b"IDAT":
            idat += data
        at += 12 + n
    raw = zlib.decompress(idat)
    rows = [raw[i * (1 + 2 * width) + 1:(i + 1) * (1 + 2 * width)] for i in range(len(raw) // (1 + 2 * width))]
    return [(row[0::2], row[1::2]) for row in rows]


def tile_at(lat, lng, z):
    n = 2 ** z
    x = (lng + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return int(x), int(y), (x % 1) * 256, (y % 1) * 256


class LidarTests(unittest.TestCase):
    def setUp(self):
        self.server = Ranges(cog_bytes())
        self.dir = Path(tempfile.mkdtemp())
        self.relief = lidar.WalesRelief(self.dir, lambda: self.server, url="https://x/wales.tif",
                                        extent=(WEST, NORTH - SIZE, WEST + SIZE, NORTH))

    def test_grid_references_both_ways(self):
        for lat, lng in [(51.069824, -1.797089), (53.0685, -4.0763), (60.155111, -1.143391)]:
            e, n = wgs84_to_bng(lat, lng)
            self.assertLess(haversine_m(*bng_to_wgs84(e, n), lat, lng), 0.01)
        self.assertLess(math.dist(wgs84_to_bng(51.069824, -1.797089), (414313, 130075)), 3)   # postcodes.io

    def test_reads_the_file_layout(self):
        cog = self.relief.cog()
        self.assertEqual([(lv.width, lv.scale) for lv in cog.levels], [(512, 1.0), (256, 2.0)])
        self.assertEqual((cog.west, cog.north, cog.nodata, cog.dtype), (WEST, NORTH, -9999.0, "f"))
        top = cog.tile(0, 1, 1)
        self.assertAlmostEqual(top[0], height(WEST + 256.5, NORTH - 256.5), places=3)
        self.assertEqual(cog.tile(1, 0, 0)[0], cog.tile(1, 0, 0)[0])   # the one-tile level, read from its entry
        self.assertIsNone(cog.tile(0, 5, 5))

    def pixel(self, e, n, z=16):
        """(grey, alpha) of the map pixel over a grid position, from whichever tile it falls in."""
        x, y, px, py = tile_at(*bng_to_wgs84(e, n), z)
        g, a = decode_png(self.relief.tile(z, x, y))[int(py)]
        return g[int(px)], a[int(px)]

    def test_a_hill_is_lit_from_the_north_west(self):
        (nw, nw_alpha), (se, _) = self.pixel(HILL[0] - 45, HILL[1] + 45), self.pixel(HILL[0] + 45, HILL[1] - 45)
        self.assertEqual(nw_alpha, 255)
        self.assertGreater(nw, 200)            # the slope facing the sun is bright...
        self.assertLess(se, 150)               # ...the far side in shadow
        flat, _ = self.pixel(WEST + 40, NORTH - 460)
        self.assertAlmostEqual(flat, 180, delta=3)   # level ground: a 45 degree sun

    def test_zoomed_in_past_the_data_it_stays_smooth(self):
        # Zoom 17 is finer than the 1 m data: heights are blended, so the slope shades in a smooth run
        # rather than a staircase of repeated values.
        (nw, _), (se, _) = self.pixel(HILL[0] - 45, HILL[1] + 45, 17), self.pixel(HILL[0] + 45, HILL[1] - 45, 17)
        self.assertGreater(nw, se)
        x, y, px, py = tile_at(*bng_to_wgs84(HILL[0] - 100, HILL[1]), 17)
        greys, _ = decode_png(self.relief.tile(17, x, y))[int(py)]
        run = greys[max(0, int(px) - 40):int(px)]
        self.assertLess(max(abs(a - b) for a, b in zip(run, run[1:])), 12)

    def test_no_data_is_see_through_and_tiles_are_kept(self):
        lat, lng = bng_to_wgs84(WEST + 480, NORTH - 30)    # inside the no-data corner
        x, y, px, py = tile_at(lat, lng, 16)
        png = self.relief.tile(16, x, y)
        g, a = decode_png(png)[int(py)]
        self.assertEqual(a[int(px)], 0)
        asked = self.server.calls
        self.assertEqual(self.relief.tile(16, x, y), png)   # from disk
        self.assertEqual(self.server.calls, asked)
        self.assertTrue(any(self.dir.rglob("*.png")))

    def test_the_sea_and_england_cost_nothing(self):
        x, y, _, _ = tile_at(51.45, -2.58, 14)              # Bristol
        self.assertIs(self.relief.tile(14, x, y), lidar.EMPTY_TILE)
        self.assertEqual(self.server.calls, 0)              # not even opened
        with self.assertRaises(ValueError):
            self.relief.tile(9, 0, 0)                       # too far out to be worth drawing

    def test_zoomed_out_uses_the_zoomed_out_copy(self):
        lat, lng = bng_to_wgs84(*HILL)
        x, y, _, _ = tile_at(lat, lng, 12)                  # about 23 m a pixel: the 2 m copy will do
        self.relief.tile(12, x, y)
        cog = self.relief.cog()
        self.assertTrue(all(key[0] == 1 for key in cog._tiles))


if __name__ == "__main__":
    unittest.main()
