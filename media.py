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
import html as htmllib
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from curl_cffi import AsyncSession

import fetch
import mirrors
import providers

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


async def http(url: str, json_body: dict | None = None, timeout: int = 12):
    """GET (or POST json_body) with a Chrome fingerprint. Falls back to Tor when the
    connection itself fails, which is what an ISP block looks like."""
    host = urlparse(url).hostname or ""
    await _polite(host)
    routes = [TOR] if host in _via_tor else [None, TOR]
    error = None
    for proxy in routes:
        try:
            async with AsyncSession() as s:
                r = await s.request("POST" if json_body else "GET", url, json=json_body,
                                    impersonate="chrome", proxy=proxy,
                                    timeout=timeout * 2 if proxy else timeout)
        except Exception as e:  # noqa: BLE001 - connection failure: try the next route
            error = e
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


def _hex_hash(infohash: str) -> str:
    """Infohashes come as 40 hex chars or 32 base32 chars; use hex so duplicates match."""
    if len(infohash) == 32:
        return base64.b32decode(infohash.upper()).hex()
    return infohash.lower()


def _magnet(infohash: str, name: str) -> str:
    return f"magnet:?xt=urn:btih:{_hex_hash(infohash)}&dn={quote(name)}"


# ---------------------------------------------------------------- torrents

async def knaben(query: str, limit: int, category: str = "") -> list[dict]:
    """Knaben: a meta-index over The Pirate Bay, 1337x, Nyaa, RuTracker and others."""
    r = await http("https://api.knaben.org/v1", {
        "query": query, "size": limit * 3, "order_by": "seeders", "order_direction": "desc",
        "hide_unsafe": True, "hide_xxx": True})
    hits = r.json()["hits"]
    prefix = {"movies": "Movies", "tv": "TV", "anime": "Anime", "games": "PC Games",
              "books": "Books", "comics": "Books", "manga": "Anime"}.get(category)
    return [{"source": f"knaben/{h.get('cachedOrigin') or '?'}", "title": h["title"],
             "size": _size(h.get("bytes")), "seeders": h.get("seeders") or 0,
             "year": (h.get("date") or "")[:4], "info": h.get("category") or "",
             "magnet": _magnet(h["hash"], h["title"]), "hash": h["hash"].lower(),
             "url": h.get("details") or ""}
            for h in hits if h.get("hash") and (not prefix or (h.get("category") or "").startswith(prefix))][:limit]


APIBAY_GROUPS = {"movies": {201, 202, 207, 209}, "tv": {205, 208}, "games": {400, 401, 404, 408},
                 "books": {601}, "comics": {602}, "anime": {201, 205, 207, 208}}


async def piratebay(query: str, limit: int, category: str = "") -> list[dict]:
    r = await http(f"https://apibay.org/q.php?q={quote(query)}")
    wanted = APIBAY_GROUPS.get(category)
    out = []
    for it in r.json():
        if it.get("id") == "0" or not it.get("info_hash"):
            continue  # apibay's "no results" row
        if wanted and int(it.get("category", 0)) not in wanted:
            continue
        out.append({"source": "piratebay", "title": it["name"], "size": _size(it.get("size")),
                    "seeders": int(it.get("seeders", 0)), "year": time.strftime("%Y", time.gmtime(int(it.get("added", 0)))),
                    "info": f"cat {it.get('category')}", "magnet": _magnet(it["info_hash"], it["name"]),
                    "hash": it["info_hash"].lower(), "url": f"https://thepiratebay.org/description.php?id={it['id']}"})
    return sorted(out, key=lambda x: -x["seeders"])[:limit]


async def torrents_csv(query: str, limit: int, category: str = "") -> list[dict]:
    """Torrents-CSV: an open index built by crawling the DHT, so it has no domain to seize."""
    r = await http(f"https://torrents-csv.com/service/search?q={quote(query)}&size={limit}")
    return [{"source": "torrents-csv", "title": t["name"], "size": _size(t.get("size_bytes")),
             "seeders": t.get("seeders", 0), "year": time.strftime("%Y", time.gmtime(t.get("created_unix", 0))),
             "magnet": _magnet(t["infohash"], t["name"]), "hash": t["infohash"].lower(), "url": ""}
            for t in r.json().get("torrents", [])]


