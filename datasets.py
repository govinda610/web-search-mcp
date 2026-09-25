"""Keyless datasets: GDELT news trends, SEC filings, OpenStreetMap places, Google News."""
import asyncio
import html as htmllib
import os
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from googlenewsdecoder import GoogleDecoderAsync

from media import http
from providers import ProviderError

GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
_gdelt_lock = asyncio.Lock()
_gdelt_last = 0.0


async def _gdelt_get(params: dict) -> dict:
    """GDELT asks for at most one request every 5 seconds."""
    global _gdelt_last
    async with _gdelt_lock:
        wait = 5.0 - (time.time() - _gdelt_last)
        if wait > 0:
            await asyncio.sleep(wait)
        qs = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
        r = await http(f"{GDELT}?{qs}", timeout=25)  # GDELT can take well over media.http's default 12s
        _gdelt_last = time.time()
        return r.json()


async def news_trends(query: str, timespan: str = "1w", limit: int = 10) -> str:
    """Recent articles plus daily coverage volume for a topic, via GDELT's DOC 2.0 API."""
    volume = await _gdelt_get({"query": query, "mode": "TimelineVolRaw", "format": "json", "timespan": timespan})
    articles = await _gdelt_get({"query": query, "mode": "ArtList", "format": "json",
                                 "maxrecords": limit, "timespan": timespan})
    lines = [f"coverage volume ({timespan}):"]
    points = (volume.get("timeline") or [{}])[0].get("data", [])
    seen_days: dict[str, int] = {}
    for p in points:  # GDELT buckets by hour; roll up to per-day counts
        day = p.get("date", "")[:8]
        seen_days[day] = seen_days.get(day, 0) + p.get("value", 0)
    for day, count in seen_days.items():
        lines.append(f"  {day[:4]}-{day[4:6]}-{day[6:8]}: {count} articles")
    lines.append("\narticles:")
    for a in articles.get("articles", [])[:limit]:
        lines.append(f"- {a.get('title', '')} ({a.get('domain', '')}, {a.get('seendate', '')})\n  {a.get('url', '')}")
    return "\n".join(lines)


def _rss_field(item: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", item, re.DOTALL)
    return htmllib.unescape(m.group(1).strip()) if m else ""


async def google_news(query: str, recency: str, limit: int) -> list[dict]:
    """Google News RSS. The link is Google's own redirect URL, not the article's; resolving it
    needs a follow-up request per article (it's a JS redirect, not an HTTP one), so it's left as-is."""
    op = {"day": "1d", "week": "7d", "month": "30d", "year": "1y"}.get(recency, "")
    q = f"{query} when:{op}" if op else query
    r = await http(f"https://news.google.com/rss/search?q={quote(q)}&hl=en-US&gl=US&ceid=US:en")
    out = []
    for item in r.text.split("<item>")[1:limit + 1]:
        title, source = _rss_field(item, "title"), _rss_field(item, "source")
        date = _rss_field(item, "pubDate")
        try:
            date = parsedate_to_datetime(date).strftime("%Y-%m-%d") if date else ""
        except ValueError:
            date = ""
        out.append({"title": title.removesuffix(f" - {source}") if source else title,
                    "url": _rss_field(item, "link"), "source": source, "date": date})
    return out



async def resolve_google_news(items: list[dict]) -> None:
    """Swap Google News redirect links (300+ characters each) for the article URLs, in place.
    Google hides the target behind a signed batchexecute call, so this takes one page fetch
    per article plus one POST for all of them. A link that doesn't resolve stays as it was."""
    wrapped = [r for r in items if "news.google.com/" in r["url"]]
    if not wrapped:
        return
    try:
        async with GoogleDecoderAsync(timeout=10) as decoder:
            decoded = await decoder.decode_google_news_urls([r["url"] for r in wrapped])
    except Exception:  # noqa: BLE001 - the long links still work
        return
    for r, d in zip(wrapped, decoded):
        if d.get("success"):
            r["url"] = d["decoded_url"]

# ---------------------------------------------------------------- SEC EDGAR

_SEC_TICKERS: dict | None = None


def _sec_ua() -> str:
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua or "@" not in ua:
        raise ProviderError(
            'SEC requires a contact email in the request User-Agent. Set SEC_USER_AGENT="Your Name '
            'you@example.com" and try again.')
    return ua


async def _sec_lookup(company: str) -> tuple[str, str]:
    """(10-digit CIK, official title) for a ticker or company name."""
    global _SEC_TICKERS
    ua = _sec_ua()
    if _SEC_TICKERS is None:
        data = (await http("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": ua})).json()
        _SEC_TICKERS = {v["ticker"].upper(): v for v in data.values()}
    q = company.strip().upper()
    hit = _SEC_TICKERS.get(q)
    if not hit:
        hit = next((v for v in _SEC_TICKERS.values() if company.lower() in v["title"].lower()), None)
    if not hit:
        raise ProviderError(f"No SEC filer found for {company!r}.")
    return f"{hit['cik_str']:010d}", hit["title"]


