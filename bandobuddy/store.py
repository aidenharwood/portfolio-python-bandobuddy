"""SQLite store for the whole-UK dataset.

Raw source items (OSM elements, Wikidata items) are kept with first_seen/last_seen/gone_at, so each
update can tell what's new and what has disappeared. `sites` is derived from them by sites.py and is
what the map reads. Every call opens its own connection (WAL mode), so the updater threads and the
web server can share one database file safely.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .config import BEST, DATE_FILTERS

SCHEMA = """
CREATE TABLE IF NOT EXISTS osm_items (
    osm_id TEXT PRIMARY KEY, lat REAL, lng REAL, tags TEXT,
    first_seen TEXT, last_seen TEXT, gone_at TEXT, extent_m REAL
);
CREATE TABLE IF NOT EXISTS wd_items (
    qid TEXT PRIMARY KEY, label TEXT, lat REAL, lng REAL, types TEXT, states TEXT, ended TEXT, wiki TEXT,
    first_seen TEXT, last_seen TEXT, gone_at TEXT, aliases TEXT
);
CREATE TABLE IF NOT EXISTS od_items (
    dataset TEXT, ref TEXT, name TEXT, lat REAL, lng REAL, kind TEXT, evidence TEXT, weight INTEGER, url TEXT,
    first_seen TEXT, last_seen TEXT, gone_at TEXT, aliases TEXT,
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
    condition TEXT, kind TEXT, sources TEXT, reasons TEXT, detail TEXT, first_seen TEXT, added TEXT,
    aliases TEXT, entrances TEXT, reported TEXT, reported_as TEXT, reported_by TEXT, report TEXT, dates TEXT
);
CREATE INDEX IF NOT EXISTS sites_lat ON sites(lat);
CREATE INDEX IF NOT EXISTS sites_score ON sites(score);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS reports (
    key TEXT, reporter TEXT, difficulty INTEGER, access TEXT, tags TEXT, at TEXT, PRIMARY KEY (key, reporter)
);
CREATE TABLE IF NOT EXISTS report_log (key TEXT, reporter TEXT, at TEXT, access TEXT, difficulty INTEGER);
CREATE INDEX IF NOT EXISTS report_log_key ON report_log(key);
CREATE TABLE IF NOT EXISTS report_touched (key TEXT PRIMARY KEY, at TEXT);
"""

SITE_LIST_FIELDS = ("key, name, lat, lng, score, strength, category, condition, kind, sources, added, aliases, "
                    "reported, reported_as, reported_by, report, "
                    "json_array_length(entrances) AS entrance_count")
# Every place in brief, for phones to keep: enough to draw the map, fill the list and search offline.
INDEX_COLUMNS = ["key", "name", "lat", "lng", "score", "strength", "category", "condition", "kind", "sources",
                 "added", "first_seen", "aliases", "entrance_count", "summary", "reported", "reported_as", "reported_by",
                 "report", "dates"]
MAP_SITE_LIMIT = 400   # more matches than this in view and the map shows clusters instead
# SQLite lets one connection write at a time. Every source refreshing at once (a restart, "Update all") queues for
# that turn: waiting up to ten minutes for it, rather than giving up after 30 seconds with "database is locked".
BUSY_WAIT_S = 600
# ...and no one write holds it for long: big writes go in batches of this many rows, each its own transaction.
WRITE_BATCH = 5000
SITES_DDL = re.search(r"CREATE TABLE IF NOT EXISTS sites (\(.*?\));", SCHEMA, re.S).group(1)


DATE_FILTER_KEYS = {key for key, _ in DATE_FILTERS}


def _batches(rows: list, size: int = WRITE_BATCH):
    for at in range(0, len(rows), size):
        yield rows[at:at + size]
CLUSTER_PX = 80        # roughly how wide a cluster cell is on screen: wide enough to leave the map showing


def now_iso() -> str:
    # Microseconds, so two updates in quick succession still order correctly ("new since" relies on it).
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """The tables, and the columns added since they were made. At start, and again whenever a query finds a
        column missing: an older copy of bandobuddy on the same data rebuilds the map in its own layout."""
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            columns = {r["name"] for r in db.execute("PRAGMA table_info(sites)")}
            if columns and not {"condition", "entrances"} <= columns:
                db.execute("DROP TABLE sites")  # derived data from an older version; rebuilt from raw items
            db.executescript(SCHEMA)
            # Columns added since a table was made: add them in place, and the next update fills them in.
            for table, column, kind in (("osm_items", "extent_m", "REAL"), ("wd_items", "aliases", "TEXT"),
                                        ("od_items", "aliases", "TEXT"), ("osm_items", "edited", "TEXT"),
                                        ("wd_items", "modified", "TEXT"), ("od_items", "reported", "TEXT"),
                                        ("od_items", "reported_as", "TEXT"), ("od_items", "dates", "TEXT"),
                                        ("sites", "reported", "TEXT"), ("sites", "reported_as", "TEXT"),
                                        ("sites", "reported_by", "TEXT"), ("sites", "report", "TEXT"),
                                        ("sites", "dates", "TEXT")):
                if column not in {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(str(self.path), timeout=BUSY_WAIT_S)
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
        rows = [(i["osm_id"], i["lat"], i["lng"], json.dumps(i["tags"], ensure_ascii=False), i.get("extent_m") or 0,
                 i.get("edited"), seen_at, seen_at) for i in items]
        for batch in _batches(rows):
            with self.connect() as db:
                db.executemany(
                    """INSERT INTO osm_items (osm_id, lat, lng, tags, extent_m, edited, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(osm_id) DO UPDATE SET lat = excluded.lat, lng = excluded.lng, tags = excluded.tags,
                       extent_m = excluded.extent_m, edited = COALESCE(excluded.edited, osm_items.edited),
                       last_seen = excluded.last_seen, gone_at = NULL""",
                    batch,
                )

    def upsert_wd(self, items: Iterable[dict], seen_at: str) -> None:
        rows = [(i["qid"], i["label"], i["lat"], i["lng"], json.dumps(i["types"]), json.dumps(i["states"]),
                 i["ended"], i["wiki"], json.dumps(i.get("aliases") or [], ensure_ascii=False), i.get("modified"),
                 seen_at, seen_at) for i in items]
        for batch in _batches(rows):
            with self.connect() as db:
                db.executemany(
                    """INSERT INTO wd_items (qid, label, lat, lng, types, states, ended, wiki, aliases, modified,
                       first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(qid) DO UPDATE SET label = excluded.label, lat = excluded.lat, lng = excluded.lng,
                       types = excluded.types, states = excluded.states, ended = excluded.ended, wiki = excluded.wiki,
                       aliases = excluded.aliases, modified = COALESCE(excluded.modified, wd_items.modified),
                       last_seen = excluded.last_seen, gone_at = NULL""",
                    batch,
                )

    def upsert_od(self, items: Iterable[dict], seen_at: str) -> None:
        """Records from an open register (Historic England, Canmore, Coflein, brownfield...)."""
        rows = [(i["dataset"], i["ref"], i["name"], i["lat"], i["lng"], i["kind"], i["evidence"], i["weight"],
                 i.get("url"), json.dumps(i.get("aliases") or [], ensure_ascii=False), i.get("reported"),
                 i.get("reported_as"), json.dumps(i.get("dates") or []), seen_at, seen_at) for i in items]
        for batch in _batches(rows):
            with self.connect() as db:
                db.executemany(
                    """INSERT INTO od_items (dataset, ref, name, lat, lng, kind, evidence, weight, url, aliases,
                       reported, reported_as, dates, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(dataset, ref) DO UPDATE SET name = excluded.name, lat = excluded.lat,
                       lng = excluded.lng, kind = excluded.kind, evidence = excluded.evidence,
                       weight = excluded.weight, url = excluded.url, aliases = excluded.aliases,
                       reported = excluded.reported, reported_as = excluded.reported_as, dates = excluded.dates,
                       last_seen = excluded.last_seen, gone_at = NULL""",
                    batch,
                )

    def active_od(self, dataset: str | None = None) -> list[dict]:
        where = "gone_at IS NULL" + (" AND dataset = ?" if dataset else "")
        with self.connect() as db:
            rows = db.execute(f"SELECT * FROM od_items WHERE {where} ORDER BY dataset, ref",
                              (dataset,) if dataset else ())
            return [{**dict(r), "aliases": json.loads(r["aliases"] or "[]"), "dates": json.loads(r["dates"] or "[]")}
                    for r in rows]

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

    def seen_since(self, source: str, since: str) -> bool:
        """Has anything from the source been seen since then?"""
        table = {"osm": "osm_items", "wikidata": "wd_items"}.get(source)
        with self.connect() as db:
            if table:
                return db.execute(f"SELECT 1 FROM {table} WHERE last_seen >= ? LIMIT 1", (since,)).fetchone() is not None
            return db.execute("SELECT 1 FROM od_items WHERE dataset = ? AND last_seen >= ? LIMIT 1",
                              (source, since)).fetchone() is not None

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
            rows = db.execute("SELECT osm_id, lat, lng, tags, extent_m, edited, first_seen FROM osm_items"
                              " WHERE gone_at IS NULL")
            return [{"osm_id": r["osm_id"], "lat": r["lat"], "lng": r["lng"], "tags": json.loads(r["tags"]),
                     "extent_m": r["extent_m"] or 0, "edited": r["edited"], "first_seen": r["first_seen"]} for r in rows]

    def active_wd(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM wd_items WHERE gone_at IS NULL")
            return [{"qid": r["qid"], "label": r["label"], "lat": r["lat"], "lng": r["lng"],
                     "types": json.loads(r["types"]), "states": json.loads(r["states"]), "ended": r["ended"],
                     "wiki": r["wiki"], "aliases": json.loads(r["aliases"] or "[]"), "modified": r["modified"],
                     "first_seen": r["first_seen"]}
                    for r in rows]

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

    def tiles_of(self, source: str, crawl_id: int) -> int:
        """How many boxes a crawl has queued: none if it stopped before it got that far."""
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM tiles WHERE source = ? AND crawl_id = ?",
                              (source, crawl_id)).fetchone()[0]

    def tile_counts(self, source: str) -> dict:
        with self.connect() as db:
            rows = db.execute("SELECT status, COUNT(*) AS n FROM tiles WHERE source = ? GROUP BY status", (source,))
            return {r["status"]: r["n"] for r in rows}

    # -- sites ------------------------------------------------------------------------------------
    def replace_sites(self, sites: list[dict]) -> None:
        rows = [(s["key"], s["name"], s["lat"], s["lng"], s["score"], s["strength"], s["category"], s["condition"],
                 s["kind"], s["sources"], json.dumps(s["reasons"], ensure_ascii=False),
                 json.dumps(s["detail"], ensure_ascii=False), s["first_seen"], s["added"],
                 json.dumps(s.get("aliases") or [], ensure_ascii=False),
                 json.dumps(s.get("entrances") or [], ensure_ascii=False), s.get("reported"), s.get("reported_as"),
                 s.get("reported_by"), json.dumps(s["report"]) if s.get("report") else None,
                 json.dumps(s.get("dates") or {})) for s in sites]
        # Phones keep a copy of the map, and answer from it while it matches the server's. A rebuild that comes
        # out the same (a restart, or an update that found nothing new) leaves theirs current, and the table alone.
        # The best-spots rules count too: phones apply them to their copy, so new rules need a new copy.
        digest = hashlib.blake2b(json.dumps(BEST, sort_keys=True).encode("utf-8"), digest_size=16)
        for row in rows:
            digest.update("\x1f".join(map(str, row)).encode("utf-8", "surrogatepass") + b"\x1e")
        digest = digest.hexdigest()
        if digest == self.get_setting("sites_digest") and self.get_setting("sites_built"):
            return
        # Written beside the map in use, a batch at a time, then swapped in at once. Written in one go, 160,000
        # places held the database for over a minute, and every source refreshing meanwhile gave up waiting.
        # Readers see the old map until the swap. Named for this process: another may be rebuilding too.
        table = f"sites_next_{os.getpid()}"
        with self.connect() as db:
            db.execute(f"DROP TABLE IF EXISTS {table}")
            db.execute(f"CREATE TABLE {table} {SITES_DDL}")
        for batch in _batches(rows):
            with self.connect() as db:
                db.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * 21)})", batch)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")      # DDL doesn't start a transaction by itself: the swap is all or nothing
            db.execute("DROP TABLE sites")
            db.execute(f"ALTER TABLE {table} RENAME TO sites")
            db.execute("CREATE INDEX sites_lat ON sites(lat)")
            db.execute("CREATE INDEX sites_score ON sites(score)")
            db.execute("INSERT OR REPLACE INTO settings VALUES ('sites_built', ?)", (json.dumps(now_iso()),))
            db.execute("INSERT OR REPLACE INTO settings VALUES ('sites_digest', ?)", (json.dumps(digest),))

    # -- visitors' reports ---------------------------------------------------------------------
    def save_report(self, key: str, reporter: str, difficulty: int | None, access: str | None, tags: list[str],
                    at: str) -> None:
        """A device's report on a place, in place of its last. Marking it accessible or inaccessible goes in the
        place's history too: once a day per device, unless it changes its mind."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            before = db.execute("SELECT access, at FROM reports WHERE key = ? AND reporter = ?",
                                (key, reporter)).fetchone()
            db.execute("INSERT OR REPLACE INTO reports VALUES (?, ?, ?, ?, ?, ?)",
                       (key, reporter, difficulty, access, json.dumps(tags), at))
            if access and (not before or before["access"] != access or (before["at"] or "")[:10] != at[:10]):
                db.execute("INSERT INTO report_log VALUES (?, ?, ?, ?, ?)", (key, reporter, at, access, difficulty))
            db.execute("INSERT OR REPLACE INTO report_touched VALUES (?, ?)", (key, now_iso()))

    def clear_report(self, key: str, reporter: str) -> None:
        """Taken back: the report, and that device's marks in the place's history (a slip shouldn't stay)."""
        with self.connect() as db:
            db.execute("DELETE FROM reports WHERE key = ? AND reporter = ?", (key, reporter))
            db.execute("DELETE FROM report_log WHERE key = ? AND reporter = ?", (key, reporter))
            db.execute("INSERT OR REPLACE INTO report_touched VALUES (?, ?)", (key, now_iso()))

    def reports_for(self, key: str) -> tuple[list[dict], list[dict]]:
        """A place's reports, and its history of being marked accessible or not."""
        with self.connect() as db:
            rows = [{**dict(r), "tags": json.loads(r["tags"] or "[]")}
                    for r in db.execute("SELECT * FROM reports WHERE key = ?", (key,))]
            history = [dict(r) for r in db.execute("SELECT at, access, difficulty FROM report_log WHERE key = ?", (key,))]
        return rows, history

    def report_pairs(self) -> list[tuple[str, str]]:
        """(place, reporter) for every report: which device a reporter is, only that device can tell."""
        with self.connect() as db:
            return [(r["key"], r["reporter"]) for r in db.execute("SELECT key, reporter FROM reports")]

    def all_reports(self) -> dict[str, list[dict]]:
        """Every report, by place: for the map's rebuild."""
        out: dict[str, list[dict]] = {}
        with self.connect() as db:
            for r in db.execute("SELECT * FROM reports"):
                out.setdefault(r["key"], []).append({**dict(r), "tags": json.loads(r["tags"] or "[]")})
        return out

    def reports_touched_since(self, at: str) -> list[str]:
        """Places whose reports changed since then: a rebuild that read them earlier puts these back."""
        with self.connect() as db:
            return [r[0] for r in db.execute("SELECT key FROM report_touched WHERE at >= ?", (at,))]

    def site_exists(self, key: str) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM sites WHERE key = ?", (key,)).fetchone() is not None

    def set_site_report(self, key: str, summary: dict | None) -> None:
        """A place's reports, straight onto the map: one row, as the next rebuild would leave it. (The rebuild
        also weighs them for the place's last update; the panel shows that live meanwhile.)"""
        with self.connect() as db:
            db.execute("UPDATE sites SET report = ? WHERE key = ?", (json.dumps(summary) if summary else None, key))

    def sites_built(self) -> str:
        """When the map was last rebuilt (or, from before that was recorded, something that changes with it)."""
        built = self.get_setting("sites_built")
        if built:
            return built
        with self.connect() as db:
            n, latest = db.execute("SELECT COUNT(*), MAX(COALESCE(added, first_seen)) FROM sites").fetchone()
        return f"{n}@{latest or ''}"

    def index_rows(self) -> list[list]:
        """Every place in brief, as rows in INDEX_COLUMNS order. Positions to about a metre."""
        with self.connect() as db:
            rows = db.execute("SELECT key, name, ROUND(lat, 5), ROUND(lng, 5), score, strength, category, condition, "
                              "kind, sources, added, first_seen, aliases, "
                              "json_array_length(entrances), json_extract(reasons, '$[0]'), reported, reported_as, "
                              "reported_by, report, dates FROM sites ORDER BY key")
            return [[*r[:12], json.loads(r[12] or "[]"), r[13] or 0, r[14] or "", *r[15:18], json.loads(r[18] or "null"),
                     json.loads(r[19] or "{}")] for r in rows]

    def sites_by_key(self, keys: list[str]) -> list[dict]:
        """Everything about particular places (any that no longer exist are left out)."""
        if not keys:
            return []
        with self.connect() as db:
            rows = db.execute(f"SELECT * FROM sites WHERE key IN ({','.join('?' * len(keys))}) ORDER BY key", keys)
            return [_decode_site(r) for r in rows]

    def details_page(self, bbox=None, after: str = "", limit: int = 500) -> list[dict]:
        """Everything about the places in an area (or everywhere), a page at a time in key order."""
        where, args = self._site_filter(bbox=bbox)
        with self.connect() as db:
            rows = db.execute(f"SELECT * FROM sites WHERE {where} AND key > ? ORDER BY key LIMIT ?",
                              [*args, after, limit])
            return [_decode_site(r) for r in rows]

    def _site_filter(self, bbox=None, min_score=0, categories=None, sources=None, added_since=None, q=None,
                     best=False, date_kind=None, date_from=None, date_to=None):
        where, args = ["score >= ?"], [max(min_score, BEST["min_score"]) if best else min_score]
        if best:   # config.BEST: standing, empty or derelict, and somewhere to go and see
            where.append(f"(condition IN ({','.join('?' * len(BEST['conditions']))})"
                         " OR condition GLOB 'Closed [0-9][0-9][0-9][0-9]')")
            args += BEST["conditions"]
            where.append(f"LOWER(kind) NOT IN ({','.join('?' * len(BEST['skip_kinds']))})")
            args += BEST["skip_kinds"]
            where.append(f"(name NOT LIKE 'Unnamed %' OR category IN ({','.join('?' * len(BEST['unnamed_ok']))})"
                         f" OR LOWER(kind) NOT IN ({','.join('?' * len(BEST['vague_kinds']))}))")
            args += BEST["unnamed_ok"] + BEST["vague_kinds"]
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
        if date_kind in DATE_FILTER_KEYS and (date_from or date_to):   # "last update more than five years ago"
            field = f"json_extract(dates, '$.{date_kind}')"
            where.append(f"{field} IS NOT NULL")
            if date_from:
                where.append(f"{field} >= ?")
                args.append(date_from)
            if date_to:
                where.append(f"{field} < ?")
                args.append(date_to)
        if q:
            # "Bethel" finds Gripwood Quarry; "butser hill" finds Butserhill Lime Works, as the old map spells it.
            squashed = re.sub(r"[\s-]+", "", q)
            where.append("(name LIKE ? OR aliases LIKE ? OR REPLACE(REPLACE(name, ' ', ''), '-', '') LIKE ?"
                         " OR REPLACE(REPLACE(aliases, ' ', ''), '-', '') LIKE ?)")
            args += [f"%{q}%", f"%{q}%", f"%{squashed}%", f"%{squashed}%"]
        return " AND ".join(where), args

    def query_sites(self, limit: int = 3000, **filters) -> tuple[int, list[dict]]:
        where, args = self._site_filter(**filters)
        with self.connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM sites WHERE {where}", args).fetchone()[0]
            rows = db.execute(f"SELECT {SITE_LIST_FIELDS} FROM sites WHERE {where} ORDER BY score DESC, key LIMIT ?",
                              [*args, limit]).fetchall()
        return total, [_listed(r) for r in rows]

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
                return {"mode": "sites", "total": total, "sites": [_listed(r) for r in rows], "clusters": []}

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
            single_rows = {r["key"]: _listed(r) for r in db.execute(
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
            site = _listed(r)
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


def _listed(row: sqlite3.Row) -> dict:
    site = dict(row)
    site["aliases"] = json.loads(site.get("aliases") or "[]")
    site["report"] = json.loads(site.get("report") or "null")
    return site


def _decode_site(row: sqlite3.Row) -> dict:
    site = dict(row)
    site["reasons"] = json.loads(site["reasons"])
    site["detail"] = json.loads(site["detail"])
    site["aliases"] = json.loads(site.get("aliases") or "[]")
    site["entrances"] = json.loads(site.get("entrances") or "[]")
    site["report"] = json.loads(site.get("report") or "null")
    return site
