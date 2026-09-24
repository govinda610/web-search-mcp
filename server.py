"""web-search MCP server — self-owned search/fetch stack (MCP SDK v2, spec 2026-07-28).

Tools: web_search (fallback/merge/exhaustive, LLM answer/highlights/auto), news_search,
suggest, image_search, fetch_page (smart router), fetch_pages (concurrent), reddit_fetch,
youtube_transcript, instagram_fetch, usage_status.
Run: uv run server.py (stdio) | MCP_TRANSPORT=http uv run server.py (shared HTTP instance)
"""
import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

import llm
import providers
import quota
import sources

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
CONFIG = json.loads((ROOT / "config.json").read_text())

import os  # noqa: E402  (after dotenv)
ENV = {k: os.environ.get(k, "") for k in [
    "SEARXNG_URL", "CRAWL4AI_URL", "CRAWL4AI_TOKEN",
    "TAVILY_API_KEY", "EXA_API_KEY", "FIRECRAWL_API_KEY", "JINA_API_KEY"]}

mcp = MCPServer("web-search")
CACHE = ROOT / "state" / "cache"
TIMEOUT = CONFIG.get("request_timeout_seconds", 15)


def _available_search_providers() -> list[dict]:
    """Enabled providers with quota left, in configured priority order, that we have adapters for."""
    out = []
    for p in CONFIG["search_providers"]:
        if not p["enabled"] or p["name"] not in providers.REGISTRY:
            continue
        if quota.remaining(p["name"], p["monthly_limit"]) == 0:
            continue
        out.append(p)
    return out


async def _search_one(name: str, query: str, n: int, opts=None) -> list[dict]:
    fn = providers.REGISTRY[name]
    try:
        return await fn(query, n, ENV, TIMEOUT, opts=opts)
    except Exception as e:  # noqa: BLE001 - any provider failure falls through
        raise providers.ProviderError(f"{name}: {e}") from e


_RECENCY_DAYS = {"day": 1, "week": 7, "month": 31, "year": 366}


def _recency_opts(name: str, recency: str) -> dict:
    """Tavily/Exa/Firecrawl-style recency, mapped to each provider's native param."""
    if recency not in _RECENCY_DAYS:
        return {}
    if name in ("searxng", "tavily"):
        return {"time_range": recency}
    if name == "exa":
        from datetime import date, timedelta
        return {"startPublishedDate": str(date.today() - timedelta(days=_RECENCY_DAYS[recency]))}
    if name == "firecrawl":
        return {"tbs": "qdr:" + {"day": "d", "week": "w", "month": "m", "year": "y"}[recency]}
    return {}


def _domain_ok(url: str, include: str, exclude: str) -> bool:
    """Tavily-style include_domains/exclude_domains (comma-separated substrings)."""
    inc = [d.strip().lower() for d in include.split(",") if d.strip()]
    exc = [d.strip().lower() for d in exclude.split(",") if d.strip()]
    u = url.lower()
    if inc and not any(d in u for d in inc):
        return False
    return not any(d in u for d in exc)


async def _auto_classify(query: str) -> dict:
    r = await llm.ask(
        'Classify this web-search query as JSON only: {"strategy":"fallback|merge|exhaustive",'
        f'"news":true|false,"recency":"|day|week|month|year"}}. Query: {query}', max_tokens=60)
    if not r:
        return {}
    m = re.search(r"\{.*\}", r, re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(0))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


async def _enrich(query, items, want_answer, want_highlights) -> str:
    """LLM answer + highlights; silently skipped if coding-plan models are down/exhausted."""
    if not ((want_answer or want_highlights) and items and llm.llm_available()):
        return ""
    ctx = chr(10).join(
        f"[{i + 1}] {r['title']} | {r['url']} | {r['snippet'][:220]}"
        for i, r in enumerate(items[:12]))
    extra = ""
    if want_answer:
        a = await llm.ask(f"Answer using ONLY the numbered results; cite as [n]. Query: {query}"
                          f"{chr(10)}{chr(10)}{ctx}", max_tokens=500)
        if a:
            extra += "ANSWER (LLM-synthesized, [n] = source):\n" + a + chr(10) + chr(10)
    if want_highlights:
        h = await llm.ask(f"5 key facts with [n] citations. Query: {query}"
                          f"{chr(10)}{chr(10)}{ctx}", max_tokens=350)
        if h:
            extra += "HIGHLIGHTS:" + chr(10) + h + chr(10) + chr(10)
    return extra
