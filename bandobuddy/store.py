"""SQLite store for the whole-UK dataset.

Raw source items (OSM elements, Wikidata items) are kept with first_seen/last_seen/gone_at, so each
update can tell what's new and what has disappeared. `sites` is derived from them by sites.py and is
what the map reads. Every call opens its own connection (WAL mode), so the updater threads and the
web server can share one database file safely.
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS osm_items (
    osm_id TEXT PRIMARY KEY, lat REAL, lng REAL, tags TEXT,
    first_seen TEXT, last_seen TEXT, gone_at TEXT
);
CREATE TABLE IF NOT EXISTS wd_items (
    qid TEXT PRIMARY KEY, label TEXT, lat REAL, lng REAL, types TEXT, states TEXT, ended TEXT, wiki TEXT,
    first_seen TEXT, last_seen TEXT, gone_at TEXT
);
CREATE TABLE IF NOT EXISTS od_items (
    dataset TEXT, ref TEXT, name TEXT, lat REAL, lng REAL, kind TEXT, evidence TEXT, weight INTEGER, url TEXT,
    first_seen TEXT, last_seen TEXT, gone_at TEXT,
    PRIMARY KEY (dataset, ref)
);
CREATE TABLE IF NOT EXISTS intros (title TEXT PRIMARY KEY, extract TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS crawls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, started_at TEXT, finished_at TEXT, status TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS tiles (
    source TEXT, tile TEXT, s REAL, w REAL, n REAL, e REAL, depth INTEGER, status TEXT,
    attempts INTEGER DEFAULT 0, crawl_id INTEGER, rows INTEGER, error TEXT,
    PRIMARY KEY (source, tile)
);
CREATE TABLE IF NOT EXISTS sites (
    key TEXT PRIMARY KEY, name TEXT, lat REAL, lng REAL, score INTEGER, strength TEXT, category TEXT,
    condition TEXT, kind TEXT, sources TEXT, reasons TEXT, detail TEXT, first_seen TEXT, added TEXT
);
CREATE INDEX IF NOT EXISTS sites_lat ON sites(lat);
CREATE INDEX IF NOT EXISTS sites_score ON sites(score);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

SITE_LIST_FIELDS = "key, name, lat, lng, score, strength, category, condition, kind, sources, added"
MAP_SITE_LIMIT = 400   # more matches than this in view and the map shows clusters instead
CLUSTER_PX = 64        # roughly how wide a cluster cell is on screen


def now_iso() -> str:
    # Microseconds, so two updates in quick succession still order correctly ("new since" relies on it).
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            columns = {r["name"] for r in db.execute("PRAGMA table_info(sites)")}
            if columns and "condition" not in columns:
                db.execute("DROP TABLE sites")  # derived data from an older version; rebuilt from raw items
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    # -- settings ------------------------------------------------------------------------
    def get_setting(self, key: str, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    # -- raw items -----------------------------------------------------------------------
    def upsert_osm(self, items: Iterable[dict], seen_at: str) -> None:
        rows = [(i["osm_id"], i["lat"], i["lng"], json.dumps(i["tags"], ensure_ascii=False), seen_at, seen_at)
                for i in items]
        with self.connect() as db:
            db.executemany(
                """INSERT INTO osm_items (osm_id, lat, lng, tags, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(osm_id) DO UPDATE SET lat = excluded.lat, lng = excluded.lng, tags = excluded.tags,
                   last_seen = excluded.last_seen, gone_at = NULL""",
                rows,
            )

    def upsert_wd(self, items: Iterable[dict], seen_at: str) -> None:
        rows = [(i["qid"], i["label"], i["lat"], i["lng"], json.dumps(i["types"]), json.dumps(i["states"]),
                 i["ended"], i["wiki"], seen_at, seen_at) for i in items]
        with self.connect() as db:
            db.executemany(
                """INSERT INTO wd_items (qid, label, lat, lng, types, states, ended, wiki, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(qid) DO UPDATE SET label = excluded.label, lat = excluded.lat, lng = excluded.lng,
                   types = excluded.types, states = excluded.states, ended = excluded.ended, wiki = excluded.wiki,
                   last_seen = excluded.last_seen, gone_at = NULL""",
                rows,
            )

    def upsert_od(self, items: Iterable[dict], seen_at: str) -> None:
        """Records from an open register (Historic England, Canmore, Coflein, brownfield...)."""
        rows = [(i["dataset"], i["ref"], i["name"], i["lat"], i["lng"], i["kind"], i["evidence"], i["weight"],
                 i.get("url"), seen_at, seen_at) for i in items]
        with self.connect() as db:
            db.executemany(
                """INSERT INTO od_items (dataset, ref, name, lat, lng, kind, evidence, weight, url,
                   first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(dataset, ref) DO UPDATE SET name = excluded.name, lat = excluded.lat,
                   lng = excluded.lng, kind = excluded.kind, evidence = excluded.evidence,
                   weight = excluded.weight, url = excluded.url, last_seen = excluded.last_seen, gone_at = NULL""",
                rows,
            )

    def active_od(self, dataset: str | None = None) -> list[dict]:
        where = "gone_at IS NULL" + (" AND dataset = ?" if dataset else "")
        with self.connect() as db:
            rows = db.execute(f"SELECT * FROM od_items WHERE {where} ORDER BY dataset, ref",
                              (dataset,) if dataset else ())
            return [dict(r) for r in rows]

    def import_labels(self) -> dict[str, int]:
        """The imported sets in the database, and how many places each holds."""
        with self.connect() as db:
            rows = db.execute("SELECT substr(ref, 1, instr(ref, ':') - 1) AS label, COUNT(*) AS n "
                              "FROM od_items WHERE dataset = 'imported' AND gone_at IS NULL GROUP BY label")
            return {r["label"]: r["n"] for r in rows}

    def forget_imports(self, label: str) -> int:
        with self.connect() as db:
            cur = db.execute("DELETE FROM od_items WHERE dataset = 'imported' AND ref LIKE ?", (f"{label}:%",))
            return cur.rowcount

    def mark_gone(self, source: str, before: str) -> int:
        """Items not seen by a complete crawl that started at `before` have left the source."""
        table = {"osm": "osm_items", "wikidata": "wd_items"}.get(source)
        with self.connect() as db:
            if table:
                cur = db.execute(f"UPDATE {table} SET gone_at = ? WHERE last_seen < ? AND gone_at IS NULL",
                                 (now_iso(), before))
            else:  # one of the open registers
                cur = db.execute("UPDATE od_items SET gone_at = ? WHERE dataset = ? AND last_seen < ? "
                                 "AND gone_at IS NULL", (now_iso(), source, before))
            return cur.rowcount

    def active_osm(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT osm_id, lat, lng, tags, first_seen FROM osm_items WHERE gone_at IS NULL")
            return [{"osm_id": r["osm_id"], "lat": r["lat"], "lng": r["lng"], "tags": json.loads(r["tags"]),
                     "first_seen": r["first_seen"]} for r in rows]

    def active_wd(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM wd_items WHERE gone_at IS NULL")
            return [{"qid": r["qid"], "label": r["label"], "lat": r["lat"], "lng": r["lng"],
                     "types": json.loads(r["types"]), "states": json.loads(r["states"]), "ended": r["ended"],
                     "wiki": r["wiki"], "first_seen": r["first_seen"]} for r in rows]

    def count_items(self) -> dict:
        with self.connect() as db:
            counts = {
                "osm": db.execute("SELECT COUNT(*) FROM osm_items WHERE gone_at IS NULL").fetchone()[0],
                "wikidata": db.execute("SELECT COUNT(*) FROM wd_items WHERE gone_at IS NULL").fetchone()[0],
            }
            for row in db.execute("SELECT dataset, COUNT(*) AS n FROM od_items WHERE gone_at IS NULL "
                                  "GROUP BY dataset"):
                counts[row["dataset"]] = row["n"]
        return counts

    # -- Wikipedia intro cache -------------------------------------------------------------
    def intros(self) -> dict[str, str]:
        with self.connect() as db:
            return {r["title"]: r["extract"] for r in db.execute("SELECT title, extract FROM intros")}

    def intro_ages(self) -> dict[str, str]:
        with self.connect() as db:
            return {r["title"]: r["fetched_at"] for r in db.execute("SELECT title, fetched_at FROM intros")}

    def save_intros(self, intros: dict[str, str]) -> None:
        ts = now_iso()
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO intros VALUES (?, ?, ?)", [(t, x, ts) for t, x in intros.items()])

    # -- crawls ------------------------------------------------------------------------------
    def start_crawl(self, source: str) -> dict:
        with self.connect() as db:
            cur = db.execute("INSERT INTO crawls (source, started_at, status) VALUES (?, ?, 'running')",
                             (source, now_iso()))
            return dict(db.execute("SELECT * FROM crawls WHERE id = ?", (cur.lastrowid,)).fetchone())

    def unfinished_crawl(self, source: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM crawls WHERE source = ? AND status IN ('running', 'paused') "
                             "ORDER BY id DESC LIMIT 1", (source,)).fetchone()
        return dict(row) if row else None

    def set_crawl_status(self, crawl_id: int, status: str, note: str | None = None) -> None:
        finished = now_iso() if status in ("done", "failed") else None
        with self.connect() as db:
            db.execute("UPDATE crawls SET status = ?, note = ?, finished_at = COALESCE(?, finished_at) WHERE id = ?",
                       (status, note, finished, crawl_id))

    def last_finished(self, source: str) -> dict | None:
        """The latest crawl that ran to the end ('failed' = finished, but some areas couldn't be fetched)."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM crawls WHERE source = ? AND status IN ('done', 'failed') "
                             "ORDER BY id DESC LIMIT 1", (source,)).fetchone()
        return dict(row) if row else None

    # -- tiles (resumable box-by-box crawls) -----------------------------------------------------
    def seed_tiles(self, source: str, crawl_id: int, roots: list[tuple[float, float, float, float]]) -> None:
        """Queue boxes for a new crawl, reusing the previous crawl's splits where there were any."""
        with self.connect() as db:
            leaves = db.execute("SELECT s, w, n, e, depth FROM tiles WHERE source = ? AND status IN ('done', 'failed')",
                                (source,)).fetchall()
            boxes = [(r["s"], r["w"], r["n"], r["e"], r["depth"]) for r in leaves] or [(*b, 0) for b in roots]
            db.execute("DELETE FROM tiles WHERE source = ?", (source,))
            db.executemany(
                "INSERT INTO tiles (source, tile, s, w, n, e, depth, status, crawl_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                [(source, _tile_id(s, w, n, e), s, w, n, e, d, crawl_id) for s, w, n, e, d in boxes],
            )

    def next_tile(self, source: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tiles WHERE source = ? AND status = 'pending' ORDER BY depth, s, w LIMIT 1",
                             (source,)).fetchone()
        return dict(row) if row else None

    def finish_tile(self, source: str, tile: str, status: str, rows: int | None = None, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE tiles SET status = ?, rows = ?, error = ?, attempts = attempts + 1 "
                       "WHERE source = ? AND tile = ?", (status, rows, error, source, tile))

    def split_tile(self, source: str, tile: dict) -> None:
        s, w, n, e = tile["s"], tile["w"], tile["n"], tile["e"]
        ms, mw = (s + n) / 2, (w + e) / 2
        kids = [(s, w, ms, mw), (s, mw, ms, e), (ms, w, n, mw), (ms, mw, n, e)]
        with self.connect() as db:
            db.execute("UPDATE tiles SET status = 'split' WHERE source = ? AND tile = ?", (source, tile["tile"]))
            db.executemany(
                "INSERT OR REPLACE INTO tiles (source, tile, s, w, n, e, depth, status, crawl_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                [(source, _tile_id(*k), *k, tile["depth"] + 1, tile["crawl_id"]) for k in kids],
            )

    def tile_counts(self, source: str) -> dict:
        with self.connect() as db:
            rows = db.execute("SELECT status, COUNT(*) AS n FROM tiles WHERE source = ? GROUP BY status", (source,))
            return {r["status"]: r["n"] for r in rows}

    # -- sites ------------------------------------------------------------------------------------
    def replace_sites(self, sites: list[dict]) -> None:
        rows = [(s["key"], s["name"], s["lat"], s["lng"], s["score"], s["strength"], s["category"], s["condition"],
                 s["kind"], s["sources"], json.dumps(s["reasons"], ensure_ascii=False),
                 json.dumps(s["detail"], ensure_ascii=False), s["first_seen"], s["added"]) for s in sites]
        with self.connect() as db:
            db.execute("DELETE FROM sites")
            db.executemany("INSERT INTO sites VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def _site_filter(self, bbox=None, min_score=0, categories=None, sources=None, added_since=None, q=None):
        where, args = ["score >= ?"], [min_score]
        if bbox:
            w, s, e, n = bbox
            where += ["lat BETWEEN ? AND ?", "lng BETWEEN ? AND ?"]
            args += [s, n, w, e]
        if categories:
            where.append(f"category IN ({','.join('?' * len(categories))})")
            args += list(categories)
        if sources:
            where.append("(" + " OR ".join("sources LIKE ?" for _ in sources) + ")")
            args += [f"%{src}%" for src in sources]
        if added_since:
            where.append("added > ?")
            args.append(added_since)
        if q:
            where.append("name LIKE ?")
            args.append(f"%{q}%")
        return " AND ".join(where), args

    def query_sites(self, limit: int = 3000, **filters) -> tuple[int, list[dict]]:
        where, args = self._site_filter(**filters)
        with self.connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM sites WHERE {where}", args).fetchone()[0]
            rows = db.execute(f"SELECT {SITE_LIST_FIELDS} FROM sites WHERE {where} ORDER BY score DESC, key LIMIT ?",
                              [*args, limit]).fetchall()
        return total, [dict(r) for r in rows]

    def full_sites(self, limit: int = 50_000, **filters) -> list[dict]:
        where, args = self._site_filter(**filters)
        with self.connect() as db:
            rows = db.execute(f"SELECT * FROM sites WHERE {where} ORDER BY score DESC, key LIMIT ?", [*args, limit])
            return [_decode_site(r) for r in rows]

    def get_site(self, key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM sites WHERE key = ?", (key,)).fetchone()
        return _decode_site(row) if row else None

    def map_view(self, bbox: tuple[float, float, float, float], zoom: int, **filters) -> dict:
        """What to draw for a map view: every matching site if there are few enough (or we're zoomed
        right in), otherwise grid clusters with counts and their most common category."""
        where, args = self._site_filter(bbox=bbox, **filters)
        with self.connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM sites WHERE {where}", args).fetchone()[0]
            if total <= MAP_SITE_LIMIT or zoom >= 15:
                rows = db.execute(f"SELECT {SITE_LIST_FIELDS} FROM sites WHERE {where} "
                                  "ORDER BY score DESC, key LIMIT 2000", args).fetchall()
                return {"mode": "sites", "total": total, "sites": [dict(r) for r in rows], "clusters": []}

            _, s, _, n = bbox
            cell_lng = 360 / (256 * 2 ** zoom) * CLUSTER_PX
            # A fixed grid, not one tied to the view's edges, so clusters stay put while the map pans.
            cell_lat = cell_lng * math.cos(math.radians(round((s + n) / 2)))
            cell = "CAST((lat + 90) / ? AS INTEGER) AS gy, CAST((lng + 180) / ? AS INTEGER) AS gx"
            cell_args = [cell_lat, cell_lng]
            groups = db.execute(
                f"SELECT {cell}, COUNT(*) AS n, AVG(lat) AS lat, AVG(lng) AS lng, MIN(lat) AS s, MIN(lng) AS w, "
                f"MAX(lat) AS nn, MAX(lng) AS e, MIN(key) AS key FROM sites WHERE {where} GROUP BY gy, gx",
                [*cell_args, *args]).fetchall()
            top: dict[tuple[int, int], tuple[int, str]] = {}
            for r in db.execute(f"SELECT {cell}, category, COUNT(*) AS n FROM sites WHERE {where} "
                                "GROUP BY gy, gx, category", [*cell_args, *args]):
                k = (r["gy"], r["gx"])
                if r["n"] > top.get(k, (0, ""))[0]:
                    top[k] = (r["n"], r["category"])
            singles = [g["key"] for g in groups if g["n"] == 1]
            single_rows = {r["key"]: dict(r) for r in db.execute(
                f"SELECT {SITE_LIST_FIELDS} FROM sites WHERE key IN ({','.join('?' * len(singles))})", singles)
            } if singles else {}
        clusters = [{"lat": g["lat"], "lng": g["lng"], "count": g["n"], "category": top[(g["gy"], g["gx"])][1],
                     "bounds": [g["w"], g["s"], g["e"], g["nn"]]} for g in groups if g["n"] > 1]
        return {"mode": "clusters", "total": total, "clusters": clusters, "sites": list(single_rows.values())}

    def list_sites(self, near: tuple[float, float] | None = None, sort: str = "nearest", limit: int = 100,
                   **filters) -> tuple[int, list[dict]]:
        """Sites for the side list: nearest to `near` first, strongest evidence first, or newest first."""
        where, args = self._site_filter(**filters)
        if sort == "nearest" and near:
            lat, lng = near
            k2 = math.cos(math.radians(lat)) ** 2  # longitude degrees shrink away from the equator
            order, order_args = "(lat - ?) * (lat - ?) + (lng - ?) * (lng - ?) * ?", [lat, lat, lng, lng, k2]
        elif sort == "newest":
            order, order_args = "COALESCE(added, first_seen) DESC, score DESC, key", []
        else:
            order, order_args = "score DESC, key", []
        with self.connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM sites WHERE {where}", args).fetchone()[0]
            rows = db.execute(f"SELECT {SITE_LIST_FIELDS}, reasons FROM sites WHERE {where} ORDER BY {order} LIMIT ?",
                              [*args, *order_args, limit]).fetchall()
        out = []
        for r in rows:
            site = dict(r)
            site["summary"] = (json.loads(site.pop("reasons")) or [""])[0]
            out.append(site)
        return total, out

    def site_stats(self) -> dict:
        with self.connect() as db:
            rows = db.execute("SELECT category, COUNT(*) AS n FROM sites WHERE strength != 'weak' GROUP BY category")
            by_cat = {r["category"]: r["n"] for r in rows}
            total = db.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
        return {"total": total, "by_category": by_cat}


def _tile_id(s: float, w: float, n: float, e: float) -> str:
    return f"{s:.5f},{w:.5f},{n:.5f},{e:.5f}"


def _decode_site(row: sqlite3.Row) -> dict:
    site = dict(row)
    site["reasons"] = json.loads(site["reasons"])
    site["detail"] = json.loads(site["detail"])
    return site
