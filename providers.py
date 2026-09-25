"""Search provider adapters. Each returns list[{title,url,snippet}] or raises ProviderError."""
import re
import urllib.parse

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


class ProviderError(Exception):
    pass


async def search_searxng(query: str, n: int, env: dict, timeout: int, opts=None) -> list[dict]:
    base = env.get("SEARXNG_URL", "").rstrip("/")
    if not base:
        raise ProviderError("SEARXNG_URL not configured")
    params = {"q": query, "format": "json"}
    if opts:
        params.update({k: v for k, v in opts.items()
                       if k in ("time_range", "categories", "pageno", "language", "safesearch")})
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(f"{base}/search", params=params,
                        headers={"Accept": "application/json"})
        r.raise_for_status()
    data = r.json()
    if not data.get("results") and data.get("unresponsive_engines"):
        down = ", ".join(f"{e} ({why})" for e, why in data["unresponsive_engines"])
        raise ProviderError(f"searxng: no results, upstream engines down: {down}")
    return [{"title": x.get("title", ""), "url": x.get("url", ""),
             "snippet": (x.get("content") or "")[:300], "published": (x.get("publishedDate") or "")[:10],
             **({"img_src": x["img_src"], "thumbnail": x.get("thumbnail_src", "")}
                if x.get("img_src") else {})}
            for x in data.get("results", [])[:n]]


async def search_duckduckgo(query: str, n: int, env: dict, timeout: int, opts=None) -> list[dict]:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": UA}) as c:
        r = await c.post("https://html.duckduckgo.com/html/",
                         data={"q": query, **{k: v for k, v in (opts or {}).items() if k == "df"}})
        r.raise_for_status()
    anchors = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S)
    out, seen = [], set()
    for i, (href, title) in enumerate(anchors):
        if "uddg=" in href:  # legacy redirect wrapper
            href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
        if href in seen or href.startswith("//duckduckgo.com"):
            continue
        seen.add(href)
        snippet = re.sub(r"<[^>]+>", "", snippets[i]) if i < len(snippets) else ""
        out.append({"title": re.sub(r"<[^>]+>", "", title)[:120], "url": href,
                    "snippet": snippet[:300]})
        if len(out) >= n:
            break
    if not out:
        raise ProviderError("duckduckgo: no results parsed")
    return out


async def search_tavily(query: str, n: int, env: dict, timeout: int, opts=None) -> list[dict]:
    key = env.get("TAVILY_API_KEY")
    if not key:
        raise ProviderError("TAVILY_API_KEY missing")
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post("https://api.tavily.com/search",
                         json={"api_key": key, "query": query, "max_results": n,
                               **{k: v for k, v in (opts or {}).items()
                                  if k in ("time_range", "topic", "days", "include_domains", "exclude_domains")}})
        r.raise_for_status()
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "snippet": (x.get("content") or "")[:300], "published": (x.get("published_date") or "")[:10]}
                for x in r.json().get("results", [])[:n]]


async def search_exa(query: str, n: int, env: dict, timeout: int, opts=None) -> list[dict]:
    key = env.get("EXA_API_KEY")
    if not key:
        raise ProviderError("EXA_API_KEY missing")
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post("https://api.exa.ai/search",
                         headers={"x-api-key": key},
                         json={"query": query, "numResults": n,
                               "contents": {"text": {"maxCharacters": 300}},
                               **{k: v for k, v in (opts or {}).items()
                                  if k in ("startPublishedDate", "category", "includeDomains", "excludeDomains")}})
        r.raise_for_status()
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "snippet": (x.get("text") or "")[:300], "published": (x.get("publishedDate") or "")[:10]}
                for x in r.json().get("results", [])[:n]]


async def search_firecrawl(query: str, n: int, env: dict, timeout: int, opts=None) -> list[dict]:
    key = env.get("FIRECRAWL_API_KEY")
    if not key:
        raise ProviderError("FIRECRAWL_API_KEY missing")
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post("https://api.firecrawl.dev/v2/search",
                         headers={"Authorization": f"Bearer {key}"},
                         json={"query": query, "limit": n,
                               **{k: v for k, v in (opts or {}).items() if k in ("tbs",)}})
        r.raise_for_status()
        data = r.json().get("data") or {}
        items = data.get("web") or data.get("results") or []
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "snippet": (x.get("description") or x.get("content") or "")[:300]}
                for x in items[:n]]


async def search_jina(query: str, n: int, env: dict, timeout: int, opts=None) -> list[dict]:
    key = env.get("JINA_API_KEY")
    if not key:
        raise ProviderError("JINA_API_KEY missing")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"https://s.jina.ai/{urllib.parse.quote(query)}",
                        headers={"Authorization": f"Bearer {key}",
                                 "Accept": "application/json"})
        r.raise_for_status()
        items = r.json().get("data", [])
        BLOCKED = "You've been blocked"
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "snippet": "" if (x.get("description") or x.get("content") or "").startswith(BLOCKED)
                 else (x.get("description") or x.get("content") or "")[:300]}
                for x in items[:n] if x.get("url")]


# name -> async fn(query, n, env, timeout) -> list[dict]
REGISTRY = {
    "searxng": search_searxng,
    "duckduckgo": search_duckduckgo,
    "tavily": search_tavily,
    "exa": search_exa,
    "firecrawl": search_firecrawl,
    "jina": search_jina,
}