@mcp.tool()
async def web_search(query: str, num_results: int = 8, strategy: str = "fallback",
                     include_domains: str = "", exclude_domains: str = "",
                     recency: str = "", answer: bool = False,
                     highlights: bool = False, auto: bool = False) -> str:
    """Search the web.
    strategy: fallback (first working) | merge (top 3 parallel) | exhaustive (ALL
    providers parallel, deduped, max 2/domain).
    include/exclude_domains: comma-separated substrings. recency: day|week|month|year.
    answer: LLM synthesis with [n] citations. highlights: LLM key-fact bullets.
    auto: LLM classifies query and picks strategy/recency/news routing.
    LLM features use coding-plan models and degrade gracefully to plain results."""
    avail = _available_search_providers()
    if not avail:
        return "No search providers available (quota exhausted or none enabled)."
    if auto and llm.llm_available():
        spec = await _auto_classify(query)
        strategy = spec.get("strategy") or strategy
        recency = spec.get("recency") or recency
        if spec.get("news"):
            return await news_search(query, num_results, recency or "week")
    items, errors = [], []
    if strategy in ("merge", "exhaustive"):
        names = [p["name"] for p in (avail[:3] if strategy == "merge" else avail)]
        results = await asyncio.gather(
            *[_search_one(n, query, num_results, _recency_opts(n, recency)) for n in names],
            return_exceptions=True)
        seen, per_domain = set(), {}
        for name, res in zip(names, results):
            if isinstance(res, Exception):
                errors.append(f"{name}: {res}")
                continue
            quota.record(name)
            for r in res:
                key = re.sub(r"[?#].*$", "", r["url"].rstrip("/")).lower()
                dom = urlparse(r["url"]).netloc
                if key in seen or per_domain.get(dom, 0) >= 2 \
                        or not _domain_ok(r["url"], include_domains, exclude_domains):
                    continue
                seen.add(key)
                per_domain[dom] = per_domain.get(dom, 0) + 1
                items.append({**r, "via": name})
        items = items[:num_results]
        if not items:
            return f"All providers failed for: {query}" + chr(10) + chr(10).join(errors)
    else:
        for p in avail:
            try:
                raw = await _search_one(p["name"], query, num_results,
                                        _recency_opts(p["name"], recency))
                items = [{**r, "via": p["name"]} for r in raw
                         if _domain_ok(r["url"], include_domains, exclude_domains)]
                if items:
                    quota.record(p["name"])
                    break
                errors.append(f"{p['name']}: 0 results after filtering")
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))
        if not items:
            return f"All providers failed for: {query}" + chr(10) + chr(10).join(errors)
    out = await _enrich(query, items, answer, highlights)
    out += chr(10).join(
        f"[{r['via']}] {r['title']}{chr(10)}  {r['url']}{chr(10)}  {r['snippet']}"
        for r in items[:num_results])
    return out


@mcp.tool()
async def news_search(query: str, num_results: int = 5, recency: str = "day") -> str:
    """Search recent news (SearXNG news vertical, then Tavily news topic). recency: day|week|month."""
    for name in ("searxng", "tavily"):
        if not any(p["name"] == name for p in _available_search_providers()):
            continue
        opts = ({"categories": "news", "time_range": recency} if name == "searxng"
                else {"topic": "news", "time_range": recency})
        try:
            results = await _search_one(name, query, num_results, opts)
            quota.record(name)
            out = [f"{r['title']}\n  {r['url']}\n  {r['snippet']}" for r in results[:num_results]]
            return f"(via {name} news)\n" + "\n\n".join(out)
        except Exception:
            continue
    return "News search unavailable (searxng and tavily both failed)."


