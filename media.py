"""Search books, comics, manga, anime, movies, TV and games across many sources at once.

Each source is one small async function: query -> list of results. They use the site's
lightest endpoint (JSON API, RSS, or its search page), never its ad-laden detail pages.
Every result is a dict with: source, title, url, and when known size, seeders, year,
info (author/format/quality), magnet, md5.

Politeness: one request per source per search, at most one request per second per host,
and results are cached for an hour. Sites your ISP blocks are retried through Tor
(TOR_PROXY, default socks5h://127.0.0.1:9050) and remembered as Tor-only for the session.
"""
import asyncio
import base64
import hashlib
import html as htmllib
import os
import re
import time
from email.message import Message
from pathlib import Path
from urllib.parse import quote, urlparse

from curl_cffi import AsyncSession
from rapidfuzz import fuzz

import fetch
import health
import mirrors
import movies
import providers
import quality
from store import STATE, load_json, save_json

TOR = os.environ.get("TOR_PROXY", "socks5h://127.0.0.1:9050")
CACHE_TTL = 3600
_cache: dict[tuple, tuple[float, list]] = {}
_via_tor: set[str] = set()
_last_hit: dict[str, float] = {}
_host_locks: dict[str, asyncio.Lock] = {}


# ---------------------------------------------------------------- HTTP

async def _polite(host: str) -> None:
    """At most one request per second per host."""
    lock = _host_locks.setdefault(host, asyncio.Lock())
    async with lock:
        wait = _last_hit.get(host, 0) + 1 - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_hit[host] = time.time()


async def http(url: str, json_body: dict | None = None, timeout: int = 12, form: dict | None = None,
               cookies: dict | None = None, headers: dict | None = None):
    """GET (or POST json_body) with a Chrome fingerprint. Falls back to Tor when the
    connection itself fails, which is what an ISP block looks like; a slow site is not retried."""
    host = urlparse(url).hostname or ""
    await _polite(host)
    routes = [TOR] if host in _via_tor else [None, TOR]
    error = None
    for proxy in routes:
        try:
            async with AsyncSession() as s:
                r = await s.request("POST" if json_body or form else "GET", url, json=json_body, data=form,
                                    cookies=cookies, headers=headers, impersonate="chrome", proxy=proxy,
                                    timeout=timeout * 2 if proxy else timeout)
        except Exception as e:
            if not fetch.is_unreachable(e):
                raise
            error = e  # connection cut before any HTTP: try the next route
            continue
        if proxy:
            _via_tor.add(host)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r
    raise RuntimeError(f"unreachable directly and via Tor: {error}"[:200])


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def _size_bytes(size: str) -> int | None:
    """Inverse of _size(): recovers raw bytes from a result's formatted "4.3 GB" string, for
    quality.classify's undersized-file check. None if the string doesn't parse."""
    m = re.match(r"([\d.]+)\s*([KMGTP]?B)", size or "", re.IGNORECASE)
    if not m:
        return None
    scale = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4, "PB": 1024**5}
    return int(float(m.group(1)) * scale[m.group(2).upper()])


def _hex_hash(infohash: str) -> str:
    """Infohashes come as 40 hex chars or 32 base32 chars; use hex so duplicates match.
    Raises binascii.Error on a malformed hash; use _try_hex_hash to drop just that item."""
    if len(infohash) == 32:
        return base64.b32decode(infohash.upper()).hex()
    return infohash.lower()


def _try_hex_hash(infohash: str) -> str | None:
    """None on a malformed hash, so the caller drops that one item, not the source's whole list."""
    try:
        return _hex_hash(infohash)
    except Exception:  # noqa: BLE001 - any decode failure means this one item is unusable
        return None


def _magnet(hexhash: str, name: str) -> str:
    return f"magnet:?xt=urn:btih:{hexhash}&dn={quote(name)}"


# ---------------------------------------------------------------- torrents

async def knaben(query: str, limit: int, category: str = "") -> list[dict]:
    """Knaben: a meta-index over The Pirate Bay, 1337x, Nyaa, RuTracker and others."""
    r = await http("https://api.knaben.org/v1", {
        "query": query, "size": limit * 3, "order_by": "seeders", "order_direction": "desc",
        "hide_unsafe": True, "hide_xxx": True})
    hits = r.json()["hits"]
    prefix = {"movies": "Movies", "tv": "TV", "anime": "Anime", "games": "PC Games",
              "books": "Books", "comics": "Books", "manga": "Anime", "music": "Audio"}.get(category)
    out = []
    for h in hits:
        if not h.get("hash") or (prefix and not (h.get("category") or "").startswith(prefix)):
            continue
        hexhash = _try_hex_hash(h["hash"])
        if not hexhash:
            continue
        out.append({"source": f"knaben/{h.get('cachedOrigin') or '?'}", "title": h["title"],
                    "size": _size(h.get("bytes")), "seeders": h.get("seeders") or 0,
                    "year": (h.get("date") or "")[:4], "info": h.get("category") or "",
                    "magnet": _magnet(hexhash, h["title"]), "hash": hexhash, "url": h.get("details") or ""})
        if len(out) >= limit:
            break
    return out


APIBAY_GROUPS = {"movies": {201, 202, 207, 209}, "tv": {205, 208}, "games": {400, 401, 404, 408},
                 "books": {601}, "comics": {602}, "anime": {201, 205, 207, 208}}


PIRATEBAY_ENRICH = 5  # fetch uploader status + file list for at most this many, and only the top ones


async def _piratebay_extra(tid: str) -> tuple[str, list[str]]:
    """Uploader trust status and any executable/script bundled inside the torrent's own file
    list — apibay exposes both for free, but only fetch it for a torrent we're about to show."""
    status, warnings = "", []
    try:
        t = (await http(f"https://apibay.org/t.php?id={tid}")).json()
        if t.get("status") in ("trusted", "vip"):
            status = t["status"]
    except Exception:  # noqa: BLE001, S110 - best-effort enrichment, never blocks the result
        pass
    try:
        files = (await http(f"https://apibay.org/f.php?id={tid}")).json()
        bad = [f["name"][0] for f in files if f.get("name")
               and f["name"][0].lower().endswith((".exe", ".scr", ".bat", ".cmd", ".msi", ".lnk", ".vbs"))]
        if bad:
            warnings.append(f"contains {', '.join(bad[:3])}")
    except Exception:  # noqa: BLE001, S110 - best-effort enrichment, never blocks the result
        pass
    return status, warnings


async def piratebay(query: str, limit: int, category: str = "") -> list[dict]:
    r = await http(f"https://apibay.org/q.php?q={quote(query)}")
    wanted = APIBAY_GROUPS.get(category)
    out = []
    for it in r.json():
        if it.get("id") == "0" or not it.get("info_hash"):
            continue  # apibay's "no results" row
        if wanted and int(it.get("category", 0)) not in wanted:
            continue
        hexhash = _try_hex_hash(it["info_hash"])
        if not hexhash:
            continue
        out.append({"source": "piratebay", "title": it["name"], "size": _size(it.get("size")),
                    "seeders": int(it.get("seeders", 0)), "year": time.strftime("%Y", time.gmtime(int(it.get("added", 0)))),
                    "info": f"cat {it.get('category')}", "magnet": _magnet(hexhash, it["name"]),
                    "hash": hexhash, "url": f"https://thepiratebay.org/description.php?id={it['id']}",
                    "_id": it["id"]})
    out = sorted(out, key=lambda x: -x["seeders"])[:limit]
    try:
        extras = await asyncio.wait_for(asyncio.gather(
            *(_piratebay_extra(r["_id"]) for r in out[:PIRATEBAY_ENRICH]), return_exceptions=True), 8)
    except TimeoutError:
        extras = []
    for r, extra in zip(out, extras):
        if isinstance(extra, Exception):
            continue
        status, warnings = extra
        if status:
            r["info"] = f"{r['info']} · {status} uploader" if r["info"] else f"{status} uploader"
        if warnings:
            r["warnings"] = warnings
    for r in out:
        r.pop("_id", None)
    return out


