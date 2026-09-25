"""Wayback Machine lookups: list a page's archived snapshots, or find the one closest to a date.
Used standalone, and by fetch.py as the last-resort fallback when every live stage fails."""
import asyncio
import json
import re
from urllib.parse import quote

import fetch

CDX = "https://web.archive.org/cdx/search/cdx"
AVAILABLE = "https://archive.org/wayback/available"
RETRIES = 3  # archive.org's APIs return transient 5xx/400s under load often enough to need this


async def _get_json(q: str):
    """get_checked against archive.org, retried: its APIs are flaky enough that a single-shot
    request meant to be a *fallback* would defeat its own purpose."""
    for attempt in range(RETRIES):
        # chrome/firefox/safari TLS fingerprints hang indefinitely against archive.org; plain curl doesn't
        _, _, body = await fetch.get_checked(q, timeout=15, impersonate=None)
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            if attempt == RETRIES - 1:
                page = body.decode(errors="replace")
                reason = re.search(r"<title>(.*?)</title>|<h1>(.*?)</h1>", page, re.IGNORECASE | re.DOTALL)
                said = next(g for g in reason.groups() if g) if reason else page[:80]
                raise RuntimeError(f"archive.org answered {said.strip()!r} instead of data") from e
            await asyncio.sleep(1)
    return None  # unreachable


async def snapshots(url: str, limit: int = 20, year_from: int | None = None, year_to: int | None = None) -> str:
    """Archived snapshots of url, newest first, deduped by content (collapse=digest)."""
    q = (f"{CDX}?url={quote(url, safe='')}&output=json"
        f"&fl=timestamp,original,statuscode,mimetype,length&collapse=digest&limit={limit}")
    if year_from:
        q += f"&from={year_from}"
    if year_to:
        q += f"&to={year_to}"
    rows = await _get_json(q)
    if len(rows) <= 1:  # first row is the header
        return f"no Wayback snapshots found for {url}"
    lines = [f"{len(rows) - 1} Wayback snapshot(s) of {url}:"]
    for ts, original, status, mimetype, _length in rows[1:]:
        date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
        lines.append(f"{date} (HTTP {status}, {mimetype}): https://web.archive.org/web/{ts}/{original}")
    return "\n".join(lines)


async def closest(url: str, timestamp: str = "") -> str | None:
    """Replay URL of the snapshot closest to timestamp (YYYYMMDDhhmmss..., default now), or None
    if archive.org has never captured this page."""
    q = f"{AVAILABLE}?url={quote(url, safe='')}"
    if timestamp:
        q += f"&timestamp={timestamp}"
    data = await _get_json(q)
    snap = data.get("archived_snapshots", {}).get("closest")
    if not snap or not snap.get("available"):
        return None
    return snap["url"]
