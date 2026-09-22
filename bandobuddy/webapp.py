"""Web app: a map of every likely-abandoned place in the UK, kept up to date in the background.

Two ways to run it:
  - personal (default): bound to 127.0.0.1, full control over updates and settings
  - public (--public): read-only for visitors, e.g. behind an ingress; updates still run on schedule

Every request must carry an allowed Host header (localhost, plus any --allowed-host), which stops
DNS-rebinding tricks against a local copy. POSTs must also be same-origin JSON.
"""
from __future__ import annotations

import json
import signal
import threading
import time
import traceback
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qs, unquote, urlparse

import requests

from . import __version__, export, geocode, imagery
from .config import CATEGORIES, DB_NAME, DEFAULT_MIN_SCORE, OTHER_CATEGORY, UK_BBOX
from .sites import build_sites
from .store import Store
from .updater import SOURCE_LABELS, SOURCES, Updater

DEFAULT_PORT = 8642
MAX_BODY_BYTES = 16_000
MAP_LIMIT = 4000
SCHEDULE_CHECK_S = 300
CACHE_TTL_S = 24 * 3600
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
CATEGORY_KEYS = [c[0] for c in CATEGORIES] + [OTHER_CATEGORY[0]]


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class TTLCache:
    """Small in-memory cache so repeat look-ups don't hit the free upstream services again."""

    def __init__(self, max_items: int = 1000, ttl: float = CACHE_TTL_S):
        self.max_items, self.ttl = max_items, ttl
        self._items: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._items.get(key)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
            return None

    def put(self, key, value) -> None:
        with self._lock:
            if len(self._items) >= self.max_items:
                self._items.pop(next(iter(self._items)))
            self._items[key] = (time.time(), value)


def parse_filters(qs: dict[str, list[str]]) -> dict:
    def one(name: str, default: str = "") -> str:
        return (qs.get(name) or [default])[0].strip()

    filters: dict = {}
    if one("bbox"):
        try:
            w, s, e, n = (float(x) for x in one("bbox").split(","))
        except ValueError:
            raise ApiError(400, "bbox must be west,south,east,north")
        filters["bbox"] = (w, s, e, n)
    try:
        filters["min_score"] = max(0, min(100, int(one("min_score", str(DEFAULT_MIN_SCORE)))))
    except ValueError:
        raise ApiError(400, "min_score must be a number")
    cats = [c for c in one("categories").split(",") if c in CATEGORY_KEYS]
    if cats:
        filters["categories"] = cats
    srcs = [s for s in one("sources").split(",") if s in SOURCES]
    if srcs:
        filters["sources"] = srcs
    if one("added_since"):
        try:
            datetime.fromisoformat(one("added_since").replace("Z", "+00:00"))
        except ValueError:
            raise ApiError(400, "added_since must be an ISO date")
        filters["added_since"] = one("added_since")
    if one("q"):
        filters["q"] = one("q")[:80]
    return filters


