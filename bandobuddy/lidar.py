"""LiDAR relief for Wales, drawn here.

England's and Scotland's LiDAR come straight from their governments' own map services, in the page.
Wales publishes its 1 m terrain model (2020-23) only as one cloud-optimised GeoTIFF: no map service, and
no CORS, so a browser can't read it. So bandobuddy reads just the parts of it a map tile needs (HTTP range
requests), shades them the way the other two services are shaded, and serves ordinary map tiles, kept on
disk so each is only drawn once.

Standard library only: the file is deflate-compressed 32-bit floats in 256-pixel tiles, with zoomed-out
copies built in, which zlib, struct and array can read.
"""
from __future__ import annotations

import array
import math
import os
import struct
import sys
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from .config import USER_AGENT
from .geo import wgs84_to_bng

WALES_DTM = "https://dmwproductionblob.blob.core.windows.net/cogs/lidar/wales_dtm_32bit_cog.tif"
WALES_BNG = (164993, 164000, 356000, 397000)   # the file's extent (W, S, E, N), to skip the sea without asking
TILE = 256
MIN_ZOOM, MAX_ZOOM = 12, 17    # 17 is about 0.7 m a pixel, past the 1 m data: further in, the map stretches these
LIGHT = (-0.5, 0.5, math.sqrt(0.5))   # sun in the north-west, 45 degrees up, as England's hillshade
EXAGGERATE = 2.0                      # steeper than life, so a bank or a filled shaft stands out
EARTH_M = 40075016.686
HEADER_BYTES = 1 << 16
TABLE_PAGE = 1 << 14
KEEP_SOURCE_TILES = 160        # decoded source tiles in memory: about 40 MB
KEEP_TILES_ON_DISK = 20000     # rendered tiles kept: about 1 GB at most
RENDERS_AT_ONCE = 4

_TIFF_TYPES = {1: "B", 2: "B", 3: "H", 4: "I", 6: "b", 7: "B", 8: "h", 9: "i", 11: "f", 12: "d", 16: "Q", 17: "q"}


class LidarError(RuntimeError):
    """The LiDAR file couldn't be read."""


