"""Command line entry point.

    bandobuddy                          open the map (and keep the UK data up to date in the background)
    bandobuddy update [--source osm]    build/refresh the data without the UI (e.g. from a scheduler)
    bandobuddy export --near 51.34,-2.25 --radius 5 --format gpx -o spots.gpx

Every server option can also come from a BANDOBUDDY_* environment variable (handy in containers).
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

from . import __version__, export
from .config import DATA_DIR, DB_NAME, WEAK_BELOW
from .store import Store
from .updater import SOURCES, Updater


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str) -> list[str]:
    return [h.strip() for h in os.environ.get(name, "").split(",") if h.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bandobuddy",
                                description="A free, open map of likely-abandoned places across the UK.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--data-dir", type=Path, default=DATA_DIR,
                   help="where the database and map extract live [BANDOBUDDY_DATA] (default: %(default)s)")
    p.add_argument("--host", default=os.environ.get("BANDOBUDDY_HOST", "127.0.0.1"),
                   help="address to listen on; 0.0.0.0 in a container [BANDOBUDDY_HOST] (default: %(default)s)")
    p.add_argument("--port", type=int, default=int(os.environ.get("BANDOBUDDY_PORT") or 8642),
                   help="port for the map [BANDOBUDDY_PORT] (default: %(default)s)")
    p.add_argument("--public", action="store_true", default=_env_flag("BANDOBUDDY_PUBLIC"),
                   help="read-only mode for visitors: no update/settings controls [BANDOBUDDY_PUBLIC]")
    p.add_argument("--allowed-host", action="append", default=None, metavar="HOSTNAME",
                   help="extra hostname the site is served on, e.g. bandobuddy.example.org; repeatable "
                        "[BANDOBUDDY_ALLOWED_HOSTS, comma-separated]")
    p.add_argument("--no-browser", action="store_true", default=_env_flag("BANDOBUDDY_NO_BROWSER"),
                   help="don't open a browser tab automatically [BANDOBUDDY_NO_BROWSER]")
    p.add_argument("--no-auto-update", action="store_true", default=_env_flag("BANDOBUDDY_NO_AUTO_UPDATE"),
                   help="don't refresh the data in the background [BANDOBUDDY_NO_AUTO_UPDATE]")

    sub = p.add_subparsers(dest="command", metavar="{update,export}")
    up = sub.add_parser("update", help="build or refresh the UK data now, then exit")
    up.add_argument("--source", choices=[*SOURCES, "all"], default="all")

    ex = sub.add_parser("export", help="write places to CSV/KML/GPX")
    ex.add_argument("--near", metavar="LAT,LNG", help="centre point")
    ex.add_argument("--radius", type=float, default=5, help="km around --near (default: %(default)s)")
    ex.add_argument("--bbox", metavar="W,S,E,N", help="or a bounding box instead of --near")
    ex.add_argument("--include-weak", action="store_true",
                    help="also export weak leads (closed shop units, heritage ruins, caves, brownfield)")
    ex.add_argument("--format", choices=sorted(export.FORMATS), default="gpx")
    ex.add_argument("-o", "--output", type=Path, required=True)
    return p


def run_update(args: argparse.Namespace) -> int:
    store = Store(args.data_dir / DB_NAME)
    updater = Updater(store, args.data_dir)
    sources = SOURCES if args.source == "all" else (args.source,)
    ok = True
    for src in sources:
        try:
            updater.run(src)
        except KeyboardInterrupt:
            print("\nStopped; the next update carries on from here.", file=sys.stderr)
            return 130
        except RuntimeError as exc:
            print(f"{src}: {exc}", file=sys.stderr)
            ok = False
    return 0 if ok else 1


def run_export(args: argparse.Namespace) -> int:
    if args.bbox:
        bbox = tuple(float(x) for x in args.bbox.split(","))
    elif args.near:
        lat, lng = (float(x) for x in args.near.split(","))
        dlat = args.radius / 111.32
        dlng = args.radius / (111.32 * math.cos(math.radians(lat)))
        bbox = (lng - dlng, lat - dlat, lng + dlng, lat + dlat)
    else:
        print("Give --near LAT,LNG or --bbox W,S,E,N", file=sys.stderr)
        return 2
    store = Store(args.data_dir / DB_NAME)
    sites = store.full_sites(bbox=bbox, min_score=0 if args.include_weak else WEAK_BELOW)
    render, _ = export.FORMATS[args.format]
    args.output.write_text(render(sites), encoding="utf-8")
    print(f"Wrote {len(sites)} places to {args.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "update":
        return run_update(args)
    if args.command == "export":
        return run_export(args)

    from .webapp import serve

    serve(args.data_dir, port=args.port, open_browser=not args.no_browser, auto_update=not args.no_auto_update,
          host=args.host, read_only=args.public,
          allowed_hosts=(args.allowed_host or []) + _env_list("BANDOBUDDY_ALLOWED_HOSTS"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