async def yts(query: str, limit: int, category: str = "") -> list[dict]:
    """YTS movies: small, good-quality encodes. Its domain changes often, so mirrors apply."""
    async def attempt(base):
        r = await http(f"{base}/api/v2/list_movies.json?query_term={quote(query)}&limit={limit}")
        return r.json()["data"].get("movies") or []
    movies = await mirrors.call("yts", ["https://movies-api.accel.li", "https://yts.gg"],
                                {"prowlarr": "yts"}, attempt)
    out = []
    for m in movies:
        for t in m.get("torrents", []):
            name = f"{m['title_long']} [{t['quality']} {t.get('type', '')}]".strip()
            out.append({"source": "yts", "title": name, "size": _size(t.get("size_bytes")),
                        "seeders": t.get("seeds", 0), "year": str(m.get("year", "")),
                        "info": f"IMDb {m.get('imdb_code', '')} rating {m.get('rating', '')}",
                        "magnet": _magnet(t["hash"], name), "hash": t["hash"].lower(), "url": m.get("url", "")})
    return out


def _rss_tag(item: str, name: str) -> str:
    m = re.search(rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", item, re.DOTALL)
    return htmllib.unescape(m.group(1).strip()) if m else ""


async def nyaa(query: str, limit: int, category: str = "") -> list[dict]:
    """Nyaa: the main anime torrent tracker (also manga scans, J-drama, live action)."""
    cat = {"anime": "1_0", "manga": "3_0"}.get(category, "0_0")

    async def attempt(base):
        r = await http(f"{base}/?page=rss&q={quote(query)}&c={cat}&f=0")
        if "<rss" not in r.text:
            raise RuntimeError("not a Nyaa RSS feed")
        return r.text
    xml = await mirrors.call("nyaa", ["https://nyaa.si"], {"prowlarr": "nyaasi"}, attempt)
    out = []
    for item in xml.split("<item>")[1:limit + 1]:
        infohash, title = _rss_tag(item, "nyaa:infoHash"), _rss_tag(item, "title")
        out.append({"source": "nyaa", "title": title, "size": _rss_tag(item, "nyaa:size"),
                    "seeders": int(_rss_tag(item, "nyaa:seeders") or 0),
                    "year": _rss_tag(item, "pubDate")[12:16], "info": _rss_tag(item, "nyaa:category"),
                    "magnet": _magnet(infohash, title), "hash": infohash.lower(), "url": _rss_tag(item, "guid")})
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
        out.append({"source": "subsplease", "title": title, "size": "", "seeders": 0,
                    "year": (entry.get("release_date") or "")[-4:], "magnet": _magnet(infohash, title),
                    "hash": _hex_hash(infohash), "url": f"https://subsplease.org/shows/{entry.get('page', '')}"})
    return out[:limit]


async def animetosho(query: str, limit: int, category: str = "") -> list[dict]:
    """AnimeTosho: mirrors Nyaa, AniDex and nekoBT, with direct-download links too."""
    r = await http(f"https://animetosho.org/feed/json?q={quote(query)}")
    return [{"source": "animetosho", "title": t["title"], "size": _size(t.get("total_size")),
             "seeders": t.get("seeders") or 0, "year": time.strftime("%Y", time.gmtime(t.get("timestamp", 0))),
             "magnet": _magnet(t["info_hash"], t["title"]), "hash": _hex_hash(t["info_hash"]),
             "url": t.get("link", "")}
            for t in r.json()[:limit] if t.get("info_hash")]


async def fitgirl(query: str, limit: int, category: str = "") -> list[dict]:
    """FitGirl repacks: games come only from here because games are the one category that
    runs code on your machine. fitgirl-repacks.site is the only official domain."""
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
    It sits behind DDoS-Guard, so this goes through the browser stage; the first time, a
    window opens for you to tick the check, and the saved cookies cover later searches."""
    content = {"books": "&content=book_nonfiction&content=book_fiction&content=book_unknown",
               "comics": "&content=book_comic", "papers": "&content=journal_article",
               "magazines": "&content=magazine"}.get(category, "")

    async def attempt(base):
        page = await fetch.fetch(f"{base}/search?q={quote(query)}{content}", 20)
        text = page.body.decode("utf-8", "replace")
        if "js-vim-focus" not in text:
            raise RuntimeError("not an Anna's Archive results page")
        return base, text
    base, page = await mirrors.call("annas-archive", ["https://annas-archive.gl", "https://annas-archive.pk",
                                                      "https://annas-archive.gd"], {"slum": "annas-archive"}, attempt)
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

CATALOGS = {"anilist", "mangadex", "tvmaze"}  # what a title is; everything else is where to get it
SOURCES = {
    "books": [libgen, annas_archive, knaben],
    "comics": [libgen, getcomics, annas_archive],
    "manga": [anilist, mangadex, nyaa, libgen],
    "anime": [anilist, subsplease, animetosho, nyaa, knaben],
    "movies": [yts, knaben, piratebay, torrents_csv],
    "tv": [tvmaze, knaben, piratebay, torrents_csv],
    "games": [fitgirl],
    "torrents": [knaben, piratebay, torrents_csv, nyaa],
}
SOURCES["all"] = list(dict.fromkeys(f for fns in SOURCES.values() for f in fns))


async def _cached(fn, query: str, limit: int, category: str):
    key = (fn.__name__, query.lower(), limit, category)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    results = await asyncio.wait_for(fn(query, limit, category), timeout=150)
    _cache[key] = (time.time(), results)
    return results


async def search(query: str, category: str = "all", limit: int = 10,
                 only: list[str] | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """Fan out to every source for the category in parallel.
    Returns (catalog entries, downloadable results deduped by infohash/md5, per-source notes)."""
    fns = [f for f in SOURCES.get(category, SOURCES["all"]) if not only or f.__name__ in only]
    runs = await asyncio.gather(*(_cached(f, query, limit, category) for f in fns), return_exceptions=True)
    lists, notes = [], []
    for fn, res in zip(fns, runs):
        if isinstance(res, Exception):
            notes.append(f"{fn.__name__}: failed ({type(res).__name__}: {res})"[:200])
            continue
        notes.append(f"{fn.__name__}: {len(res)}")
        lists.append((fn.__name__ in CATALOGS, res))
    # Take each source's best, then each one's second best, ... so every source is represented.
    catalog, found, seen = [], [], set()
    for rank in range(max((len(res) for _, res in lists), default=0)):
        for is_catalog, res in lists:
            if rank >= len(res):
                continue
            r = res[rank]
            key = r.get("hash") or r.get("md5") or r["url"] or r["title"]
            if key not in seen:
                seen.add(key)
                (catalog if is_catalog else found).append(r)
    return catalog, found, notes


def format_results(catalog: list[dict], found: list[dict], notes: list[str], limit: int) -> str:
    def line(r):
        head = f"[{r['source']}] {r['title']}"
        meta = " | ".join(x for x in (r.get("year"), r.get("size"),
                                      f"{r['seeders']} seeders" if r.get("seeders") else "", r.get("info")) if x)
        out = f"{head}\n  {meta}" if meta else head
        for k in ("url", "magnet"):
            if r.get(k):
                out += f"\n  {r[k]}"
        if r.get("md5"):
            out += f"\n  md5: {r['md5']} (book_download)"
        return out
    parts = [f"sources: {', '.join(notes)}"]
    if catalog:
        parts.append("WHAT IT IS:\n" + "\n".join(line(r) for r in catalog[:limit]))
    if found:
        parts.append("WHERE TO GET IT:\n" + "\n".join(line(r) for r in found[:limit * 3]))
    return "\n\n".join(parts)


async def book_download(md5: str, save_dir: str) -> str:
    """Download a book/comic/paper by md5 through LibGen's download page."""
    async def attempt(base):
        page = await http(f"{base}/ads.php?md5={md5}")
        link = re.search(r'href="(get\.php\?md5=[0-9a-f]{32}&(?:amp;)?key=\w+)"', page.text)
        if not link:
            raise RuntimeError("no download link on the page")
        r = await http(f"{base}/{htmllib.unescape(link.group(1))}", timeout=300)
        return r
    r = await mirrors.call("libgen", ["https://libgen.li", "https://libgen.bz", "https://libgen.vg"],
                           {"slum": "libgen"}, attempt)
    name = re.search(r'filename="?([^";]+)', r.headers.get("content-disposition", ""))
    filename = name.group(1) if name else f"{md5}.{(r.headers.get('content-type', '').split('/')[-1] or 'bin')}"
    path = Path(save_dir).expanduser() / re.sub(r"[/\\]", "_", filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(r.content)
    return f"saved {len(r.content):,} bytes to {path}"