async def _sec_facts(cik: str, ua: str) -> str:
    """A few key financial facts, best-effort (not every filer reports these tags)."""
    try:
        data = (await http(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
                           headers={"User-Agent": ua})).json()
    except Exception:  # noqa: BLE001 - facts are a bonus on top of the filing list
        return ""
    gaap = data.get("facts", {}).get("us-gaap", {})
    facts = []
    for tag, label in (("Revenues", "revenue"), ("NetIncomeLoss", "net income"), ("Assets", "assets")):
        units = gaap.get(tag, {}).get("units", {}).get("USD", [])
        if units:
            latest = max(units, key=lambda u: u.get("end", ""))
            facts.append(f"{label}: ${latest['val']:,} (as of {latest.get('end')})")
    return "key financials: " + ", ".join(facts) if facts else ""


async def sec_filings(company: str, form: str = "", limit: int = 10) -> str:
    """Recent EDGAR filings for a ticker or company name, optionally filtered to one form
    (10-K, 10-Q, 8-K, ...), plus a few headline financial facts when available."""
    cik, name = await _sec_lookup(company)
    ua = _sec_ua()
    data = (await http(f"https://data.sec.gov/submissions/CIK{cik}.json", headers={"User-Agent": ua})).json()
    recent = data.get("filings", {}).get("recent", {})
    rows = list(zip(recent.get("form", []), recent.get("filingDate", []), recent.get("primaryDocument", []),
                    recent.get("accessionNumber", []), recent.get("primaryDocDescription", [])))
    if form:
        rows = [r for r in rows if r[0].upper() == form.upper()]
    lines = [f"{name} (CIK {cik})"]
    for f, date, doc, accn, desc in rows[:limit]:
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/{doc}"
        lines.append(f"- {f} filed {date}: {desc or doc}\n  {url}")
    if not rows:
        lines.append("no filings found" + (f" of form {form}" if form else ""))
    facts = await _sec_facts(cik, ua)
    if facts:
        lines.append("\n" + facts)
    return "\n".join(lines)


# ---------------------------------------------------------------- places (OpenStreetMap)

_geocode_cache: dict[str, tuple[float, float, str]] = {}
_OSM_UA = "web-search-mcp/1.0 (+https://github.com/govinda610/web-search-mcp)"
_OVERPASS_TAGS = ("amenity", "shop", "tourism", "leisure")
# the main instance is often overloaded (504); mail.ru runs a public mirror
_OVERPASS = ("https://overpass-api.de/api/interpreter", "https://maps.mail.ru/osm/tools/overpass/api/interpreter")


async def _geocode(place: str) -> tuple[float, float, str] | None:
    if place in _geocode_cache:
        return _geocode_cache[place]
    try:
        results = (await http(f"https://nominatim.openstreetmap.org/search?q={quote(place)}&format=jsonv2&limit=1",
                              headers={"User-Agent": _OSM_UA})).json()
    except Exception:  # noqa: BLE001 - fall back to Photon below
        results = []
    if results:
        top = results[0]
        _geocode_cache[place] = (float(top["lat"]), float(top["lon"]), top.get("display_name", place))
        return _geocode_cache[place]
    try:
        feats = (await http(f"https://photon.komoot.io/api/?q={quote(place)}&limit=1")).json().get("features", [])
    except Exception:  # noqa: BLE001 - no geocoder could resolve this place
        feats = []
    if not feats:
        return None
    lon, lat = feats[0]["geometry"]["coordinates"]
    name = feats[0]["properties"].get("name", place)
    _geocode_cache[place] = (lat, lon, name)
    return _geocode_cache[place]


async def places(query: str, near: str = "", limit: int = 10) -> str:
    """A bare place ("Koramangala, Bangalore") is geocoded directly. With `near` set, `query`
    (e.g. "cafe") is searched for around that place via Overpass."""
    place = near or query
    geo = await _geocode(place)
    if not geo:
        return f"No place found for {place!r}."
    lat, lon, name = geo
    if not near:
        return f"{name}\nlat/lon: {lat}, {lon}\nhttps://www.openstreetmap.org/?mlat={lat}&mlon={lon}"
    word = (query.strip().split() or [""])[0].lower()
    filters = "".join(f'node["{tag}"="{word}"](around:2000,{lat},{lon});' for tag in _OVERPASS_TAGS)
    ql = f"[out:json][timeout:20];({filters});out center {limit * 2};"
    for endpoint in _OVERPASS:
        try:  # Overpass answers 406 to some User-Agents (curl's among them), so send ours
            r = await http(endpoint, form={"data": ql}, timeout=25, headers={"User-Agent": _OSM_UA})
            break
        except Exception as e:  # noqa: BLE001 - HTTP errors and TLS failures alike: try the mirror
            error = e
    else:
        raise RuntimeError(f"OpenStreetMap search (Overpass) unavailable: {error}")
    elements = r.json().get("elements", [])[:limit]
    if not elements:
        return f"No {query!r} found near {name}."
    lines = [f"{query} near {name}:"]
    for e in elements:
        tags = e.get("tags", {})
        addr = " ".join(x for x in (tags.get("addr:housenumber", ""), tags.get("addr:street", ""),
                                    tags.get("addr:city", "")) if x).strip()
        bits = [f"- {tags.get('name', '?')} ({addr or '?'})"]
        if tags.get("opening_hours"):
            bits.append(tags["opening_hours"])
        if tags.get("website"):
            bits.append(tags["website"])
        lines.append(", ".join(bits) + f"\n  https://www.openstreetmap.org/node/{e['id']}")
    return "\n".join(lines)
