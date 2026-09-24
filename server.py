"""web-search MCP server — self-owned search/fetch stack.

Tools: web_search (fallback or merge across providers), fetch_page (stealth chain), usage_status.
Run: uv run server.py   (stdio MCP transport)
"""
import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

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

mcp = FastMCP("web-search")
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


@mcp.tool()
async def web_search(query: str, num_results: int = 8, strategy: str = "fallback",
                     include_domains: str = "", exclude_domains: str = "",
                     recency: str = "") -> str:
    """Search the web. strategy=fallback: first working provider (SearXNG -> keyless -> cloud).
    strategy=merge: top 3 providers in parallel, deduped, max 2 per domain, source-tagged.
    include_domains/exclude_domains: comma-separated (e.g. 'reddit.com'). recency: day|week|month|year."""
    avail = _available_search_providers()
    if not avail:
        return "No search providers available (quota exhausted or none enabled)."

    if strategy == "merge":
        names = [p["name"] for p in avail[:3]]
        tasks = [_search_one(n, query, num_results, _recency_opts(n, recency)) for n in names]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        merged, seen, per_domain = [], set(), {}
        for name, res in zip(names, results):
            if isinstance(res, Exception):
                continue
            quota.record(name)
            for r in res:
                key = re.sub(r"[?#].*$", "", r["url"].rstrip("/")).lower()
                dom = r["url"].split("/")[2] if "://" in r["url"] else ""
                if key in seen or per_domain.get(dom, 0) >= 2 \
                        or not _domain_ok(r["url"], include_domains, exclude_domains):
                    continue
                seen.add(key)
                per_domain[dom] = per_domain.get(dom, 0) + 1
                merged.append({**r, "via": name})
        if not merged:
            return f"All merge providers failed for: {query}"
        out = [f"[{r['via']}] {r['title']}\n  {r['url']}\n  {r['snippet']}" for r in merged[:num_results]]
        return "\n\n".join(out)

    errors = []
    for p in avail:
        try:
            raw = await _search_one(p["name"], query, num_results,
                                    _recency_opts(p["name"], recency))
            results = [r for r in raw if _domain_ok(r["url"], include_domains, exclude_domains)]
            quota.record(p["name"])
            out = [f"{r['title']}\n  {r['url']}\n  {r['snippet']}" for r in results[:num_results]]
            return f"(via {p['name']})\n" + "\n\n".join(out)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
    return f"All providers failed for: {query}\n" + "\n".join(errors)


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
def usage_status() -> str:
    """Show this month's usage vs limits for every configured search provider."""
    return quota.status_table(CONFIG["search_providers"])


if __name__ == "__main__":
    mcp.run()
