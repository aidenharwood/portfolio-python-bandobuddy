"""Get the Geofabrik UK extract onto disk and keep it current.

First run downloads the whole file (~2.3 GB; resumable, checked against Geofabrik's .md5). After
that, the daily change files Geofabrik publishes are applied to it, which is a few MB a day
instead of another full download.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Callable

import requests

from .config import USER_AGENT
from .osm import Cancelled

CHUNK = 1 << 20
Progress = Callable[[str, int, "int | None"], None]


def _noop(stage: str, done: int, total: int | None) -> None:
    pass


def download(url: str, dest: Path, session: requests.Session, progress: Progress = _noop,
             cancel: threading.Event | None = None) -> None:
    """Download url to dest, resuming a previous partial download if the file hasn't changed since."""
    cancel = cancel or threading.Event()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    meta_path = dest.with_name(dest.name + ".part.json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() and part.exists() else {}
    have = part.stat().st_size if part.exists() and meta else 0

    headers = {"User-Agent": USER_AGENT}
    if have and meta.get("last_modified"):
        headers["Range"] = f"bytes={have}-"
        headers["If-Range"] = meta["last_modified"]  # file changed upstream? then start again
    with session.get(url, headers=headers, stream=True, timeout=60) as resp:
        if resp.status_code == 206:
            mode = "ab"
        elif resp.status_code == 200:
            mode, have = "wb", 0
            meta = {"last_modified": resp.headers.get("Last-Modified")}
            meta_path.write_text(json.dumps(meta))
        else:
            raise RuntimeError(f"download failed: HTTP {resp.status_code} for {url}")
        total = have + int(resp.headers.get("Content-Length") or 0)
        with part.open(mode) as f:
            for chunk in resp.iter_content(CHUNK):
                if cancel.is_set():
                    raise Cancelled()
                f.write(chunk)
                have += len(chunk)
                progress("Downloading UK map data", have, total or None)

    expected = _published_md5(url, session)
    if expected:
        progress("Checking the download", 0, None)
        digest = hashlib.md5()
        with part.open("rb") as f:
            for chunk in iter(lambda: f.read(CHUNK * 8), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            part.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            raise RuntimeError("download was corrupted (checksum mismatch); it will start again next time")
    part.replace(dest)
    meta_path.unlink(missing_ok=True)


def _published_md5(url: str, session: requests.Session) -> str | None:
    try:
        resp = session.get(url + ".md5", headers={"User-Agent": USER_AGENT}, timeout=30)
        if resp.status_code == 200 and resp.text.strip():
            return resp.text.split()[0].lower()
    except requests.RequestException:
        pass
    return None


def data_timestamp(pbf: Path) -> str | None:
    """When the extract's data was last brought up to date (ISO string), if it says."""
    try:
        from osmium.replication.utils import get_replication_header

        ts = get_replication_header(str(pbf)).timestamp
        return ts.isoformat(timespec="seconds") if ts else None
    except Exception:
        return None


def refresh(url: str, dest: Path, session: requests.Session, progress: Progress = _noop,
            cancel: threading.Event | None = None, log: Callable[[str], None] = print) -> str:
    """Make sure dest exists and is current. Returns 'downloaded', 'updated' or 'current'."""
    if not dest.exists():
        download(url, dest, session, progress, cancel)
        return "downloaded"
    try:
        return _apply_updates(dest, progress, cancel)
    except Cancelled:
        raise
    except Exception as exc:  # updates missing/too old/corrupt: a fresh download always works
        log(f"  could not apply updates ({type(exc).__name__}: {exc}); downloading a fresh copy instead")
        dest.with_name(dest.name + ".updating.pbf").unlink(missing_ok=True)
        download(url, dest, session, progress, cancel)
        return "downloaded"


def _apply_updates(dest: Path, progress: Progress, cancel: threading.Event | None) -> str:
    from osmium.replication.server import ReplicationServer
    from osmium.replication.utils import get_replication_header

    header = get_replication_header(str(dest))
    if not header.url or header.sequence is None:
        raise ValueError("extract has no update information")
    tmp = dest.with_name(dest.name + ".updating.pbf")
    changed = False
    server = ReplicationServer(header.url)
    try:
        server.set_request_parameter("headers", {"User-Agent": USER_AGENT})
        latest = server.get_state_info()
        if latest is None:
            raise ValueError("update server unavailable")
        seq = header.sequence
        while seq < latest.sequence:
            if cancel and cancel.is_set():
                raise Cancelled()
            progress("Applying map updates", seq - header.sequence, latest.sequence - header.sequence)
            result = server.apply_diffs_to_file(str(dest), str(tmp), seq + 1, max_size=512)
            if result is None:
                break
            tmp.replace(dest)
            changed = True
            seq = result[0]
    finally:
        server.close()
    return "updated" if changed else "current"