class App:
    def __init__(self, store: Store, updater: Updater,
                 session_factory: Callable[[], requests.Session] = requests.Session, read_only: bool = False):
        self.store = store
        self.updater = updater
        self.session_factory = session_factory
        self.read_only = read_only
        self._search_cache = TTLCache()
        self._photo_cache = TTLCache()

    def page(self) -> bytes:
        boot = {
            "version": __version__,
            "categories": [[k, label] for k, label, _ in CATEGORIES] + [list(OTHER_CATEGORY)],
            "sources": [[s, SOURCE_LABELS[s]] for s in SOURCES],
            "min_score": DEFAULT_MIN_SCORE,
            "uk_bbox": UK_BBOX,
            "read_only": self.read_only,
        }
        template = resources.files("bandobuddy").joinpath("app.html").read_text(encoding="utf-8")
        return template.replace("__BOOT__", json.dumps(boot).replace("<", "\\u003c")).encode("utf-8")

    def health(self) -> dict:
        with self.store.connect() as db:
            sites = db.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
        return {"ok": True, "version": __version__, "sites": sites}

    def sites(self, qs: dict) -> dict:
        filters = parse_filters(qs)
        total, rows = self.store.query_sites(limit=MAP_LIMIT, **filters)
        return {"total": total, "sites": rows, "version": self.updater.sites_version}

    def site(self, key: str) -> dict:
        site = self.store.get_site(key)
        if not site:
            raise ApiError(404, "Place not found")
        site["links"] = export.links(site)
        return site

    def photos(self, qs: dict) -> dict:
        try:
            lat, lng = float(qs["lat"][0]), float(qs["lng"][0])
        except (KeyError, ValueError):
            raise ApiError(400, "lat and lng required")
        key = (round(lat, 5), round(lng, 5))
        cached = self._photo_cache.get(key)
        if cached is not None:
            return cached
        try:
            result = {"photos": imagery.nearby_photos(lat, lng, self.session_factory())}
        except requests.RequestException as exc:
            raise ApiError(502, f"Panoramax unavailable: {type(exc).__name__}")
        self._photo_cache.put(key, result)
        return result

    def search(self, q: str) -> dict:
        q = q.strip()[:200]
        if not q:
            raise ApiError(400, "Type a place to search for.")
        hit = self._search_cache.get(q.lower())
        if hit is None:
            try:
                hit = geocode.search(q, self.session_factory()) or {}
            except requests.RequestException as exc:
                raise ApiError(502, f"Search unavailable: {type(exc).__name__}")
            self._search_cache.put(q.lower(), hit)
        if not hit:
            raise ApiError(404, f"Couldn't find '{q}'.")
        return hit

    def status(self) -> dict:
        st = self.updater.status()
        st["read_only"] = self.read_only
        if self.read_only:  # visitors don't need server paths or logs
            st["data_dir"] = None
            st["log"] = []
        return st

    def update(self, body: dict) -> dict:
        wanted = body.get("source", "all")
        sources = SOURCES if wanted == "all" else [wanted] if wanted in SOURCES else None
        if not sources:
            raise ApiError(400, "Unknown source")
        started = [s for s in sources if self.updater.start(s, by_user=True)]
        return {"started": started}

    def pause(self, body: dict) -> dict:
        wanted = body.get("source", "all")
        for s in SOURCES if wanted == "all" else [wanted] if wanted in SOURCES else []:
            self.updater.pause(s)
        return {"ok": True}

    def settings(self, body: dict) -> dict:
        if "auto_update" in body:
            self.store.set_setting("auto_update", bool(body["auto_update"]))
        if "update_days" in body:
            try:
                days = int(body["update_days"])
            except (TypeError, ValueError):
                raise ApiError(400, "update_days must be a whole number")
            self.store.set_setting("update_days", max(1, min(90, days)))
        return self.status()

    def export(self, qs: dict) -> tuple[bytes, str, str]:
        fmt = (qs.get("format") or ["csv"])[0]
        if fmt not in export.FORMATS:
            raise ApiError(400, "format must be csv, kml or gpx")
        sites = self.store.full_sites(**parse_filters(qs))
        render, ctype = export.FORMATS[fmt]
        return render(sites).encode("utf-8"), ctype, f"bandobuddy-{date.today().isoformat()}.{fmt}"


def host_only(value: str) -> str:
    """'Example.org:8080' -> 'example.org'; '[::1]:8642' -> '[::1]'."""
    v = value.strip().lower()
    if v.startswith("["):
        return v.split("]", 1)[0] + "]"
    return v.split(":", 1)[0]


