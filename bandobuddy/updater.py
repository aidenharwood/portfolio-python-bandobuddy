"""Build and refresh the whole-UK database in the background.

Two independent sources, each with its own worker thread and its own resumable crawl:
  osm       download (first time) or update the Geofabrik UK extract, then read it
  wikidata  query Wikidata box by box across the UK, then fetch Wikipedia intros
Sites are rebuilt as results land, so the map fills in while an update runs.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests

from . import extract, osm, wikidata
from .config import DEFAULT_UPDATE_DAYS, GEOFABRIK_UK_URL, UK_BBOX
from .geo import grid_boxes
from .sites import build_sites
from .store import Store

SOURCES = ("osm", "wikidata")
SOURCE_LABELS = {"osm": "OpenStreetMap", "wikidata": "Wikidata & Wikipedia"}
WIKIDATA_BOX_DEG = 0.5
MAX_TILE_DEPTH = 5            # 0.5 deg -> ~1.7 km boxes at most
MAX_BUSY_RETRIES = 8
POLITE_DELAY_S = 1.0          # between Wikidata queries
INTRO_MAX_AGE_DAYS = 90
REBUILD_EVERY_S = 20
RETRY_FAILED_AFTER_S = 3600


class Updater:
    def __init__(
        self,
        store: Store,
        data_dir: Path,
        session_factory: Callable[[], requests.Session] = requests.Session,
        log: Callable[[str], None] = print,
        extract_url: str = GEOFABRIK_UK_URL,
        uk_bbox: tuple[float, float, float, float] = UK_BBOX,
    ):
        self.store = store
        self.data_dir = data_dir
        self.session_factory = session_factory
        self._log = log
        self.extract_url = extract_url
        self.uk_bbox = uk_bbox
        self.pbf = data_dir / extract_url.rsplit("/", 1)[1]
        self._lock = threading.Lock()
        self._rebuild_lock = threading.Lock()
        self._last_rebuild = 0.0
        self.sites_version = 0
        self.lines: list[str] = []
        self.state = {src: {"running": False, "stage": "", "done": 0, "total": None, "error": None,
                            "paused_by_user": False, "last_attempt": 0.0} for src in SOURCES}
        self._threads: dict[str, threading.Thread] = {}
        self._cancel = {src: threading.Event() for src in SOURCES}
        self.polite_delay = POLITE_DELAY_S
        self.intro_delay = 0.2

    # -- logging / progress -----------------------------------------------------------------------
    def log(self, msg: str) -> None:
        with self._lock:
            self.lines.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
            del self.lines[:-300]
        self._log(msg)

    def _progress(self, src: str) -> Callable[[str, int, "int | None"], None]:
        def report(stage: str, done: int, total: int | None) -> None:
            with self._lock:
                self.state[src].update(stage=stage, done=done, total=total)
        return report

    # -- control ----------------------------------------------------------------------------------
    def start(self, source: str, by_user: bool = False) -> bool:
        """Start updating a source in the background. False if it's already running."""
        with self._lock:
            st = self.state[source]
            if st["running"]:
                return False
            st.update(running=True, error=None, stage="Starting", done=0, total=None, last_attempt=time.time())
            if by_user:
                st["paused_by_user"] = False
            self._cancel[source].clear()
        t = threading.Thread(target=self._run_safely, args=(source,), daemon=True, name=f"update-{source}")
        self._threads[source] = t
        t.start()
        return True

    def pause(self, source: str) -> None:
        with self._lock:
            self.state[source]["paused_by_user"] = True
        self._cancel[source].set()

    def join(self, timeout: float | None = None) -> None:
        for t in list(self._threads.values()):
            t.join(timeout)

    def run(self, source: str) -> None:
        """Update one source in the foreground (used by `bandobuddy update`)."""
        with self._lock:
            self.state[source].update(running=True, error=None)
        self._run_safely(source)
        if self.state[source]["error"]:
            raise RuntimeError(self.state[source]["error"])

    def _run_safely(self, source: str) -> None:
        try:
            (self._run_osm if source == "osm" else self._run_wikidata)()
        except osm.Cancelled:
            self.log(f"{SOURCE_LABELS[source]}: paused")
        except Exception as exc:  # keep the app alive; show the problem in the UI
            with self._lock:
                self.state[source]["error"] = f"{type(exc).__name__}: {exc}"
            self.log(f"{SOURCE_LABELS[source]}: failed - {exc}")
        finally:
            with self._lock:
                self.state[source].update(running=False, stage="")
            self.rebuild(force=True)

    # -- scheduling -------------------------------------------------------------------------------
    def due_sources(self) -> list[str]:
        """Sources that should update now: never built, unfinished, or older than the schedule."""
        if not self.store.get_setting("auto_update", True):
            return []
        days = self.store.get_setting("update_days", DEFAULT_UPDATE_DAYS)
        due = []
        now = datetime.now(timezone.utc)
        for src in SOURCES:
            st = self.state[src]
            if st["running"] or st["paused_by_user"]:
                continue
            if st["error"] and time.time() - st["last_attempt"] < RETRY_FAILED_AFTER_S:
                continue
            last = self.store.last_finished(src)
            unfinished = self.store.unfinished_crawl(src)
            if unfinished or not last or datetime.fromisoformat(last["finished_at"]) < now - timedelta(days=days):
                due.append(src)
        return due

    def status(self) -> dict:
        days = self.store.get_setting("update_days", DEFAULT_UPDATE_DAYS)
        sources = {}
        for src in SOURCES:
            with self._lock:
                st = {k: v for k, v in self.state[src].items() if k != "last_attempt"}
            last = self.store.last_finished(src)
            st["label"] = SOURCE_LABELS[src]
            st["last_update"] = last["finished_at"] if last else None
            st["next_due"] = ((datetime.fromisoformat(last["finished_at"]) + timedelta(days=days)).isoformat(timespec="seconds")
                              if last else None)
            if src == "wikidata":
                st["tiles"] = self.store.tile_counts("wikidata")
            if src == "osm":
                st["data_as_of"] = self.store.get_setting("osm_data_as_of")
            sources[src] = st
        with self._lock:
            lines = list(self.lines[-60:])
        return {
            "sources": sources,
            "items": self.store.count_items(),
            "sites": self.store.site_stats(),
            "sites_version": self.sites_version,
            "auto_update": self.store.get_setting("auto_update", True),
            "update_days": days,
            "data_dir": str(self.data_dir),
            "log": lines,
        }

    # -- rebuilding sites ---------------------------------------------------------------------------
    def rebuild(self, force: bool = False) -> None:
        if not force and time.time() - self._last_rebuild < REBUILD_EVERY_S:
            return
        with self._rebuild_lock:
            n = build_sites(self.store)
            self._last_rebuild = time.time()
            self.sites_version += 1
        self.log(f"map updated: {n:,} sites")

    def _finish(self, source: str, crawl: dict, complete: bool) -> None:
        if complete:
            # Only a complete crawl can say what has disappeared.
            gone = self.store.mark_gone(source, crawl["started_at"])
            if gone:
                self.log(f"{SOURCE_LABELS[source]}: {gone:,} items no longer in the source")
        if not self.store.get_setting(f"baseline_{source}"):
            # Everything from the first build is the starting point, not "new".
            self.store.set_setting(f"baseline_{source}", crawl["started_at"])
        self.store.set_crawl_status(crawl["id"], "done" if complete else "failed",
                                    None if complete else "some areas could not be fetched")

    # -- OpenStreetMap ------------------------------------------------------------------------------
    def _run_osm(self) -> None:
        cancel = self._cancel["osm"]
        progress = self._progress("osm")
        crawl = self.store.unfinished_crawl("osm") or self.store.start_crawl("osm")
        self.store.set_crawl_status(crawl["id"], "running")
        session = self.session_factory()
        try:
            self.log("OpenStreetMap: checking the UK extract")
            result = extract.refresh(self.extract_url, self.pbf, session, progress, cancel, log=self.log)
            self.log(f"OpenStreetMap: extract {result}")
            as_of = extract.data_timestamp(self.pbf)
            if as_of:
                self.store.set_setting("osm_data_as_of", as_of)

            seen_at = crawl["started_at"]

            def early_nodes(nodes: list[dict]) -> None:
                self.store.upsert_osm(nodes, seen_at)
                self.log(f"OpenStreetMap: {len(nodes):,} points found, placing outlines next")
                self.rebuild(force=True)

            items = osm.extract_candidates(self.pbf, progress, cancel, on_nodes=early_nodes)
            self.store.upsert_osm(items, seen_at)
            self.log(f"OpenStreetMap: {len(items):,} tagged places")
            self._finish("osm", crawl, complete=True)
        except osm.Cancelled:
            self.store.set_crawl_status(crawl["id"], "paused")
            raise

    # -- Wikidata ---------------------------------------------------------------------------------------
    def _run_wikidata(self) -> None:
        cancel = self._cancel["wikidata"]
        progress = self._progress("wikidata")
        session = self.session_factory()
        crawl = self.store.unfinished_crawl("wikidata")
        if crawl:
            self.log("Wikidata: resuming where the last update stopped")
        else:
            crawl = self.store.start_crawl("wikidata")
            self.store.seed_tiles("wikidata", crawl["id"], grid_boxes(self.uk_bbox, WIKIDATA_BOX_DEG))
        self.store.set_crawl_status(crawl["id"], "running")
        seen_at = crawl["started_at"]

        busy_retries = 0
        while True:
            if cancel.is_set():
                self.store.set_crawl_status(crawl["id"], "paused")
                raise osm.Cancelled()
            counts = self.store.tile_counts("wikidata")
            done = counts.get("done", 0) + counts.get("failed", 0)
            progress("Querying Wikidata", done, done + counts.get("pending", 0))
            tile = self.store.next_tile("wikidata")
            if not tile:
                break
            try:
                rows = wikidata.fetch_tile(tile["s"], tile["w"], tile["n"], tile["e"], session)
            except wikidata.TileTooBig as exc:
                if tile["depth"] < MAX_TILE_DEPTH:
                    self.store.split_tile("wikidata", tile)
                else:
                    self.store.finish_tile("wikidata", tile["tile"], "failed", error=str(exc))
                continue
            except wikidata.ServiceBusy as exc:
                busy_retries += 1
                if busy_retries > MAX_BUSY_RETRIES:
                    self.store.set_crawl_status(crawl["id"], "paused", "Wikidata busy")
                    raise RuntimeError(f"Wikidata is busy ({exc}); the update will resume later")
                self._sleep(min(exc.retry_after * busy_retries, 300), cancel)
                continue
            busy_retries = 0
            self.store.upsert_wd(rows, seen_at)
            self.store.finish_tile("wikidata", tile["tile"], "done", rows=len(rows))
            self.rebuild()
            self._sleep(self.polite_delay, cancel)

        self._fetch_intros(session, progress, cancel)
        failed = self.store.tile_counts("wikidata").get("failed", 0)
        if failed:
            self.log(f"Wikidata: {failed} small areas could not be fetched; they'll be retried next update")
        self._finish("wikidata", crawl, complete=not failed)

    def _fetch_intros(self, session: requests.Session, progress, cancel: threading.Event) -> None:
        """Wikipedia intros for candidate items with an article, cached for INTRO_MAX_AGE_DAYS."""
        ages = self.store.intro_ages()
        stale_before = (datetime.now(timezone.utc) - timedelta(days=INTRO_MAX_AGE_DAYS)).isoformat()
        titles = sorted({
            t for row in self.store.active_wd()
            if (t := wikidata.wikipedia_title(row["wiki"]))
            and (t not in ages or ages[t] < stale_before)
            and wikidata.evaluate(row, None)
        })
        for i in range(0, len(titles), wikidata.INTRO_BATCH):
            if cancel.is_set():
                raise osm.Cancelled()
            progress("Reading Wikipedia intros", i, len(titles))
            batch = titles[i:i + wikidata.INTRO_BATCH]
            got = wikidata.fetch_intros(batch, session)
            if got:
                self.store.save_intros(got)
            self._sleep(self.intro_delay, cancel)
        if titles:
            self.log(f"Wikidata: read {len(titles):,} Wikipedia intros")

    @staticmethod
    def _sleep(seconds: float, cancel: threading.Event) -> None:
        if cancel.wait(seconds):
            raise osm.Cancelled()