@dataclass
class Level:
    width: int
    height: int
    tile_w: int
    tile_h: int
    offsets: tuple      # (where the tile offset table is, its entry type, how many entries[, the entries themselves])
    counts: tuple
    scale: float                       # metres per pixel

    @property
    def across(self) -> int:
        return -(-self.width // self.tile_w)


class Cog:
    """A cloud-optimised GeoTIFF read over HTTP, a range at a time."""

    def __init__(self, url: str, session_factory: Callable[[], requests.Session]):
        self.url = url
        self._session_factory = session_factory
        self._local = threading.local()
        self._lock = threading.Lock()
        self._pages: OrderedDict = OrderedDict()
        self._tiles: OrderedDict = OrderedDict()
        self._parse(self._read(0, HEADER_BYTES))

    # -- reading -------------------------------------------------------------------------------------
    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            self._local.session = self._session_factory()
        return self._local.session

    def _read(self, start: int, length: int) -> bytes:
        resp = self._session().get(self.url, headers={"Range": f"bytes={start}-{start + length - 1}",
                                                      "User-Agent": USER_AGENT}, timeout=60)
        if resp.status_code not in (200, 206):
            raise LidarError(f"LiDAR file: HTTP {resp.status_code}")
        data = resp.content if resp.status_code == 206 else resp.content[start:start + length]
        return data

    def _parse(self, head: bytes) -> None:
        if head[:2] not in (b"II", b"MM"):
            raise LidarError("LiDAR file isn't a TIFF")
        o = self.order = "<" if head[:2] == b"II" else ">"
        big = struct.unpack(o + "H", head[2:4])[0] == 43
        offset = struct.unpack(o + ("Q" if big else "I"), head[8:16] if big else head[4:8])[0]
        levels: list[dict] = []
        while offset:
            if offset + 64 > len(head):
                raise LidarError("LiDAR file's layout is further in than expected")
            count = struct.unpack(o + ("Q" if big else "H"), head[offset:offset + (8 if big else 2)])[0]
            at, size = offset + (8 if big else 2), (20 if big else 12)
            tags = {}
            for k in range(count):
                entry = head[at + k * size: at + (k + 1) * size]
                tag, typ = struct.unpack(o + "HH", entry[:4])
                n = struct.unpack(o + ("Q" if big else "I"), entry[4:12] if big else entry[4:8])[0]
                raw = entry[12:20] if big else entry[8:12]
                tags[tag] = (typ, n, raw)
            levels.append(tags)
            end = at + count * size
            offset = struct.unpack(o + ("Q" if big else "I"), head[end:end + (8 if big else 4)])[0]
        self.big = big

        def value(tags, tag, default=None):
            if tag not in tags:
                return default
            typ, n, raw = tags[tag]
            fmt = _TIFF_TYPES[typ]
            size = struct.calcsize(fmt) * n
            inline = 8 if big else 4
            if size <= inline:
                data = raw[:size]
            else:
                where = struct.unpack(o + ("Q" if big else "I"), raw[:inline])[0]
                data = head[where:where + size]
                if len(data) < size:
                    return ("table", where, fmt, n)
            if typ == 2:
                return data.rstrip(b"\0").decode("latin1")
            values = struct.unpack(o + fmt * n, data)
            return values[0] if n == 1 else values

        def table(tags, tag):
            typ, n, raw = tags[tag]
            fmt = _TIFF_TYPES[typ]
            inline = 8 if big else 4
            if struct.calcsize(fmt) * n <= inline:     # a level of one or two tiles keeps them in the entry
                return 0, fmt, n, struct.unpack(o + fmt * n, raw[:struct.calcsize(fmt) * n])
            return struct.unpack(o + ("Q" if big else "I"), raw[:inline])[0], fmt, n

        first = levels[0]
        if value(first, 259) not in (1, 8, 32946) or value(first, 317, 1) != 1:
            raise LidarError("LiDAR file is compressed in a way this can't read")
        kinds = {(3, 32): "f", (2, 16): "h", (1, 16): "H", (3, 64): "d"}
        self.dtype = kinds.get((value(first, 339, 1), value(first, 258)))
        if not self.dtype or value(first, 277, 1) != 1:
            raise LidarError("LiDAR file holds something other than one height per pixel")
        self.compressed = value(first, 259) != 1
        scale = value(first, 33550)[0]
        tie = value(first, 33922)
        self.west, self.north = tie[3], tie[4]
        nodata = value(first, 42113)
        self.nodata = float(nodata) if nodata not in (None, "") else None
        self.levels = []
        for tags in levels:
            width = value(tags, 256)
            self.levels.append(Level(width=width, height=value(tags, 257), tile_w=value(tags, 322),
                                     tile_h=value(tags, 323), offsets=table(tags, 324), counts=table(tags, 325),
                                     scale=scale * value(first, 256) / width))

    def _entry(self, where: tuple, i: int) -> int:
        start, fmt, n = where[:3]
        if not 0 <= i < n:
            return 0
        if len(where) > 3:
            return where[3][i]
        size = struct.calcsize(fmt)
        byte = start + i * size
        page = byte // TABLE_PAGE
        with self._lock:
            data = self._pages.get(page)
            if data is not None:
                self._pages.move_to_end(page)
        if data is None:
            data = self._read(page * TABLE_PAGE, TABLE_PAGE)
            with self._lock:
                self._pages[page] = data
                while len(self._pages) > 512:
                    self._pages.popitem(last=False)
        local = byte - page * TABLE_PAGE
        if local + size > len(data):     # straddles two pages: rare, read it directly
            return struct.unpack(self.order + fmt, self._read(byte, size))[0]
        return struct.unpack(self.order + fmt, data[local:local + size])[0]

    def tile(self, level: int, tx: int, ty: int):
        """One tile's heights (a flat array, row by row), or None if it's outside the data."""
        lv = self.levels[level]
        if not (0 <= tx < lv.across and 0 <= ty < -(-lv.height // lv.tile_h)):
            return None
        key = (level, tx, ty)
        with self._lock:
            if key in self._tiles:
                self._tiles.move_to_end(key)
                return self._tiles[key]
        i = ty * lv.across + tx
        start, length = self._entry(lv.offsets, i), self._entry(lv.counts, i)
        heights = None
        if length:
            raw = self._read(start, length)
            heights = array.array(self.dtype, zlib.decompress(raw) if self.compressed else raw)
            if (self.order == "<") != (sys.byteorder == "little"):
                heights.byteswap()
        with self._lock:
            self._tiles[key] = heights
            while len(self._tiles) > KEEP_SOURCE_TILES:
                self._tiles.popitem(last=False)
        return heights


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    """A greyscale-with-transparency PNG."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\0" + row for row in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 4, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


EMPTY_TILE = _png(TILE, TILE, [bytes(2 * TILE)] * TILE)


def _lat_lng(px: float, py: float, z: int) -> tuple[float, float]:
    """Web Mercator pixel (at zoom z) to latitude and longitude."""
    world = TILE * 2 ** z
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * py / world)))), px / world * 360 - 180


class WalesRelief:
    """Map tiles of Wales's LiDAR terrain, shaded."""

    def __init__(self, cache_dir: Path, session_factory: Callable[[], requests.Session] = requests.Session,
                 url: str = WALES_DTM, extent: tuple[float, float, float, float] = WALES_BNG):
        self.cache_dir = cache_dir
        self.session_factory = session_factory
        self.url = url
        self.extent = extent
        self._cog: Cog | None = None
        self._open_lock = threading.Lock()
        self._renders = threading.Semaphore(RENDERS_AT_ONCE)
        self._written = 0

    def cog(self) -> Cog:
        with self._open_lock:
            if self._cog is None:
                self._cog = Cog(self.url, self.session_factory)
            return self._cog

    def tile(self, z: int, x: int, y: int) -> bytes:
        """A PNG map tile; EMPTY_TILE where there's no Welsh data. Raises LidarError if the file can't be read."""
        if not (MIN_ZOOM <= z <= MAX_ZOOM and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            raise ValueError("No LiDAR tile at that zoom")
        path = self.cache_dir / str(z) / str(x) / f"{y}.png"
        try:
            return path.read_bytes()
        except OSError:
            pass
        corners = [wgs84_to_bng(*_lat_lng(px, py, z)) for px in (x * TILE, (x + 1) * TILE)
                   for py in (y * TILE, (y + 1) * TILE)]
        w, s, e, n = self.extent
        if max(c[0] for c in corners) < w or min(c[0] for c in corners) > e \
                or max(c[1] for c in corners) < s or min(c[1] for c in corners) > n:
            return EMPTY_TILE                      # the sea, or England
        with self._renders:
            png = self._render(z, x, y)
        self._keep(path, png)
        return png

    def _keep(self, path: Path, png: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_suffix(f".{threading.get_ident()}.part")
            part.write_bytes(png)
            os.replace(part, path)
        except OSError:
            return   # read-only or full: tiles are just drawn again
        self._written += 1
        if self._written % 500 == 0:
            self._prune()

    def _prune(self) -> None:
        tiles = sorted(self.cache_dir.rglob("*.png"), key=lambda p: p.stat().st_mtime)
        for old in tiles[:max(0, len(tiles) - KEEP_TILES_ON_DISK)]:
            try:
                old.unlink()
            except OSError:
                pass

    def _render(self, z: int, x: int, y: int) -> bytes:
        cog = self.cog()
        lat_mid, _ = _lat_lng((x + 0.5) * TILE, (y + 0.5) * TILE, z)
        metres = EARTH_M * math.cos(math.radians(lat_mid)) / (TILE * 2 ** z)   # per output pixel
        # The most detailed copy that isn't finer than the tile needs.
        level = max((i for i, lv in enumerate(cog.levels) if lv.scale <= metres * 1.01), default=0)
        lv = cog.levels[level]

        # Grid positions for the centres of the tile's pixels, plus a one-pixel border for the shading:
        # exact on a 9 x 9 lattice, in between by interpolation (the grid barely bends across a tile).
        size = TILE + 2
        steps = 8
        lattice = [[wgs84_to_bng(*_lat_lng(x * TILE - 0.5 + (TILE + 1) * i / steps,
                                           y * TILE - 0.5 + (TILE + 1) * j / steps, z))
                    for i in range(steps + 1)] for j in range(steps + 1)]
        cell = (TILE + 1) / steps
        heights = [0.0] * (size * size)
        known = bytearray(size * size)
        nodata = cog.nodata
        tiles: dict = {}

        def height_at(col: int, row: int):
            if col < 0 or row < 0 or col >= lv.width or row >= lv.height:
                return None
            key = (col // lv.tile_w, row // lv.tile_h)
            if key not in tiles:
                tiles[key] = cog.tile(level, *key)
            source = tiles[key]
            if source is None:
                return None
            h = source[(row % lv.tile_h) * lv.tile_w + col % lv.tile_w]
            return None if (nodata is not None and h == nodata) or h < -1000 else h

        # Zoomed in past the data (a pixel finer than a metre), blend the four nearest heights rather than
        # repeating one, or the shading comes out in steps.
        smooth = metres < lv.scale * 0.95
        for r in range(size):
            fj = r / cell
            j = min(int(fj), steps - 1)
            v = fj - j
            row_lattice = [(a[0] + (b[0] - a[0]) * v, a[1] + (b[1] - a[1]) * v)
                           for a, b in zip(lattice[j], lattice[j + 1])]
            for c in range(size):
                fi = c / cell
                i = min(int(fi), steps - 1)
                u = fi - i
                (e0, n0), (e1, n1) = row_lattice[i], row_lattice[i + 1]
                fx = (e0 + (e1 - e0) * u - cog.west) / lv.scale
                fy = (cog.north - (n0 + (n1 - n0) * u)) / lv.scale
                if smooth:
                    cx, cy = math.floor(fx - 0.5), math.floor(fy - 0.5)
                    tx, ty = fx - 0.5 - cx, fy - 0.5 - cy
                    corners = (height_at(cx, cy), height_at(cx + 1, cy), height_at(cx, cy + 1), height_at(cx + 1, cy + 1))
                    if None in corners:
                        h = height_at(int(fx), int(fy))
                    else:
                        top = corners[0] + (corners[1] - corners[0]) * tx
                        bottom = corners[2] + (corners[3] - corners[2]) * tx
                        h = top + (bottom - top) * ty
                else:
                    h = height_at(int(fx), int(fy))
                if h is None:
                    continue
                k = r * size + c
                heights[k] = h
                known[k] = 1

        # Shade (Horn's method): light on slopes facing the north-west, shadow on the far side.
        lx, ly, lz = LIGHT
        rows = []
        spacing = 8 * metres / EXAGGERATE
        for r in range(1, size - 1):
            line = bytearray(2 * TILE)
            above, here, below = (r - 1) * size, r * size, (r + 1) * size
            for c in range(1, size - 1):
                k = here + c
                if not known[k]:
                    continue
                around = (above + c - 1, above + c, above + c + 1, k - 1, k + 1, below + c - 1, below + c, below + c + 1)
                if all(known[i] for i in around):
                    a, b, cc, d, f, g, hh, i9 = (heights[i] for i in around)
                else:            # at the edge of the data: missing neighbours count as level
                    h = heights[k]
                    a, b, cc, d, f, g, hh, i9 = (heights[i] if known[i] else h for i in around)
                dx = ((cc + 2 * f + i9) - (a + 2 * d + g)) / spacing
                dy = ((g + 2 * hh + i9) - (a + 2 * b + cc)) / spacing   # rows run south
                lit = (lx * -dx + ly * dy + lz) / math.sqrt(1 + dx * dx + dy * dy)
                line[2 * (c - 1)] = max(0, min(255, int(255 * lit)))
                line[2 * (c - 1) + 1] = 255
            rows.append(bytes(line))
        return _png(TILE, TILE, rows)
