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


async def _search_one(name: str, query: str, n: int) -> list[dict]:
    fn = providers.REGISTRY[name]
    try:
        return await fn(query, n, ENV, TIMEOUT)
    except Exception as e:  # noqa: BLE001 - any provider failure falls through
        raise providers.ProviderError(f"{name}: {e}") from e


@mcp.tool()
async def web_search(query: str, num_results: int = 8, strategy: str = "fallback") -> str:
    """Search the web. strategy=fallback: first provider that works (SearXNG -> keyless -> cloud keys).
    strategy=merge: query top 3 providers in parallel, dedupe, diverse ranked results."""
    avail = _available_search_providers()
    if not avail:
        return "No search providers available (quota exhausted or none enabled)."

    if strategy == "merge":
        names = [p["name"] for p in avail[:3]]
        tasks = [_search_one(n, query, num_results) for n in names]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        merged, seen = [], set()
        for name, res in zip(names, results):
            if isinstance(res, Exception):
                continue
            quota.record(name)
            for r in res:
                key = re.sub(r"[?#].*$", "", r["url"].rstrip("/")).lower()
                if key not in seen:
                    seen.add(key)
                    merged.append({**r, "via": name})
        if not merged:
            return f"All merge providers failed for: {query}"
        out = [f"[{r['via']}] {r['title']}\n  {r['url']}\n  {r['snippet']}" for r in merged[:num_results]]
        return "\n\n".join(out)

    # fallback strategy
    errors = []
    for p in avail:
        try:
            results = await _search_one(p["name"], query, num_results)
            quota.record(p["name"])
            out = [f"{r['title']}\n  {r['url']}\n  {r['snippet']}" for r in results]
            return f"(via {p['name']})\n" + "\n\n".join(out)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
    return f"All providers failed for: {query}\n" + "\n".join(errors)


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
            return f"Reddit fetch failed ({e}); falling back to generic fetch."
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
