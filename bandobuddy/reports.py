"""What visitors say about a place, from fixed choices only: how hard it is to get in (1 to 5), whether they could,
and a few tags ("well preserved", "trashed"...). No free text, so there's nothing to moderate.

One report per device per place, changed or taken back at will. Each time a place is marked accessible or
inaccessible it's also logged, with the day, so a place keeps its history ("accessible in March, sealed up by
June"). A device is never stored: each place sees a different hash of it, so one device's visits can't be linked."""
from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta

from .config import REPORT_TAGS, REPORT_YEARS

TAG_KEYS = [key for key, _, _ in REPORT_TAGS]
ACCESS = ("accessible", "inaccessible")
HISTORY_SHOWN = 12
_DEVICE = re.compile(r"[A-Za-z0-9-]{16,64}")


class Invalid(ValueError):
    """A report that isn't one of the fixed choices."""


def valid_device(device) -> bool:
    return isinstance(device, str) and bool(_DEVICE.fullmatch(device))


def reporter_id(device: str, key: str) -> str:
    """A device as one place sees it: the same device reporting on another place can't be told to be the same."""
    return hashlib.blake2b(f"{device}\x1f{key}".encode("utf-8"), digest_size=12).hexdigest()


def parse(body: dict) -> dict:
    """A report as sent, checked against the fixed choices: {key, device, clear} or {key, device, difficulty,
    access, tags}."""
    key, device = body.get("key"), body.get("device")
    if not isinstance(key, str) or not key or len(key) > 200:
        raise Invalid("Which place?")
    if not isinstance(device, str) or not _DEVICE.fullmatch(device):
        raise Invalid("This device has no id")
    if body.get("clear"):
        return {"key": key, "device": device, "clear": True}
    difficulty = body.get("difficulty")
    if difficulty is not None and (isinstance(difficulty, bool) or not isinstance(difficulty, int)
                                   or not 1 <= difficulty <= 5):
        raise Invalid("Access difficulty is 1 to 5")
    access = body.get("access")
    if access is not None and access not in ACCESS:
        raise Invalid("Accessible or inaccessible")
    tags = body.get("tags") or []
    if not isinstance(tags, list) or any(t not in TAG_KEYS for t in tags):
        raise Invalid("Unknown tag")
    if difficulty is None and access is None and not tags:
        raise Invalid("Nothing to report")
    return {"key": key, "device": device, "clear": False, "difficulty": difficulty, "access": access,
            "tags": sorted(set(tags), key=TAG_KEYS.index)}


def summarize(rows: list[dict], history: list[dict] | None = None, today: date | None = None) -> dict | None:
    """What a place's reports add up to: {n, difficulty (the average, to one place), levels (how many said 1..5),
    accessible, inaccessible, tags {tag: n}, latest} and, when asked for, its access history (newest first).
    Reports older than REPORT_YEARS don't count: a place changes."""
    cutoff = ((today or date.today()) - timedelta(days=round(365.25 * REPORT_YEARS))).isoformat()
    recent = [r for r in rows if (r.get("at") or "") >= cutoff]
    if not recent and not history:
        return None
    levels = [0] * 5
    tags: dict[str, int] = {}
    for r in recent:
        if r.get("difficulty"):
            levels[r["difficulty"] - 1] += 1
        for tag in r.get("tags") or []:
            tags[tag] = tags.get(tag, 0) + 1
    rated = [r["difficulty"] for r in recent if r.get("difficulty")]
    summary = {
        "n": len(recent),
        "difficulty": round(sum(rated) / len(rated), 1) if rated else None,
        "rated": len(rated),
        "levels": levels,
        "accessible": sum(1 for r in recent if r.get("access") == "accessible"),
        "inaccessible": sum(1 for r in recent if r.get("access") == "inaccessible"),
        "tags": dict(sorted(tags.items(), key=lambda kv: (-kv[1], TAG_KEYS.index(kv[0])))),
        "latest": max((r["at"][:10] for r in recent), default=None),
    }
    if history is not None:
        summary["history"] = [{"on": h["at"][:10], "access": h["access"], "difficulty": h.get("difficulty")}
                              for h in sorted(history, key=lambda h: h["at"], reverse=True)[:HISTORY_SHOWN]]
        if summary["history"]:
            summary["latest"] = max(summary["latest"] or "", summary["history"][0]["on"])
    return summary


def mine(row: dict | None) -> dict | None:
    return {"difficulty": row.get("difficulty"), "access": row.get("access"), "tags": row.get("tags") or [],
            "at": row["at"][:10]} if row else None
