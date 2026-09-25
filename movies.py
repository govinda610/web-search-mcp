"""Movie/TV catalog info: what a title is, its runtime, and where it's legally streaming.

IMDb's own suggestion API resolves a query to an id (keyless); Cinemeta (Stremio's public
metadata service) gives the runtime from that id; JustWatch's unofficial GraphQL API gives
streaming/rental/cinema availability. Runtime and id are cached so torrentio (media.py) and
quality.classify's size check can reuse them without a second lookup.
"""
import os
import re
import time
from urllib.parse import quote

import media

COUNTRY = os.environ.get("WATCH_COUNTRY", "IN")
QID_FOR_CATEGORY = {"movies": "movie", "tv": "tvSeries"}
JW_TYPE = {"movies": "MOVIE", "tv": "SHOW"}
MONETIZATION_LABEL = {"FLATRATE": "flatrate", "RENT": "rent", "BUY": "buy", "FREE": "free", "ADS": "free with ads"}
JW_QUERY = """query GetSearchTitles($country: Country!, $language: Language!, $filter: TitleFilter) {
  popularTitles(country: $country, filter: $filter, first: 5) { edges { node {
    objectType content(country: $country, language: $language) { title }
    offers(country: $country, platform: WEB) { monetizationType package { clearName } } } } }
}"""

_id_cache: dict[tuple[str, str], tuple[float, str | None]] = {}
_runtime_cache: dict[tuple[str, str], tuple[float, int | None]] = {}


async def _suggest(query: str) -> list[dict]:
    r = await media.http(f"https://v3.sg.media-imdb.com/suggestion/x/{quote(query)}.json")
    return r.json().get("d") or []


async def _cinemeta(tt: str, category: str) -> dict:
    kind = "series" if category == "tv" else "movie"
    r = await media.http(f"https://v3-cinemeta.strem.io/meta/{kind}/{tt}.json", timeout=15)
    return r.json().get("meta") or {}


async def _watch_status(title: str, category: str) -> str | None:
    """None on any failure or no data: JustWatch is unofficial and best left silent when it's down."""
    try:
        r = await media.http("https://apis.justwatch.com/graphql", {
            "operationName": "GetSearchTitles", "query": JW_QUERY,
            "variables": {"country": COUNTRY, "language": "en", "filter": {"searchQuery": title}}})
        edges = r.json()["data"]["popularTitles"]["edges"]
    except Exception:  # noqa: BLE001 - degrade silently, per spec
        return None
    wanted = JW_TYPE.get(category)
    node = next((e["node"] for e in edges if e["node"]["objectType"] == wanted), None) or \
        (edges[0]["node"] if edges else None)
    offers = (node or {}).get("offers") or []
    if not offers:
        return None
    digital = [o for o in offers if o["monetizationType"] != "CINEMA"]
    if not digital:
        return f"in cinemas; not streaming in {COUNTRY} yet"
    seen, parts = set(), []
    for o in digital:
        name, kind = o["package"]["clearName"], MONETIZATION_LABEL.get(o["monetizationType"], o["monetizationType"].lower())
        if (name, kind) not in seen:
            seen.add((name, kind))
            parts.append(f"{name} ({kind})")
    return "streaming: " + ", ".join(parts)


async def imdb_id(query: str, category: str = "") -> str | None:
    """The best-matching IMDb id (tt...) for a title, cached. Used by torrentio and runtime_min."""
    key = (query.lower(), category)
    hit = _id_cache.get(key)
    if hit and time.time() - hit[0] < media.CACHE_TTL:
        return hit[1]
    hits = await _suggest(query)
    wanted = QID_FOR_CATEGORY.get(category)
    best = next((h for h in hits if h.get("qid") == wanted), None) or (hits[0] if hits else None)
    result = best["id"] if best else None
    _id_cache[key] = (time.time(), result)
    return result


async def runtime_min(query: str, category: str = "movies") -> int | None:
    """Runtime in minutes, cached; feeds quality.classify's size-sanity check for torrents."""
    key = (query.lower(), category)
    hit = _runtime_cache.get(key)
    if hit and time.time() - hit[0] < media.CACHE_TTL:
        return hit[1]
    tt = await imdb_id(query, category)
    minutes = None
    if tt:
        m = re.search(r"(\d+)", (await _cinemeta(tt, category)).get("runtime") or "")
        minutes = int(m.group(1)) if m else None
    _runtime_cache[key] = (time.time(), minutes)
    return minutes


async def imdb(query: str, limit: int, category: str = "") -> list[dict]:
    """IMDb suggestions: what the title is. The top hit is enriched with runtime (Cinemeta) and
    streaming status (JustWatch); the rest just get title/year/cast, to keep this fast."""
    hits = await _suggest(query)
    wanted = QID_FOR_CATEGORY.get(category)
    if wanted:
        hits = [h for h in hits if h.get("qid") == wanted] or hits
    out = []
    for i, h in enumerate(hits[:limit]):
        tt = h["id"]
        key = (query.lower(), category)
        if i == 0:
            _id_cache[key] = (time.time(), tt)
        length = status = ""
        if i == 0 and category in ("movies", "tv"):
            m = re.search(r"(\d+)", (await _cinemeta(tt, category)).get("runtime") or "")
            minutes = int(m.group(1)) if m else None
            _runtime_cache[key] = (time.time(), minutes)
            length = f"{minutes // 60}h {minutes % 60}m" if minutes else ""
            status = await _watch_status(h["l"], category) or ""
        info = " · ".join(x for x in (str(h.get("y") or ""), length, h.get("s"), status) if x)
        out.append({"source": "imdb", "title": h["l"], "size": "", "seeders": 0,
                    "year": str(h.get("y") or ""), "info": info, "url": f"https://www.imdb.com/title/{tt}/"})
    return out