async def torrents_csv(query: str, limit: int, category: str = "") -> list[dict]:
    """Torrents-CSV: an open index built by crawling the DHT, so it has no domain to seize."""
    r = await http(f"https://torrents-csv.com/service/search?q={quote(query)}&size={limit}")
    out = []
    for t in r.json().get("torrents", []):
        hexhash = _try_hex_hash(t["infohash"])
        if not hexhash:
            continue
        out.append({"source": "torrents-csv", "title": t["name"], "size": _size(t.get("size_bytes")),
                    "seeders": t.get("seeders", 0), "year": time.strftime("%Y", time.gmtime(t.get("created_unix", 0))),
                    "magnet": _magnet(hexhash, t["name"]), "hash": hexhash, "url": ""})
    return out


async def yts(query: str, limit: int, category: str = "") -> list[dict]:
    """YTS movies: small, good-quality encodes. Its domain changes often, so mirrors apply."""
    async def attempt(base):
        r = await http(f"{base}/api/v2/list_movies.json?query_term={quote(query)}&limit={limit}")
        return r.json()["data"].get("movies") or []
    listings = await mirrors.call("yts", ["https://movies-api.accel.li", "https://yts.gg"],
                                  {"prowlarr": "yts"}, attempt)
    out = []
    for m in listings:
        for t in m.get("torrents", []):
            hexhash = _try_hex_hash(t["hash"])
            if not hexhash:
                continue
            name = f"{m['title_long']} [{t['quality']} {t.get('type', '')}]".strip()
            out.append({"source": "yts", "title": name, "size": _size(t.get("size_bytes")),
                        "seeders": t.get("seeds", 0), "year": str(m.get("year", "")),
                        "info": f"IMDb {m.get('imdb_code', '')} rating {m.get('rating', '')}",
                        "magnet": _magnet(hexhash, name), "hash": hexhash, "url": m.get("url", "")})
    return out