@mcp.tool()
async def suggest(query: str) -> str:
    """Autocomplete suggestions for a partial query (DuckDuckGo, free, no key)."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get("https://duckduckgo.com/ac/",
                        params={"q": query, "type": "list"},
                        headers={"User-Agent": providers.UA})
        r.raise_for_status()
        data = r.json()
    items = data[0] if data and isinstance(data[0], list) else data
    return "\n".join(str(s) for s in items[:10]) or "No suggestions."


def _cache_get(url: str) -> str | None:
    f = CACHE / (hashlib.md5(url.encode()).hexdigest() + ".txt")
    if f.exists() and time.time() - f.stat().st_mtime < CONFIG["cache_ttl_minutes"] * 60:
        return f.read_text()[:20000]
    return None


def _cache_put(url: str, text: str) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / (hashlib.md5(url.encode()).hexdigest() + ".txt")).write_text(text[:20000])


async def _httpx_fetch(url: str) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                 headers={"User-Agent": providers.UA}) as c:
        r = await c.get(url)
        if r.status_code in (403, 429, 503) or "Just a moment" in r.text[:2000]:
            raise providers.ProviderError(f"blocked ({r.status_code})")
        r.raise_for_status()
        return r.text


async def _curl_cffi_fetch(url: str):
    """TLS-impersonated fetch; returns None if curl_cffi not installed."""
    try:
        from curl_cffi import requests as cffi
    except ImportError:
        return None
    return await asyncio.to_thread(
        lambda: cffi.get(url, impersonate="chrome", timeout=TIMEOUT,
                         allow_redirects=True).text)


async def _jina_fetch(url: str) -> str:
    key = ENV.get("JINA_API_KEY")
    if not key:
        raise providers.ProviderError("JINA_API_KEY missing")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"https://r.jina.ai/{url}",
                        headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        return r.text


def _strip_html(html: str) -> str:
    html = re.sub(r"(?s)<(script|style|nav|footer|header).*?</\1>", " ", html)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


@mcp.tool()
async def fetch_page(url: str) -> str:
    """Fetch a web page as clean text. Smart-routes Reddit/YouTube to dedicated fetchers.
    Generic escalation: direct -> TLS-impersonated (anti-bot) -> Jina reader."""
    kind = sources.classify(url)
    if kind == "reddit":
        try:
            return "(reddit)\n" + await sources.reddit_fetch(url)
        except Exception as e:  # noqa: BLE001
            return f"Reddit fetch failed: {e}"  # never fall through to curl_cffi for reddit
    if kind == "youtube":
        try:
            return await sources.youtube_transcript(url)
        except Exception as e:  # noqa: BLE001
            return f"Transcript unavailable ({e})."
    cached = _cache_get(url)
    if cached:
        return f"(cached)\n{cached}"
    attempts = []
    for name, fn in [("direct", lambda: _httpx_fetch(url)),
                     ("curl_cffi", lambda: _curl_cffi_fetch(url)),
                     ("jina", lambda: _jina_fetch(url))]:
        try:
            raw = await fn()
            if not raw:
                attempts.append(f"{name}: empty")
                continue
            text = _strip_html(raw) if "<" in raw[:2000] and name != "jina" else raw
            # JS-shell detection: tiny text from a huge page = soft failure, escalate.
            # example.com (~1KB raw) still passes; 62KB SPA shells do not.
            if name != "jina" and len(text) < 300 and len(raw) > 20000:
                attempts.append(f"{name}: JS shell ({len(raw)}B html -> {len(text)}ch text)")
                continue
            _cache_put(url, text)
            return f"(via {name})\n{text[:15000]}"
        except Exception as e:  # noqa: BLE001
            attempts.append(f"{name}: {e}")
    return f"Fetch failed for {url}\n" + "\n".join(attempts)


@mcp.tool()
async def reddit_fetch(target: str, sort: str = "hot", limit: int = 15) -> str:
    """Fetch a Reddit subreddit feed or post with top comments. Free public JSON, no key.
    target: post URL, or subreddit like 'r/valheim' or 'valheim'. sort: hot|new|top|best."""
    try:
        return await sources.reddit_fetch(target, sort=sort, limit=limit)
    except Exception as e:  # noqa: BLE001
        return f"Reddit fetch failed: {e}"


@mcp.tool()
async def youtube_transcript(url: str, lang: str = "en") -> str:
    """Get the transcript (captions or auto-generated) for a YouTube video URL. Free."""
    try:
        return await sources.youtube_transcript(url, lang=lang)
    except Exception as e:  # noqa: BLE001
        return f"Transcript unavailable: {e}"


@mcp.tool()
async def image_search(query: str, num_results: int = 10) -> str:
    """Search images via SearXNG image vertical (self-hosted, unlimited, no key)."""
    try:
        if "searxng" not in [p["name"] for p in _available_search_providers()]:
            return "SearXNG unavailable (is the docker container running on :8888?)"
        raw = await _search_one("searxng", query, num_results, {"categories": "images"})
        quota.record("searxng")
        out = []
        for r in raw[:num_results]:
            thumb = r.get("thumbnail") or r.get("img_src") or r["url"]
            out.append(f"{r['title'] or query}\n  img: {r.get('img_src', r['url'])}\n  thumb: {thumb}\n  page: {r['url']}")
        return "\n\n".join(out) if out else "No image results."
    except Exception as e:  # noqa: BLE001
        return f"Image search failed: {e}"


@mcp.tool()
async def fetch_pages(urls: str, concurrency: int = 5) -> str:
    """Fetch several pages concurrently (SearXNG/local cache make this cheap). urls: space or comma separated."""
    us = [u.strip() for u in re.split(r"[,\s]+", urls) if u.strip().startswith("http")]
    if not us:
        return "No URLs given."
    us = us[:20]
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(u: str) -> str:
        async with sem:
            return await fetch_page(u)
    results = await asyncio.gather(*(one(u) for u in us), return_exceptions=True)
    parts = []
    for u, r in zip(us, results):
        parts.append(f"===== {u} =====\n{r if isinstance(r, str) else f'FAILED: {r}'}")
    return chr(10).join(parts)


@mcp.tool()
async def instagram_fetch(url: str) -> str:
    """Best-effort Instagram public-page fetch (profile/post/hashtag).
    Instagram gates anonymous traffic hard; search coverage works via
    web_search(include_domains='instagram.com'). Logged-in extraction is
    the agent-browser/Chrome-CDP phase."""
    if "instagram.com" not in url.lower():
        return "Not an instagram.com URL."
    text = await fetch_page(url)
    low = text.lower()[:600]
    if "login" in low or "create account" in low or "blocked" in low:
        return ("Instagram hit its login/bot wall for anonymous requests. "
                "Use web_search with include_domains='instagram.com' for indexed content, "
                "or the agent-browser (logged-in Chrome) integration.")
    return text


@mcp.tool()
def usage_status() -> str:
    """Show this month's usage vs limits for every search provider + LLM call counts."""
    out = quota.status_table(CONFIG["search_providers"])
    llm_used = quota.llm_usage()
    if llm_used:
        out += "\n\nLLM calls this month (coding-plan models):\n" + "\n".join(
            f"  {k}: {v}" for k, v in sorted(llm_used.items()))
    return out


if __name__ == "__main__":
    if os.environ.get("MCP_TRANSPORT", "stdio") == "http":
        mcp.run(transport="streamable-http",
                host=os.environ.get("MCP_HOST", "127.0.0.1"),
                port=int(os.environ.get("MCP_PORT", "8765")),
                json_response=True)
    else:
        mcp.run()