def make_handler(app: App, allowed_hosts: Iterable[str] = ()) -> type[BaseHTTPRequestHandler]:
    allowed = LOCAL_HOSTS | {host_only(h) for h in allowed_hosts if h}

    def host_ok(value: str | None) -> bool:
        return bool(value) and ("*" in allowed or host_only(value) in allowed)

    class Handler(BaseHTTPRequestHandler):
        server_version = f"bandobuddy/{__version__}"

        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            # Map tile servers (OpenStreetMap's included) require a Referer, so don't strip it.
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data).encode("utf-8"), "application/json")

        def _refuse(self, status: int, message: str) -> bool:
            # Read (and discard) any request body first, or some clients see a reset instead of the error.
            length = int(self.headers.get("Content-Length") or 0)
            if 0 < length <= MAX_BODY_BYTES:
                self.rfile.read(length)
            self._json(status, {"error": message})
            return False

        def _guard(self, post: bool) -> bool:
            if not host_ok(self.headers.get("Host")):
                return self._refuse(403, "Forbidden host")
            if post:
                origin = self.headers.get("Origin")
                if origin and not host_ok(urlparse(origin).netloc):
                    return self._refuse(403, "Cross-origin request refused")
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    return self._refuse(415, "Expected JSON")
                if app.read_only:
                    return self._refuse(403, "This is a read-only copy of bandobuddy")
            return True

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                raise ApiError(413, "Request too large")
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise ApiError(400, "Invalid JSON")
            if not isinstance(data, dict):
                raise ApiError(400, "Expected a JSON object")
            return data

        def _dispatch(self, fn) -> None:
            try:
                result = fn()
            except ApiError as exc:
                self._json(exc.status, {"error": str(exc)})
            except Exception as exc:
                traceback.print_exc()
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            else:
                self._json(200, result)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/healthz":  # probes come from the orchestrator with its own Host header
                self._dispatch(app.health)
                return
            if not self._guard(post=False):
                return
            qs = parse_qs(url.query)
            path = url.path
            if path == "/":
                self._send(200, app.page(), "text/html; charset=utf-8")
            elif path == "/api/sites":
                self._dispatch(lambda: app.sites(qs))
            elif path.startswith("/api/site/"):
                self._dispatch(lambda: app.site(unquote(path[len("/api/site/"):])))
            elif path == "/api/photos":
                self._dispatch(lambda: app.photos(qs))
            elif path == "/api/search":
                self._dispatch(lambda: app.search((qs.get("q") or [""])[0]))
            elif path == "/api/status":
                self._dispatch(app.status)
            elif path == "/api/export":
                try:
                    body, ctype, name = app.export(qs)
                except ApiError as exc:
                    self._json(exc.status, {"error": str(exc)})
                    return
                self._send(200, body, ctype, {"Content-Disposition": f'attachment; filename="{name}"'})
            else:
                self._json(404, {"error": "Not found"})

        def do_POST(self):
            if not self._guard(post=True):
                return
            path = urlparse(self.path).path
            routes = {"/api/update": app.update, "/api/pause": app.pause, "/api/settings": app.settings}
            if path in routes:
                self._dispatch(lambda: routes[path](self._body()))
            else:
                self._json(404, {"error": "Not found"})

    return Handler


def make_server(app: App, port: int = DEFAULT_PORT, host: str = "127.0.0.1",
                allowed_hosts: Iterable[str] = (), fallback: bool = True) -> ThreadingHTTPServer:
    """Bind to the port (trying the next few, then any free port, if fallback is on)."""
    handler = make_handler(app, allowed_hosts)
    candidates = [*range(port, port + 10), 0] if port and fallback else [port]
    last: OSError | None = None
    for candidate in candidates:
        try:
            return ThreadingHTTPServer((host, candidate), handler)
        except OSError as exc:
            last = exc
    raise OSError(f"Could not listen on {host}:{port}: {last}")


def run_scheduler(updater: Updater, stop: threading.Event, check_every: float = SCHEDULE_CHECK_S) -> None:
    """Start any source that's due (never built, interrupted, or older than the schedule)."""
    stop.wait(3)
    while not stop.is_set():
        for src in updater.due_sources():
            if updater.start(src):
                updater.log(f"{SOURCE_LABELS[src]}: update started (scheduled)")
        stop.wait(check_every)


def serve(data_dir: Path, port: int = DEFAULT_PORT, open_browser: bool = True, auto_update: bool = True,
          session_factory: Callable[[], requests.Session] = requests.Session, host: str = "127.0.0.1",
          read_only: bool = False, allowed_hosts: Iterable[str] = ()) -> None:
    store = Store(data_dir / DB_NAME)
    updater = Updater(store, data_dir, session_factory=session_factory)
    app = App(store, updater, session_factory, read_only=read_only)
    # In a container the published port is fixed, so never quietly move to another one.
    server = make_server(app, port, host, allowed_hosts, fallback=host == "127.0.0.1")
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"bandobuddy {__version__} listening on http://{shown}:{server.server_address[1]}/"
          f"{' (read-only public mode)' if read_only else ''}")
    print(f"Data is kept in {data_dir}")

    stop = threading.Event()

    def request_stop(signum, frame):  # docker stop / kubectl delete send SIGTERM
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    # Re-score what's already stored (picks up any rule changes), then keep things up to date.
    threading.Thread(target=lambda: (build_sites(store), setattr(updater, "sites_version", updater.sites_version + 1)),
                     daemon=True).start()
    if auto_update:
        threading.Thread(target=run_scheduler, args=(updater, stop), daemon=True).start()
    if open_browser:
        webbrowser.open(f"http://{shown}:{server.server_address[1]}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping; any update in progress will carry on from here next time.")
        stop.set()
        for src in SOURCES:
            updater.pause(src)
        updater.join(timeout=20)
        server.server_close()