def _rss_tag(item: str, name: str) -> str:
    m = re.search(rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", item, re.DOTALL)
    return htmllib.unescape(m.group(1).strip()) if m else ""


async def nyaa(query: str, limit: int, category: str = "") -> list[dict]:
    """Nyaa: the main anime torrent tracker (also manga scans, J-drama, live action)."""
    cat = {"anime": "1_0", "manga": "3_0", "books": "3_0"}.get(category, "0_0")  # 3_0 = literature

    async def attempt(base):
        r = await http(f"{base}/?page=rss&q={quote(query)}&c={cat}&f=0")
        if "<rss" not in r.text:
            raise RuntimeError("not a Nyaa RSS feed")
        return r.text
    xml = await mirrors.call("nyaa", ["https://nyaa.si"], {"prowlarr": "nyaasi"}, attempt)
    out = []
    for item in xml.split("<item>")[1:limit + 1]:
        infohash, title = _rss_tag(item, "nyaa:infoHash"), _rss_tag(item, "title")
        hexhash = _try_hex_hash(infohash)
        if not hexhash:
            continue
        out.append({"source": "nyaa", "title": title, "size": _rss_tag(item, "nyaa:size"),
                    "seeders": int(_rss_tag(item, "nyaa:seeders") or 0),
                    "year": _rss_tag(item, "pubDate")[12:16], "info": _rss_tag(item, "nyaa:category"),
                    "magnet": _magnet(hexhash, title), "hash": hexhash, "url": _rss_tag(item, "guid")})
    return out


async def subsplease(query: str, limit: int, category: str = "") -> list[dict]:
    """SubsPlease: simulcast anime episodes, one clean release per episode."""
    r = await http(f"https://subsplease.org/api/?f=search&tz=UTC&s={quote(query)}")
    data = r.json()
    if not isinstance(data, dict):
        return []
    out = []
    for entry in data.values():
        best = next((d for res in ("1080", "720", "480") for d in entry.get("downloads", [])
                     if d.get("res") == res), None)
        if not best:
            continue
        title = f"{entry.get('show')} - {entry.get('episode')} [{best['res']}p]"
        infohash = re.search(r"btih:(\w+)", best["magnet"]).group(1)
        hexhash = _try_hex_hash(infohash)
        if not hexhash:
            continue
        out.append({"source": "subsplease", "title": title, "size": "", "seeders": 0,
                    "year": (entry.get("release_date") or "")[-4:], "magnet": _magnet(hexhash, title),
                    "hash": hexhash, "url": f"https://subsplease.org/shows/{entry.get('page', '')}"})
    return out[:limit]


async def animetosho(query: str, limit: int, category: str = "") -> list[dict]:
    """AnimeTosho: mirrors Nyaa, AniDex and nekoBT, with direct-download links too."""
    r = await http(f"https://animetosho.org/feed/json?q={quote(query)}")
    out = []
    for t in r.json()[:limit]:
        if not t.get("info_hash"):
            continue
        hexhash = _try_hex_hash(t["info_hash"])
        if not hexhash:
            continue
        out.append({"source": "animetosho", "title": t["title"], "size": _size(t.get("total_size")),
                    "seeders": t.get("seeders") or 0, "year": time.strftime("%Y", time.gmtime(t.get("timestamp", 0))),
                    "magnet": _magnet(hexhash, t["title"]), "hash": hexhash, "url": t.get("link", "")})
    return out


async def fitgirl(query: str, limit: int, category: str = "") -> list[dict]:
    """FitGirl repacks: PC game downloads come only from here because games are the one
    category that runs code on your machine. fitgirl-repacks.site is the only official domain."""
    r = await http(f"https://fitgirl-repacks.site/search/{quote(query)}/feed/rss2/")
    out = []
    for item in r.text.split("<item>")[1:]:
        magnet = re.search(r'(magnet:\?xt=urn:btih:[^"<\s]+)', htmllib.unescape(item))
        title = _rss_tag(item, "title")
        if not magnet or not all(w in title.lower() for w in query.lower().split()):
            continue  # site news ("Updates Digest") or WordPress's loose matches on other games
        out.append({"source": "fitgirl", "title": title, "size": "", "seeders": 0,
                    "year": _rss_tag(item, "pubDate")[12:16], "url": _rss_tag(item, "link"),
                    "magnet": magnet.group(1).split("&tr=")[0]})
    return out[:limit]


async def eztv(query: str, limit: int, category: str = "") -> list[dict]:
    """EZTV: TV episode torrents. Its API is keyed by IMDb id, so the show is looked up on
    TVmaze first. Add S01 or S01E02 to the query for one season/episode."""
    words = re.sub(r"\bs\d{1,2}(e\d{1,3})?\b", "", query, flags=re.IGNORECASE).strip()
    show = (await http(f"https://api.tvmaze.com/singlesearch/shows?q={quote(words)}")).json()
    imdb = ((show.get("externals") or {}).get("imdb") or "").removeprefix("tt")
    if not imdb:
        return []
    episode = re.search(r"\bs\d{1,2}(e\d{1,3})?\b", query, re.IGNORECASE)

    async def attempt(base):
        return (await http(f"{base}/api/get-torrents?imdb_id={imdb}&limit=100&page=1")).json()
    data = await mirrors.call("eztv", ["https://eztvx.to", "https://eztv.wf", "https://eztv.tf"],
                              {"prowlarr": "eztv"}, attempt)
    out = []
    for t in data.get("torrents") or []:
        if episode and episode.group(0).lower() not in t["filename"].lower():
            continue
        hexhash = _try_hex_hash(t["hash"])
        if not hexhash:
            continue
        out.append({"source": "eztv", "title": t["filename"], "size": _size(t.get("size_bytes")),
                    "seeders": t.get("seeds", 0), "year": time.strftime("%Y", time.gmtime(t.get("date_released_unix", 0))),
                    "info": f"{show['name']} S{t.get('season')}E{t.get('episode')}",
                    "magnet": _magnet(hexhash, t["filename"]), "hash": hexhash,
                    "url": t.get("episode_url", "")})
    return sorted(out, key=lambda x: -x["seeders"])[:limit]


LIME_GROUPS = {"movies": "Movies", "tv": "TV", "anime": "Anime", "games": "Games", "books": "Other - E-books",
               "music": "Music"}


async def limetorrents(query: str, limit: int, category: str = "") -> list[dict]:
    """LimeTorrents: a general torrent index, read through its search RSS feed."""
    async def attempt(base):
        r = await http(f"{base}/searchrss/{quote(query)}/")
        if "<rss" not in r.text:
            raise RuntimeError("not a LimeTorrents RSS feed")
        return r.text
    xml = await mirrors.call("limetorrents", ["https://www.limetorrents.fun"], {"prowlarr": "limetorrents"}, attempt)
    wanted = LIME_GROUPS.get(category)
    out = []
    for item in xml.split("<item>")[1:]:
        infohash = re.search(r"/torrent/([0-9A-Fa-f]{40})\.torrent", item)
        kind = _rss_tag(item, "category")
        if not infohash or (wanted and not kind.startswith(wanted)):
            continue
        hexhash = _try_hex_hash(infohash.group(1))
        if not hexhash:
            continue
        title = _rss_tag(item, "title")
        seeds = re.search(r"Seeds: (\d+)", item)
        out.append({"source": "limetorrents", "title": title, "size": _size(_rss_tag(item, "size")),
                    "seeders": int(seeds.group(1)) if seeds else 0, "year": _rss_tag(item, "pubDate")[7:11],
                    "info": kind, "magnet": _magnet(hexhash, title), "hash": hexhash,
                    "url": _rss_tag(item, "link")})
    return sorted(out, key=lambda x: -x["seeders"])[:limit]


X1337_CATEGORY = {"movies": "Movies", "tv": "TV"}
X1337_DETAILS = 6  # fetch the magnet from at most this many top-seeded rows


async def _x1337_detail(base: str, path: str) -> str | None:
    """The listing page carries no magnet, only a link to the torrent's own page."""
    try:
        page = await http(f"{base}{path}")
    except Exception:  # noqa: BLE001 - best-effort; that one row just won't have a magnet
        return None
    m = re.search(r'magnet:\?xt=urn:btih:[^"\'<>\s]+', page.text)
    return htmllib.unescape(m.group(0)) if m else None


async def x1337(query: str, limit: int, category: str = "") -> list[dict]:
    """1337x: a large general torrent index. Its main domains sit behind Cloudflare, so this
    only works through the 1337xx.to mirror, and (per http()) usually needs Tor."""
    cat = X1337_CATEGORY.get(category)
    if not cat:
        return []

    async def attempt(base):
        r = await http(f"{base}/category-search/{quote(query)}/{cat}/1/")
        if "table-list" not in r.text:
            raise RuntimeError("not a 1337x results page")
        return base, r.text
    base, page = await mirrors.call("1337x", ["https://1337xx.to", "https://1337x.to"],
                                    {"prowlarr": "1337x"}, attempt)
    rows = []
    for tr in page[page.find("table-list"):].split("<tr")[1:]:
        link = re.search(r'href="(/torrent/[^"]+)"[^>]*>([^<]+)<', tr)
        if not link:
            continue
        size = re.search(r'coll-4[^"]*">\s*([\d.]+\s*[KMGT]i?B)', tr)
        seeds = re.search(r'coll-2[^"]*">\s*(\d+)', tr)
        rows.append({"path": link.group(1), "title": _text(link.group(2)),
                    "size": (size.group(1) if size else "").replace("iB", "B"),
                    "seeders": int(seeds.group(1)) if seeds else 0})
    rows = sorted(rows, key=lambda x: -x["seeders"])[:X1337_DETAILS]
    magnets = await asyncio.gather(*(_x1337_detail(base, r["path"]) for r in rows))
    out = []
    for row, magnet in zip(rows, magnets):
        if not magnet:
            continue
        infohash = re.search(r"urn:btih:([a-zA-Z0-9]+)", magnet)
        hexhash = _try_hex_hash(infohash.group(1)) if infohash else None
        if not hexhash:
            continue
        out.append({"source": "1337x", "title": row["title"], "size": row["size"],
                    "seeders": row["seeders"], "year": "", "magnet": magnet, "hash": hexhash,
                    "url": f"{base}{row['path']}"})
    return out[:limit]


async def torrentio(query: str, limit: int, category: str = "") -> list[dict]:
    """Torrentio: a keyless aggregator over public trackers, keyed by IMDb id (movies.imdb_id).
    For TV it needs one specific episode, so it only runs when the query has "s01e02"."""
    episode = re.search(r"\bs(\d{1,2})e(\d{1,3})\b", query, re.IGNORECASE)
    if category == "tv" and not episode:
        return []
    words = re.sub(r"\bs\d{1,2}(e\d{1,3})?\b", "", query, flags=re.IGNORECASE).strip() if episode else query
    tt = await movies.imdb_id(words, "tv" if episode else category)
    if not tt:
        return []
    kind = f"series/{tt}:{int(episode.group(1))}:{int(episode.group(2))}" if episode else f"movie/{tt}"
    r = await http(f"https://torrentio.strem.fun/stream/{kind}.json", timeout=20)
    out = []
    for s in (r.json().get("streams") or [])[:limit]:
        if not s.get("infoHash"):
            continue
        hexhash = _try_hex_hash(s["infoHash"])
        if not hexhash:
            continue
        name = (s.get("behaviorHints") or {}).get("filename") or s["title"].split("\n")[0]
        seeders = re.search(r"👤 ?(\d+)", s["title"])
        size = re.search(r"💾 ?([\d.]+ ?\w+)", s["title"])
        tracker = re.search(r"⚙️ ?(\S+)", s["title"])
        out.append({"source": "torrentio", "title": name, "size": size.group(1) if size else "",
                    "seeders": int(seeders.group(1)) if seeders else 0, "year": "",
                    "info": f"via {tracker.group(1)}" if tracker else "",
                    "magnet": _magnet(hexhash, name), "hash": hexhash, "url": ""})
    return out


async def prowlarr(query: str, limit: int, category: str = "") -> list[dict]:
    """Your own Prowlarr instance, if configured (PROWLARR_URL + PROWLARR_API_KEY): every
    indexer you've added there, searched at once. A no-op (empty list) when not configured."""
    base, key = os.environ.get("PROWLARR_URL"), os.environ.get("PROWLARR_API_KEY")
    if not base or not key:
        return []
    r = await http(f"{base.rstrip('/')}/api/v1/search?query={quote(query)}&type=search&apikey={key}")
    out = []
    for it in r.json()[:limit]:
        link = it.get("magnetUrl") or it.get("downloadUrl") or ""
        entry = {"source": f"prowlarr/{it.get('indexer', '?')}", "title": it.get("title", ""),
                "size": _size(it.get("size")), "seeders": it.get("seeders") or 0,
                "year": str(it.get("publishDate") or "")[:4],
                "magnet": link if link.startswith("magnet:") else "",
                "url": it.get("infoUrl") or (link if not link.startswith("magnet:") else "")}
        if it.get("infoHash") and (hexhash := _try_hex_hash(it["infoHash"])):
            entry["hash"] = hexhash
        out.append(entry)
    return out


async def jackett(query: str, limit: int, category: str = "") -> list[dict]:
    """Your own Jackett instance, if configured (JACKETT_URL + JACKETT_API_KEY): every indexer
    you've added there, searched at once. A no-op (empty list) when not configured."""
    base, key = os.environ.get("JACKETT_URL"), os.environ.get("JACKETT_API_KEY")
    if not base or not key:
        return []
    r = await http(f"{base.rstrip('/')}/api/v2.0/indexers/all/results?apikey={key}&Query={quote(query)}")
    out = []
    for it in r.json().get("Results", [])[:limit]:
        link = it.get("MagnetUri") or it.get("Link") or ""
        entry = {"source": f"jackett/{it.get('Tracker', '?')}", "title": it.get("Title", ""),
                "size": _size(it.get("Size")), "seeders": it.get("Seeders") or 0,
                "year": str(it.get("PublishDate") or "")[:4],
                "magnet": link if link.startswith("magnet:") else "",
                "url": it.get("Details") or (link if not link.startswith("magnet:") else "")}
        if it.get("InfoHash") and (hexhash := _try_hex_hash(it["InfoHash"])):
            entry["hash"] = hexhash
        out.append(entry)
    return out


# ---------------------------------------------------------------- books, comics

LIBGEN_TOPICS = {"books": "&topics[]=l&topics[]=f", "comics": "&topics[]=c", "papers": "&topics[]=a",
                 "magazines": "&topics[]=m"}


async def libgen(query: str, limit: int, category: str = "") -> list[dict]:
    """Library Genesis: books, fiction, comics, magazines and papers. Results carry the md5
    that Anna's Archive, Z-Library and every LibGen mirror share."""
    topics = LIBGEN_TOPICS.get(category, "")

    async def attempt(base):
        r = await http(f"{base}/index.php?req={quote(query)}&res=50{topics}")
        if 'id="tablelibgen"' not in r.text:
            raise RuntimeError("not a LibGen results page")
        return base, r.text
    base, page = await mirrors.call("libgen", ["https://libgen.li", "https://libgen.bz", "https://libgen.vg"],
                                    {"slum": "libgen"}, attempt)
    body = page[page.find("<tbody"):]
    out = []
    for row in body.split("<tr")[1:]:
        cells = re.findall(r"(?s)<td[^>]*>(.*?)</td>", row)
        md5 = re.search(r"md5=([0-9a-f]{32})", row)
        if len(cells) < 9 or not md5:
            continue
        titles = [_text(t) for t in re.findall(r'(?s)href="edition\.php\?id=\d+">(.*?)</a>', cells[0])]
        series = re.search(r'(?s)href="series\.php\?id=\d+">(.*?)</a>', cells[0])
        title = titles[0] if titles else ""
        if series and (not title or title.startswith("#")):
            title = f"{_text(series.group(1))} {title}".strip()
        kind = re.search(r'title="([^"]+)">\w</a></span>', cells[0])
        out.append({"source": "libgen", "title": title or "(untitled)", "size": _text(cells[6]),
                    "seeders": 0, "year": _text(cells[3])[:4],
                    "info": " · ".join(x for x in (_text(cells[1])[:60], _text(cells[4]), _text(cells[7]),
                                                   kind.group(1) if kind else "") if x),
                    "md5": md5.group(1), "url": f"{base}/ads.php?md5={md5.group(1)}"})
        if len(out) >= limit:
            break
    return out


async def annas_archive(query: str, limit: int, category: str = "") -> list[dict]:
    """Anna's Archive: the largest shadow-library index (LibGen, Z-Library, Sci-Hub, IA).
    It sits behind DDoS-Guard, so this goes through the browser stage; interactive=False so a
    search never blocks on a visible window. Run fetch_page on an annas-archive URL once to
    solve the check yourself and save the cookies this then reuses."""
    content = {"books": "&content=book_nonfiction&content=book_fiction&content=book_unknown",
               "comics": "&content=book_comic", "papers": "&content=journal_article",
               "magazines": "&content=magazine"}.get(category, "")

    async def attempt(base):
        page = await fetch.fetch(f"{base}/search?q={quote(query)}{content}", 20, interactive=False)
        text = page.body.decode("utf-8", "replace")
        if "js-vim-focus" not in text:
            raise RuntimeError("not an Anna's Archive results page")
        return base, text
    base, page = await mirrors.call("annas-archive", ["https://annas-archive.gl", "https://annas-archive.pk",
                                                      "https://annas-archive.gd"],
                                    {"slum": "annas-archive", "annas_info": "annas-archive"}, attempt)
    starts = [m.start() for m in re.finditer(r'<a href="/md5/[0-9a-f]{32}" class="line-clamp-\[3\]', page)]
    out = []
    for i, start in enumerate(starts[:limit]):
        card = page[start:starts[i + 1] if i + 1 < len(starts) else start + 20000]
        md5 = re.search(r"/md5/([0-9a-f]{32})", card).group(1)
        title = _text(re.search(r"(?s)>(.*?)</a>", card).group(1))
        author = re.search(r'(?s)icon-\[mdi--user-edit\][^>]*></span>(.*?)</a>', card)
        facts = re.search(r"[^<>]*\[[a-z]{2,3}\] · [^<>]*", card)
        facts = htmllib.unescape(facts.group(0)).strip() if facts else ""
        size = re.search(r"· ([\d.]+[KMG]B) ·", facts)
        year = re.search(r"· ((?:1[5-9]|20)\d\d) ·", facts)
        out.append({"source": "annas-archive", "title": title, "size": size.group(1) if size else "",
                    "seeders": 0, "year": year.group(1) if year else "",
                    "info": " · ".join(x for x in (_text(author.group(1)) if author else "", facts) if x),
                    "md5": md5, "url": f"{base}/md5/{md5}"})
    return out


async def getcomics(query: str, limit: int, category: str = "") -> list[dict]:
    """GetComics: western comics (Marvel, DC, indie) as direct downloads. Its own search box
    is Google Custom Search, so this searches the site through the local SearXNG instead."""
    hits = await providers.search_searxng(f"{query} site:getcomics.org", limit * 4, os.environ, 15)
    return [{"source": "getcomics", "title": h["title"].replace(" – GetComics", "").replace(" - GetComics", ""),
             "size": "", "seeders": 0, "year": "", "url": h["url"]}
            for h in hits if urlparse(h["url"]).hostname == "getcomics.org"][:limit]


ZLIB_SEEDS = ["https://z-library.ec"]


async def zlibrary(query: str, limit: int, category: str = "") -> list[dict]:
    """Z-Library: large ebook library. Searching needs no account; downloading (book_download)
    uses the free account in ZLIB_EMAIL / ZLIB_PASSWORD."""
    async def attempt(base):
        data = (await http(f"{base}/eapi/book/search", form={"message": query, "limit": limit})).json()
        if not data.get("success"):
            raise RuntimeError(data.get("error") or "search refused")
        return data["books"]
    books = await mirrors.call("zlibrary", ZLIB_SEEDS, {"slum": "z-lib"}, attempt)
    return [{"source": "zlibrary", "title": b["title"], "size": b.get("filesizeString", ""), "seeders": 0,
             "year": str(b.get("year") or ""),
             "info": " · ".join(str(x) for x in (b.get("author"), b.get("language"), b.get("extension"),
                                                 b.get("publisher")) if x),
             "md5": b.get("md5"), "url": b.get("href", "")}
            for b in books if b.get("md5")]


async def openlibrary(query: str, limit: int, category: str = "") -> list[dict]:
    """Open Library: book records, with a link to read or borrow a scan on the Internet Archive."""
    r = await http(f"https://openlibrary.org/search.json?q={quote(query)}&limit={limit}"
                   "&fields=title,key,author_name,first_publish_year,ia,ebook_access")
    out = []
    for d in r.json().get("docs", []):
        access = d.get("ebook_access", "no_ebook")
        out.append({"source": "openlibrary", "title": d["title"], "size": "", "seeders": 0,
                    "year": str(d.get("first_publish_year") or ""),
                    "info": " · ".join(x for x in (", ".join(d.get("author_name", [])[:2]), access.replace("_", " ")) if x),
                    "url": f"https://archive.org/details/{d['ia'][0]}" if d.get("ia") and access != "no_ebook"
                    else f"https://openlibrary.org{d['key']}"})
    return out


async def gutenberg(query: str, limit: int, category: str = "") -> list[dict]:
    """Project Gutenberg's own OPDS catalog: free public-domain ebooks with direct EPUB links."""
    r = await http(f"https://www.gutenberg.org/ebooks/search.opds/?query={quote(query)}", timeout=20)
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", r.text, re.DOTALL):
        book = re.search(r"/ebooks/(\d+)\.opds", entry)
        if not book:
            continue  # the feed also lists "sort by" and author links
        out.append({"source": "gutenberg", "title": _rss_tag(entry, "title"), "size": "", "seeders": 0, "year": "",
                    "info": _rss_tag(re.sub(r"<content[^>]*>", "<content>", entry), "content"),
                    "url": f"https://www.gutenberg.org/ebooks/{book.group(1)}",
                    "download": f"https://www.gutenberg.org/ebooks/{book.group(1)}.epub3.images"})
    return out[:limit]


# ---------------------------------------------------------------- manga, drama, subtitles

async def weebcentral(query: str, limit: int, category: str = "") -> list[dict]:
    """WeebCentral: manga/manhwa reader with most series in English (Comick's successor)."""
    r = await http(f"https://weebcentral.com/search/data?text={quote(query)}&display_mode=Minimal%20Display"
                   f"&limit={limit}&sort=Best%20Match&order=Descending", headers={"HX-Request": "true"})
    out = []
    for card in r.text.split("<article")[1:limit + 1]:
        link = re.search(r'href="(https://weebcentral\.com/series/[^"]+)"', card)
        if not link:
            continue
        facts = [_text(x) for x in re.findall(r"(?s)<div>(.*?)</div>", card)]
        out.append({"source": "weebcentral", "title": _text(re.search(r"(?s)<h2[^>]*>(.*?)</h2>", card).group(1)),
                    "size": "", "seeders": 0, "year": next((f for f in facts if re.fullmatch(r"\d{4}", f)), ""),
                    "info": " · ".join(f for f in facts if not re.fullmatch(r"\d{4}", f)), "url": link.group(1)})
    return out


async def mangaupdates(query: str, limit: int, category: str = "") -> list[dict]:
    """MangaUpdates: the reference catalog for manga/manhwa/manhua: type, year, rating, status."""
    r = await http("https://api.mangaupdates.com/v1/series/search", {"search": query, "perpage": limit})
    out = []
    for hit in r.json().get("results", [])[:limit]:
        rec = hit["record"]
        out.append({"source": "mangaupdates", "title": _text(rec["title"]), "size": "", "seeders": 0,
                    "year": str(rec.get("year") or ""),
                    "info": " · ".join(str(x) for x in (rec.get("type"), rec.get("bayesian_rating") and
                                                        f"rating {rec['bayesian_rating']}") if x),
                    "url": rec.get("url", "")})
    return out


async def kuryana(query: str, limit: int, category: str = "") -> list[dict]:
    """MyDramaList (via the Kuryana API): Asian dramas: country, episodes, rating, rank."""
    r = await http(f"https://kuryana.tbdh.app/search/q/{quote(query)}", timeout=20)
    return [{"source": "mydramalist", "title": d["title"], "size": "", "seeders": 0, "year": str(d.get("year") or ""),
             "info": " · ".join(str(x) for x in (d.get("type"), d.get("series"), d.get("rating") and f"rating {d['rating']}",
                                                 d.get("ranking") and f"rank {d['ranking']}") if x),
             "url": f"https://mydramalist.com/{d['slug'].split('-', 1)[0]}"}
            for d in r.json().get("results", {}).get("dramas", [])[:limit]]


async def kisskh(query: str, limit: int, category: str = "") -> list[dict]:
    """Kisskh: streams most Asian dramas with English subtitles. Usually reached through Tor."""
    r = await http(f"https://kisskh.co/api/DramaList/Search?q={quote(query)}&type=0", timeout=20)
    return [{"source": "kisskh", "title": d["title"], "size": "", "seeders": 0, "year": "",
             "info": " · ".join(x for x in (f"{d.get('episodesCount')} episodes", d.get("label")) if x),
             "url": f"https://kisskh.co/Drama/{quote(d['title'].replace(' ', '-'))}?id={d['id']}"}
            for d in r.json()[:limit]]


async def opensubtitles(query: str, limit: int, category: str = "") -> list[dict]:
    """OpenSubtitles: English subtitles (.srt in a zip) for films and TV, K-drama included."""
    r = await http(f"https://www.opensubtitles.org/en/search/sublanguageid-eng/moviename-{quote(query)}/rss_2_00")
    out = []
    for item in r.text.split("<item>")[1:limit + 1]:
        zip_url = re.search(r'url="([^"]+)"[^>]*type="application/zip"|type="application/zip" url="([^"]+)"', item)
        released = re.search(r"Released as: ([^;]+);", item)
        out.append({"source": "opensubtitles", "title": _rss_tag(item, "title").removesuffix(" - subtitles"),
                    "size": "", "seeders": 0, "year": _rss_tag(item, "pubDate")[12:16],
                    "info": f"released as {released.group(1).strip()}" if released else "",
                    "url": _rss_tag(item, "link"), "download": (zip_url.group(1) or zip_url.group(2)) if zip_url else ""})
    return out


# ---------------------------------------------------------------- audio, software

ITUNES_MEDIA = {"audiobooks": "audiobook", "podcasts": "podcast", "music": "music"}


async def itunes(query: str, limit: int, category: str = "") -> list[dict]:
    """Apple's catalog: audiobooks (narrator, length), podcasts (with RSS feed URL), albums."""
    media_type = ITUNES_MEDIA.get(category, "all")
    entity = "&entity=album" if media_type == "music" else ""
    r = await http(f"https://itunes.apple.com/search?term={quote(query)}&media={media_type}{entity}&limit={limit}")
    out = []
    for x in r.json().get("results", []):
        out.append({"source": "itunes", "title": x.get("collectionName") or x.get("trackName", ""), "size": "",
                    "seeders": 0, "year": (x.get("releaseDate") or "")[:4],
                    "info": " · ".join(str(v) for v in (x.get("artistName"), x.get("primaryGenreName"),
                                                        x.get("trackCount") and f"{x['trackCount']} tracks") if v),
                    "url": x.get("feedUrl") or x.get("collectionViewUrl") or x.get("trackViewUrl", "")})
    return out


async def audiobookbay(query: str, limit: int, category: str = "") -> list[dict]:
    """AudioBookBay: audiobook torrents; the magnet is on each result's page."""
    async def attempt(base):
        r = await http(f"{base}/?s={quote(query)}")
        if 'class="post"' not in r.text and "Nothing was found" not in r.text:
            raise RuntimeError("not an AudioBookBay results page")
        return base, r.text
    base, page = await mirrors.call("audiobookbay", ["https://audiobookbay.lu", "https://audiobookbay.is"], {}, attempt)
    out = []
    for post in page.split('<div class="post">')[1:limit + 1]:
        link = re.search(r'(?s)class="postTitle"><h2><a href="([^"]+)"[^>]*>(.*?)</a>', post)
        if not link:
            continue
        posted = re.search(r"Posted: [^<]*?(\d{4})", post)
        fmt = re.search(r"Format: <span[^>]*>([^<]+)", post)
        size = re.search(r"File Size: <span[^>]*>([^<]+)</span>\s*(\w+)", post)
        language = re.search(r"Language: ([^<]+)", post)
        out.append({"source": "audiobookbay", "title": _text(link.group(2)), "seeders": 0,
                    "size": f"{size.group(1)} {size.group(2)}" if size else "", "year": posted.group(1) if posted else "",
                    "info": " · ".join(x.group(1).strip() for x in (fmt, language) if x),
                    "url": link.group(1) if link.group(1).startswith("http") else base + link.group(1)})
    return out


ARCHIVE_FILTERS = {"audiobooks": "mediatype:audio AND (subject:audiobook OR collection:librivoxaudio)",
                   "music": "mediatype:audio",
                   "software": "mediatype:software", "games": "mediatype:software", "books": "mediatype:texts"}


async def archive_org(query: str, limit: int, category: str = "") -> list[dict]:
    """Internet Archive: public-domain and preserved books, audio, live concerts and old
    software/DOS games, most popular first."""
    kind = ARCHIVE_FILTERS.get(category, "")
    q = f"title:({query})" + (f" AND {kind}" if kind else "")
    r = await http(f"https://archive.org/advancedsearch.php?q={quote(q)}&fl[]=identifier&fl[]=title&fl[]=year"
                   f"&fl[]=creator&fl[]=mediatype&fl[]=downloads&sort[]=downloads+desc&rows={limit}&output=json")
    out = []
    for d in r.json()["response"]["docs"]:
        creator = d.get("creator")
        out.append({"source": "archive.org", "title": str(d.get("title", d["identifier"])), "size": "", "seeders": 0,
                    "year": str(d.get("year") or ""),
                    "info": " · ".join(str(x) for x in (creator[0] if isinstance(creator, list) else creator,
                                                        d.get("mediatype"), f"{d.get('downloads', 0)} downloads") if x),
                    "url": f"https://archive.org/details/{d['identifier']}"})
    return out


# ---------------------------------------------------------------- catalogs

async def mangadex(query: str, limit: int, category: str = "") -> list[dict]:
    """MangaDex: manga, manhwa and manhua with free, legal chapter reading."""
    r = await http(f"https://api.mangadex.org/manga?title={quote(query)}&limit={limit}"
                   "&order[relevance]=desc&contentRating[]=safe&contentRating[]=suggestive")
    out = []
    for m in r.json().get("data", []):
        a = m["attributes"]
        title = a["title"].get("en") or next(iter(a["title"].values()), "")
        out.append({"source": "mangadex", "title": title, "size": "", "seeders": 0,
                    "year": str(a.get("year") or ""),
                    "info": f"{a.get('originalLanguage', '')} · {a.get('status', '')} · last ch. {a.get('lastChapter') or '?'}",
                    "url": f"https://mangadex.org/title/{m['id']}"})
    return out


ANILIST_QUERY = """query($q:String,$type:MediaType){Page(perPage:8){media(search:$q,type:$type,sort:SEARCH_MATCH){
  title{romaji english} format status startDate{year} episodes chapters averageScore siteUrl countryOfOrigin}}}"""


async def anilist(query: str, limit: int, category: str = "") -> list[dict]:
    """AniList: what an anime/manga/manhwa is called, how many episodes/chapters, status."""
    media_type = {"anime": "ANIME", "manga": "MANGA"}.get(category)
    r = await http("https://graphql.anilist.co", {"query": ANILIST_QUERY,
                                                   "variables": {"q": query, "type": media_type}})
    return [{"source": "anilist", "title": m["title"]["english"] or m["title"]["romaji"],
             "size": "", "seeders": 0, "year": str(m["startDate"]["year"] or ""),
             "info": " · ".join(str(x) for x in (m["title"]["romaji"], m["format"], m["countryOfOrigin"], m["status"],
                                                 f"{m['episodes']} eps" if m["episodes"] else "",
                                                 f"{m['chapters']} ch" if m["chapters"] else "",
                                                 f"score {m['averageScore']}" if m["averageScore"] else "") if x),
             "url": m["siteUrl"]}
            for m in r.json()["data"]["Page"]["media"][:limit]]


async def tvmaze(query: str, limit: int, category: str = "") -> list[dict]:
    """TVmaze: TV shows including K-dramas and other Asian dramas: network, status, where to watch."""
    r = await http(f"https://api.tvmaze.com/search/shows?q={quote(query)}")
    out = []
    for hit in r.json()[:limit]:
        s = hit["show"]
        network = (s.get("network") or s.get("webChannel") or {})
        out.append({"source": "tvmaze", "title": s["name"], "size": "", "seeders": 0,
                    "year": (s.get("premiered") or "")[:4],
                    "info": " · ".join(x for x in (s.get("language"), network.get("name"), s.get("status"),
                                                   ", ".join(s.get("genres", []))) if x),
                    "url": s.get("url", "")})
    return out


# ---------------------------------------------------------------- routing

# What a title is; everything else is where to get it.
async def imdb(query: str, limit: int, category: str = "") -> list[dict]:
    """movies.imdb, referenced at call time: movies imports media, so either can be imported first."""
    return await movies.imdb(query, limit, category)


CATALOGS = {"anilist", "mangadex", "mangaupdates", "tvmaze", "kuryana", "itunes", "imdb"}
SOURCES = {
    "books": [libgen, annas_archive, zlibrary, openlibrary, gutenberg, knaben],
    "comics": [libgen, getcomics, annas_archive, zlibrary],
    "manga": [anilist, mangaupdates, mangadex, weebcentral, nyaa, libgen],
    "anime": [anilist, subsplease, animetosho, nyaa, knaben],
    "movies": [imdb, yts, knaben, piratebay, torrents_csv, limetorrents, x1337, torrentio, prowlarr, jackett],
    "tv": [imdb, tvmaze, kuryana, eztv, kisskh, knaben, piratebay, torrents_csv, limetorrents, x1337, torrentio,
          prowlarr, jackett],
    "subtitles": [opensubtitles],
    "audiobooks": [itunes, archive_org, audiobookbay],
    "music": [itunes, archive_org, knaben, limetorrents],
    "podcasts": [itunes],
    "games": [fitgirl, archive_org],
    "software": [archive_org],
    "torrents": [knaben, piratebay, torrents_csv, nyaa, limetorrents, x1337, prowlarr, jackett],
}
SOURCES["all"] = list(dict.fromkeys(f for fns in SOURCES.values() for f in fns))


SEARCH_DEADLINE = 30  # seconds; a still-running source is left running for the cache, not cancelled
_pending: set[asyncio.Task] = set()  # keeps background tasks alive past search()'s return


async def _cached(fn, query: str, limit: int, category: str):
    key = (fn.__name__, query.lower(), limit, category)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        health.record(fn.__name__, True)
        return hit[1]
    try:
        results = await asyncio.wait_for(fn(query, limit, category), timeout=150)
    except Exception:
        health.record(fn.__name__, False)
        raise
    health.record(fn.__name__, True)
    for old in [k for k, (at, _) in _cache.items() if time.time() - at > CACHE_TTL]:
        del _cache[old]
    _cache[key] = (time.time(), results)
    return results


async def _fan_out(fns: list, query: str, limit: int, category: str) -> tuple[list[tuple[bool, list]], list[str]]:
    """Run every source in fns for one query, waiting up to SEARCH_DEADLINE. A source still
    running after that keeps running in the background (its result lands in the cache for the
    next call) rather than being cancelled. A source failing twice in a row is skipped for a
    while (health.py) instead of being retried every call.
    Returns each source's (is_catalog, results) plus a status note per source."""
    notes, tasks = [], {}
    for f in fns:
        wait = health.skipped(f.__name__)
        if wait:
            notes.append(f"{f.__name__}: skipped, failing (retry in {int(wait // 60) + 1} min)")
            continue
        task = asyncio.ensure_future(_cached(f, query, limit, category))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
        tasks[task] = f
    if tasks:
        _, still_running = await asyncio.wait(tasks, timeout=SEARCH_DEADLINE)
    else:
        still_running = set()
    lists = []
    for task, fn in tasks.items():
        if task in still_running:
            notes.append(f"{fn.__name__}: still loading, try again shortly")
            continue
        try:
            res = task.result()
        except Exception as e:  # noqa: BLE001 - one source's failure doesn't stop the others
            notes.append(f"{fn.__name__}: failed ({type(e).__name__}: {e})"[:200])
            continue
        notes.append(f"{fn.__name__}: {len(res)}")
        lists.append((fn.__name__ in CATALOGS, res))
    return lists, notes


def _merge(lists: list[tuple[bool, list]], runtime: int | None, seen: set | None = None
          ) -> tuple[list[dict], list[dict], set]:
    """Take each source's best, then each one's second best, ... so every source is represented.
    seen carries over across retry rounds so a re-run with a different query doesn't re-add
    something the first round already found."""
    catalog, found, seen = [], [], set() if seen is None else seen
    for rank in range(max((len(res) for _, res in lists), default=0)):
        for is_catalog, res in lists:
            if rank >= len(res):
                continue
            r = res[rank]
            key = r.get("hash") or r.get("md5") or r["url"] or r["title"]
            if key not in seen:
                seen.add(key)
                if not is_catalog and r.get("magnet"):
                    q = quality.classify(r.get("title", ""), _size_bytes(r.get("size", "")), runtime)
                    r["tier"], r["resolution"], r["is_cam"] = q["tier"], q["resolution"], q["is_cam"]
                    r["quality_label"] = q["label"]
                    r["warnings"] = r.get("warnings", []) + q["warnings"]
                (catalog if is_catalog else found).append(r)
    return catalog, found, seen


YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


async def _retry_queries(query: str, category: str) -> list[tuple[str, str]]:
    """Retry candidates for a zero-result movies/tv search, tried in order until one hits:
    the query with its year dropped, then a cheap alternate title (movies.alt_titles)."""
    out = []
    stripped = YEAR_RE.sub("", query).strip()
    if stripped and stripped != query:
        out.append(("year dropped", stripped))
    for alt in await movies.alt_titles(query, category):
        if alt.lower() not in (query.lower(), stripped.lower()):
            out.append(("alt title", alt))
    return out[:3]


def dedupe_similar(items: list[dict], threshold: int = 90) -> list[dict]:
    """Collapse near-duplicate torrent results that _merge's exact hash/md5/url/title key
    missed (same release re-hashed and re-uploaded under a slightly different name). Only
    considers items with an infohash, bucketed by (year, season/episode, resolution) so
    different episodes, seasons or resolutions never merge; within a bucket, titles normalized
    and compared with rapidfuzz token_sort_ratio. Keeps the best-seeded copy and notes how many
    sources carried it."""
    other = [r for r in items if not r.get("hash")]
    buckets: dict[tuple, list[dict]] = {}
    for r in items:
        if not r.get("hash"):
            continue
        ep = re.search(r"\bs\d{1,2}e\d{1,3}\b", r.get("title", ""), re.IGNORECASE)
        buckets.setdefault((r.get("year", ""), ep.group(0).lower() if ep else "", r.get("resolution", "")),
                          []).append(r)
    kept = []
    for group in buckets.values():
        norm = [re.sub(r"\s+", " ", re.sub(r"[.\-_]+", " ", r["title"].lower())).strip() for r in group]
        used = [False] * len(group)
        for i, r in enumerate(group):
            if used[i]:
                continue
            dupes = [r]
            used[i] = True
            for j in range(i + 1, len(group)):
                if not used[j] and fuzz.token_sort_ratio(norm[i], norm[j]) >= threshold:
                    dupes.append(group[j])
                    used[j] = True
            best = max(dupes, key=lambda x: x.get("seeders", 0))
            if len(dupes) > 1:
                others = sorted({d["source"] for d in dupes} - {best["source"]})
                if others:
                    best["info"] = " · ".join(x for x in (best.get("info", ""), f"also on {', '.join(others)}") if x)
            kept.append(best)
    return other + kept


async def search(query: str, category: str = "all", limit: int = 10,
                 only: list[str] | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """Fan out to every source for the category in parallel. If a movies/tv search comes back
    with nothing downloadable, retry with the year dropped and then an alternate title before
    giving up. Returns (catalog entries, downloadable results deduped, per-source notes)."""
    fns = [f for f in SOURCES.get(category, SOURCES["all"]) if not only or f.__name__ in only]
    lists, notes = await _fan_out(fns, query, limit, category)
    runtime = await movies.runtime_min(query, category) if category in ("movies", "tv") else None
    catalog, found, seen = _merge(lists, runtime)

    if category in ("movies", "tv") and not found:
        retry_fns = [f for f in fns if f.__name__ not in CATALOGS]
        for why, retry_query in await _retry_queries(query, category):
            retry_lists, _ = await _fan_out(retry_fns, retry_query, limit, category)
            _, retry_found, seen = _merge(retry_lists, runtime, seen)
            notes.append(f"retried as {why} ({retry_query!r}): {len(retry_found)} results")
            if retry_found:
                found = retry_found
                break

    return catalog, dedupe_similar(found), notes


def format_results(catalog: list[dict], found: list[dict], notes: list[str], limit: int) -> str:
    def line(r):
        head = f"[{r['source']}] {r['title']}"
        meta = " | ".join(x for x in (r.get("year"), r.get("size"), r.get("quality_label"),
                                      f"{r['seeders']} seeders" if r.get("seeders") else "", r.get("info")) if x)
        out = f"{head}\n  {meta}" if meta else head
        for k in ("url", "magnet"):
            if r.get(k):
                out += f"\n  {r[k]}"
        if r.get("download"):
            out += f"\n  download: {r['download']}"
        if r.get("md5"):
            out += f"\n  md5: {r['md5']} (book_download)"
        for w in r.get("warnings") or []:
            out += f"\n  ⚠ {w}"
        return out

    def quality_sort(r):
        # Flagged (fake/suspicious) results sink to the bottom regardless of quality; among the
        # rest, best tier/resolution/seeders first.
        res = (r.get("resolution") or "").rstrip("p")
        return (bool(r.get("warnings")), -r.get("tier", 3), -int(res) if res.isdigit() else 0, -r.get("seeders", 0))

    torrents = [r for r in found if "tier" in r]
    other = [r for r in found if "tier" not in r]
    where = sorted((r for r in torrents if not r["is_cam"]), key=quality_sort) + other
    cinema = sorted((r for r in torrents if r["is_cam"]), key=quality_sort)

    parts = [f"sources: {', '.join(notes)}"]
    if catalog:
        parts.append("WHAT IT IS:\n" + "\n".join(line(r) for r in catalog[:limit]))
    if where:
        parts.append("WHERE TO GET IT:\n" + "\n".join(line(r) for r in where[:limit * 3]))
    if cinema:
        parts.append("Cinema recordings (low quality, filmed in a theatre):\n"
                      + "\n".join(line(r) for r in cinema[:limit]))
    return "\n\n".join(parts)


async def book_download(md5: str, save_dir: str) -> str:
    """Download a book/comic/paper by md5: LibGen first, then Z-Library (free account)."""
    try:
        r = await _libgen_file(md5)
    except Exception as libgen_error:
        if not os.environ.get("ZLIB_EMAIL"):
            raise RuntimeError(f"LibGen: {libgen_error}. Set ZLIB_EMAIL/ZLIB_PASSWORD in .env "
                               "to also try Z-Library") from libgen_error
        try:
            r = await _zlibrary_file(md5)
        except Exception as zlib_error:
            raise RuntimeError(f"LibGen: {libgen_error}; Z-Library: {zlib_error}") from zlib_error
    return save_download(r, md5, save_dir)


ZLIB_LOGIN = STATE / "zlibrary-login.json"


async def _zlibrary_file(md5: str):
    """Find the book by md5, then ask for its file link with the account's cookies. The login
    is cached, because Z-Library rate-limits logins."""
    async def attempt(base):
        login = load_json(ZLIB_LOGIN)
        if not login:
            data = (await http(f"{base}/eapi/user/login", form={
                "email": os.environ["ZLIB_EMAIL"], "password": os.environ.get("ZLIB_PASSWORD", "")})).json()
            if not data.get("success"):
                raise RuntimeError(f"login failed: {data.get('error') or data}"[:160])
            login = {"remix_userid": str(data["user"]["id"]), "remix_userkey": data["user"]["remix_userkey"]}
            save_json(ZLIB_LOGIN, login, private=True)
        found = (await http(f"{base}/eapi/book/search", form={"message": md5, "limit": 5})).json()
        book = next((b for b in found.get("books", []) if b.get("md5") == md5), None)
        if not book:
            raise RuntimeError("md5 not on Z-Library")
        link = (await http(f"{base}/eapi/book/{book['id']}/{book['hash']}/file", cookies=login)).json()
        url = (link.get("file") or {}).get("downloadLink")
        if not url:
            if "auth" in str(link).lower():
                ZLIB_LOGIN.unlink(missing_ok=True)  # stale login: log in again next time
            raise RuntimeError(f"no download link: {link.get('error') or link}"[:160])
        return await http(url, cookies=login, timeout=300)
    return await mirrors.call("zlibrary", ZLIB_SEEDS, {"slum": "z-lib"}, attempt)


async def _libgen_file(md5: str):
    async def attempt(base):
        page = await http(f"{base}/ads.php?md5={md5}")
        link = re.search(r'href="(get\.php\?md5=[0-9a-f]{32}&(?:amp;)?key=\w+)"', page.text)
        if not link:
            raise RuntimeError("no download link on the page")
        r = await http(f"{base}/{htmllib.unescape(link.group(1))}", timeout=300)
        return r
    return await mirrors.call("libgen", ["https://libgen.li", "https://libgen.bz", "https://libgen.vg"],
                              {"slum": "libgen"}, attempt)


MAX_DOWNLOAD_BYTES = 1024**3  # 1 GB; book/comic/paper files are never legitimately larger


def save_download(r, md5: str, save_dir: str) -> str:
    """Save a download under its Content-Disposition name, never outside save_dir and never
    over an existing file. Rejects an HTML response (a login/error page, not the file), a size
    over MAX_DOWNLOAD_BYTES, and content whose md5 doesn't match the one that was requested."""
    if "text/html" in r.headers.get("content-type", "").lower():
        raise RuntimeError("got an HTML page instead of the file (login wall or dead link)")
    if len(r.content) > MAX_DOWNLOAD_BYTES:
        raise RuntimeError(f"{len(r.content):,} bytes exceeds the {MAX_DOWNLOAD_BYTES:,} byte cap")
    digest = hashlib.md5(r.content).hexdigest()
    if digest != md5:
        raise RuntimeError(f"md5 mismatch: expected {md5}, got {digest}")
    header = Message()
    header["content-disposition"] = r.headers.get("content-disposition", "")
    name = Path(header.get_filename() or "").name  # get_filename decodes filename*=UTF-8''...
    if name.strip(". ") == "":
        name = f"{md5}.{(r.headers.get('content-type', '').split('/')[-1].split(';')[0] or 'bin')}"
    folder = Path(save_dir).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    path, n = folder / name, 1
    while path.exists():
        path, n = folder / f"{Path(name).stem} ({n}){Path(name).suffix}", n + 1
    path.write_bytes(r.content)
    return f"saved {len(r.content):,} bytes to {path}"
