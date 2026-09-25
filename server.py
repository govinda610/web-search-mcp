"""web-search MCP server — self-owned search/fetch stack (MCP SDK v2).

Tools: web_search, news_search, image_search, knowledge_search, paper_search, paper_fetch,
fetch_page, fetch_pages, site_map, crawl_site, page_history, page_watch, youtube_transcript,
social_fetch, live_data, deep_research, media_search, book_download, media_download,
release_watch, server_status.
Run: uv run server.py (stdio) | MCP_TRANSPORT=http uv run server.py (shared HTTP instance)
"""
import asyncio
import contextlib
import json
import logging
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode, urlparse

from dotenv import load_dotenv
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

import crawl
import datasets
import discover
import download
import fetch
import health
import knowledge
import live
import llm
import media
import mirrors
import monitor
import papers
import providers
import quality
import quota
import rerank
import research
import similar
import social
import sources
import torrent
import watch
import wayback

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
for noisy in ("httpx", "httpcore"):  # one INFO line per request drowns the server log
    logging.getLogger(noisy).setLevel(logging.WARNING)
CONFIG = json.loads((ROOT / "config.json").read_text())
ENV = {k: os.environ.get(k, "") for k in [
    "SEARXNG_URL", "TAVILY_API_KEY", "EXA_API_KEY", "FIRECRAWL_API_KEY", "JINA_API_KEY"]}

INSTRUCTIONS = """Local, keyless web research tools. Which to use:
- a question or topic -> web_search (more_queries for several angles, depth="advanced" to read the top pages,
  similar_to=<url> for pages like one you have)
- something that happened recently -> news_search (trends=True for coverage volume over time)
- a broad question needing many sources and a cited report -> deep_research
- facts, code, dev Q&A, ML models/papers, packages, trials, drugs, case law -> knowledge_search
  (Wikipedia, HN, Stack Overflow, GitHub...; sites=["history"] searches pages already read, offline)
- a stock price, exchange rate, crypto price, weather, economic indicator, places or SEC filings -> live_data
- a specific URL -> fetch_page (several: fetch_pages; list a site's pages: site_map; read many: crawl_site);
  paywalled pages fall back to archive.today / Wayback on their own
- an old version of a page -> fetch_page with as_of, or page_history to list snapshots
- tell me when a page changes -> page_watch
- YouTube text -> youtube_transcript; Reddit, X/Twitter, Bluesky, Telegram, Instagram -> social_fetch
- research papers -> paper_search, then paper_fetch to read one
- a book, comic, manga, anime, film, show, game, audiobook, music, podcast or subtitles -> media_search;
  book_download saves a book by md5; release_watch notifies when a good-quality release appears
- save a video or audio (YouTube and ~1800 sites) as mp4/mp3/..., or a magnet link's files -> media_download
Long outputs are paged: pass start= as the output says. Failed calls return an error saying why."""

@contextlib.asynccontextmanager
async def _watch_loop(_server):
    """HTTP mode only (one long-lived process): re-check the watch list every WATCH_INTERVAL_HOURS."""
    task = asyncio.create_task(watch.run_forever(float(os.environ.get("WATCH_INTERVAL_HOURS", "6"))))
    try:
        yield {}
    finally:
        task.cancel()


mcp = MCPServer("web-search", title="Web search & research", instructions=INSTRUCTIONS,
                lifespan=_watch_loop if os.environ.get("MCP_TRANSPORT") == "http" else None)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITES_FILES = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
MediaSite = Literal[tuple(f.__name__ for f in media.SOURCES["all"])]
MediaCategory = Literal[tuple(media.SOURCES)]
KnowledgeSource = Literal[tuple(knowledge.SOURCES)]
TIMEOUT = CONFIG.get("request_timeout_seconds", 15)
TRACKING_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|dclid|msclkid|mc_[ce]id|ref|ref_src|igshid|si|spm)$")


async def _progress(ctx: Context | None, done: float, total: float | None, message: str) -> None:
    if ctx is not None:
        await ctx.report_progress(done, total, message)


def _download_dir(save_dir: str) -> Path:
    """Where a download may go: inside your home folder, not a hidden folder or ~/Library, so a
    prompt-injected page can't steer a file into ~/.ssh or LaunchAgents. DOWNLOAD_ALLOW_ANY_DIR=1 lifts it."""
    folder = Path(save_dir).expanduser().resolve()
    if os.environ.get("DOWNLOAD_ALLOW_ANY_DIR") == "1":
        return folder
    home = Path.home().resolve()
    if not folder.is_relative_to(home):
        raise ToolError(f"Not saving to {folder}: downloads go inside {home} (set DOWNLOAD_ALLOW_ANY_DIR=1 to allow).")
    parts = folder.relative_to(home).parts
    if parts and (parts[0] == "Library" or any(p.startswith(".") for p in parts)):
        raise ToolError(f"Not saving to {folder}: hidden folders and ~/Library are off limits for downloads.")
    return folder


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
    """Call one provider and count the call against its quota (paid APIs bill even for 0 hits)."""
    fn = providers.REGISTRY[name]
    try:
        results = await fn(query, n, ENV, TIMEOUT, opts=opts)
    except Exception as e:  # noqa: BLE001 - any provider failure falls through
        raise providers.ProviderError(f"{name}: {e}") from e
    quota.record(name)
    return results


_RECENCY_DAYS = {"day": 1, "week": 7, "month": 31, "year": 366}
_RECENCY_PROVIDERS = {"searxng", "tavily", "exa", "firecrawl", "duckduckgo"}
_NATIVE_DOMAINS = {"tavily": ("include_domains", "exclude_domains"), "exa": ("includeDomains", "excludeDomains")}
_SAFESEARCH = {"off": 0, "moderate": 1, "strict": 2}


def _recency_opts(name: str, recency: str) -> dict:
    """Map day|week|month|year to each provider's native date filter."""
    if recency not in _RECENCY_DAYS:
        return {}
    if name in ("searxng", "tavily"):
        return {"time_range": recency}
    if name == "exa":
        return {"startPublishedDate": str(date.today() - timedelta(days=_RECENCY_DAYS[recency]))}
    if name == "firecrawl":
        return {"tbs": "qdr:" + recency[0]}
    if name == "duckduckgo":
        return {"df": recency[0]}
    return {}


def _domain_list(domains: list[str] | None) -> list[str]:
    return [d.strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").strip("/")
            for d in domains or [] if d.strip()]


def _domain_ok(url: str, include: list[str] | None, exclude: list[str] | None) -> bool:
    """include/exclude match a host or any of its subdomains: 'reddit.com' matches old.reddit.com."""
    host = (urlparse(url).hostname or "").lower()

    def matches(domain: str) -> bool:
        return host == domain or host.endswith("." + domain)
    inc, exc = _domain_list(include), _domain_list(exclude)
    if inc and not any(matches(d) for d in inc):
        return False
    return not any(matches(d) for d in exc)


def _url_key(url: str) -> str:
    """Same page, different spelling: ignores scheme, www., trailing slash, #fragment and
    tracking parameters, but keeps real query parameters (?id=, ?v=, ?page=)."""
    p = urlparse(url)
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query) if not TRACKING_PARAMS.match(k.lower())))
    host = (p.hostname or "").lower().removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}" + (f"?{query}" if query else "")


async def _answer(query: str, items: list[dict], ctx: Context | None) -> str:
    """An LLM answer citing the numbered results, or "" if no model answers.
    The token budget is generous because these are reasoning models: thinking eats it first."""
    if not (llm.llm_available() or llm.can_sample(ctx)):
        return ""
    numbered = "\n".join(f"[{i + 1}] {r['title']} | {r['url']} | {r.get('content') or r.get('snippet', '')[:220]}"
                          for i, r in enumerate(items[:12]))
    answer = await llm.ask(f"Answer using ONLY the numbered results; cite as [n]. Query: {query}\n\n{numbered}",
                           max_tokens=2000, ctx=ctx)
    return f"ANSWER (LLM-written, [n] = result number):\n{answer}\n\n" if answer else ""


def _text_key(r: dict) -> str | None:
    """Same page under two URLs (/a-b and /a_b, mirrors, AMP) has the same title and snippet."""
    if not r.get("snippet"):
        return None  # a bare title like "Home" isn't enough to call two pages the same
    return re.sub(r"\W+", "", f"{r['title']} {r['snippet'][:120]}".lower())


def _short(text: str, limit: int = 220) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _interleave(runs: list[list[dict]], limit: int) -> list[dict]:
    """Each run's best, then each one's second best, ... deduped, so every run is represented."""
    out, seen = [], set()
    for rank in range(max((len(r) for r in runs), default=0)):
        for run in runs:
            if rank >= len(run):
                continue
            keys = {_url_key(run[rank]["url"]), _text_key(run[rank])} - {None}
            if not keys & seen:
                seen |= keys
                out.append(run[rank])
    return out[:limit]


async def _search(query: str, num_results: int, strategy: str, include: list[str] | None,
                  exclude: list[str] | None, recency: str, extra: dict) -> tuple[list[dict], list[str]]:
    """One query through the providers. Returns (items, errors).
    extra: SearXNG-only options (pageno, language, safesearch); set, they limit the run to SearXNG."""
    avail = _available_search_providers()
    if recency in _RECENCY_DAYS:
        avail = [p for p in avail if p["name"] in _RECENCY_PROVIDERS]
    if extra:
        avail = [p for p in avail if p["name"] == "searxng"]
    if not avail:
        return [], [("No search providers available for these options (quota exhausted, none enabled, "
                     "or page/language/safesearch need the local SearXNG).")]

    sites, blocked = _domain_list(include), _domain_list(exclude)

    def call(name: str):
        opts = {**_recency_opts(name, recency), **(extra if name == "searxng" else {})}
        q = query
        if name in _NATIVE_DOMAINS:  # these APIs filter by domain themselves
            inc_key, exc_key = _NATIVE_DOMAINS[name]
            opts.update({k: v for k, v in ((inc_key, sites), (exc_key, blocked)) if v})
        elif sites:  # tell the engines which sites we want; filtering afterwards alone often leaves nothing
            q = f"{query} " + " OR ".join(f"site:{d}" for d in sites)
        return _search_one(name, q, num_results, opts)

    def keep(name: str, raw: list[dict]) -> list[dict]:
        return [{**r, "via": name} for r in raw if _domain_ok(r["url"], include, exclude)]

    errors = []
    if strategy in ("merge", "exhaustive"):
        names = [p["name"] for p in avail if strategy == "exhaustive" or p["name"] in CONFIG["merge_providers"]]
        results = await asyncio.gather(*(call(n) for n in names), return_exceptions=True)
        runs = []
        for name, res in zip(names, results):
            if isinstance(res, Exception):
                errors.append(str(res))
            else:
                runs.append(keep(name, res))
        items, per_domain = [], {}
        for r in _interleave(runs, num_results * len(runs)):
            dom = urlparse(r["url"]).netloc
            if per_domain.get(dom, 0) >= 2 and not sites:  # diversity cap, unless specific sites were asked for
                continue
            per_domain[dom] = per_domain.get(dom, 0) + 1
            items.append(r)
        return items[:num_results], errors
    for p in avail:
        try:
            items = keep(p["name"], await call(p["name"]))
        except providers.ProviderError as e:
            errors.append(str(e))
            continue
        if items:
            return items[:num_results], errors
        errors.append(f"{p['name']}: 0 results after filtering")
    return [], errors


async def _read_url(url: str, interactive: bool = True, max_age: int | None = None, on_stage=None,
                    method: str = "auto") -> tuple[str, str]:
    """(via, text) for any URL, routed to the right reader. Reddit is never scraped directly
    (it bans the IP); social posts use their public APIs, falling back to the page fetcher."""
    kind = sources.classify(url)
    if kind == "reddit":
        return "reddit", await sources.reddit_fetch(url)
    if kind == "youtube":
        return "youtube transcript", await sources.youtube_transcript(url)
    if social.is_social(url):
        try:
            return "social", await social.read(url)
        except Exception:  # noqa: BLE001 - e.g. Instagram's rate limit: the page fetcher may still work
            pass
    via, text = await fetch.fetch_text(url, TIMEOUT, max_age=max_age, interactive=interactive,
                                       on_stage=on_stage, method=method)
    if kind == "instagram" and re.search(r"log ?in|sign up", text[:1500], re.IGNORECASE):
        raise ToolError("Instagram showed its login wall to an anonymous request. For indexed posts use "
                        "web_search(include_domains=['instagram.com']).")
    return via, text


async def _add_page_content(query: str, items: list[dict], top: int = 5) -> None:
    """depth=advanced: fetch the top pages in parallel and attach their most relevant passages.
    Never opens a visible browser window, and gives each page at most FETCH_DEADLINE seconds."""
    async def one(r):
        try:
            _, text = await asyncio.wait_for(_read_url(r["url"], interactive=False), fetch.FETCH_DEADLINE)
            r["content"] = await asyncio.to_thread(rerank.best_passages, text, query)
        except Exception as e:  # noqa: BLE001 - a failed page keeps its search snippet
            r["content"] = ""
            r["fetch_error"] = (str(e) or type(e).__name__)[:120]
    await asyncio.gather(*(one(r) for r in items[:top]))


@mcp.tool(title="Web search", annotations=READ_ONLY, structured_output=False)
async def web_search(
    query: Annotated[str, Field(description="What to search for, as you'd type it into a search engine.")],
    num_results: Annotated[int, Field(description="Results per query (1-20).", ge=1, le=20)] = 8,
    strategy: Annotated[Literal["fallback", "merge", "exhaustive"], Field(description=(
        "fallback: first provider that answers, local SearXNG first (fast, free). "
        "merge: a few providers in parallel. exhaustive: every provider in parallel, deduped."))] = "fallback",
    include_domains: Annotated[list[str] | None, Field(description=(
        'Only these sites; subdomains count. e.g. ["reddit.com", "arxiv.org"]'))] = None,
    exclude_domains: Annotated[list[str] | None, Field(description='Never these sites. e.g. ["pinterest.com"]')] = None,
    recency: Annotated[Literal["any", "day", "week", "month", "year"], Field(description=(
        "Only results published within this window."))] = "any",
    depth: Annotated[Literal["basic", "advanced"], Field(description=(
        "basic: titles + snippets. advanced: also reads the top 5 pages and adds their most "
        "relevant passages (slower, much more content)."))] = "basic",
    more_queries: Annotated[list[str] | None, Field(description=(
        "Up to 9 extra phrasings or sub-questions, searched in parallel with query and merged."))] = None,
    filetype: Annotated[str, Field(description='Only this file type, e.g. "pdf", "pptx", "csv". Empty = any.')] = "",
    page: Annotated[int, Field(description="Result page, for more results beyond the first (local SearXNG only).",
                               ge=1, le=10)] = 1,
    language: Annotated[str, Field(description=(
        'Language or region code, e.g. "en", "de", "en-IN", "fr-CA" (local SearXNG only). Empty = any.'))] = "",
    safesearch: Annotated[Literal["off", "moderate", "strict"], Field(description=(
        "Adult-content filter (local SearXNG only)."))] = "off",
    max_chars: Annotated[int, Field(description="Output budget; results past it are counted, not shown.",
                                    ge=1000)] = 20000,
    answer: Annotated[bool, Field(description="Prepend an LLM-written answer citing results as [n].")] = False,
    similar_to: Annotated[str, Field(description=(
        "Find pages like this URL on other sites instead; query then narrows the topic (or repeat the URL)."))] = "",
    ctx: Context | None = None,
) -> str:
    """Search the web. Returns numbered results: title, URL, date (when known) and snippet.
    For recent events use news_search; for papers paper_search; for books, films, anime, games
    media_search."""
    queries = [query] + [q for q in (more_queries or []) if q.strip()][:9]
    if filetype:
        queries = [f"{q} filetype:{filetype.strip('. ').lower()}" for q in queries]
    extra = {k: v for k, v in (("pageno", page if page > 1 else None), ("language", language or None),
                               ("safesearch", _SAFESEARCH[safesearch] if safesearch != "off" else None)) if v}
    if similar_to:
        await _progress(ctx, 0, None, f"reading {similar_to}")
        queries, errors = [query], []
        focus = "" if query.strip() == similar_to.strip() else query.strip()

        async def search(q: str, n: int) -> list[dict]:
            found, errs = await _search(f"{q} {focus}".strip(), n, strategy, include_domains, exclude_domains,
                                        recency, extra)
            errors.extend(errs)
            return found
        try:
            _title, items = await similar.find_similar(similar_to, search, num_results)
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"Couldn't read {similar_to}\n{e}") from e
    else:
        await _progress(ctx, 0, None, f"searching {len(queries)} quer{'y' if len(queries) == 1 else 'ies'}")
        runs = await asyncio.gather(*(_search(q, num_results, strategy, include_domains, exclude_domains,
                                              recency, extra) for q in queries))
        tagged = [[{**r, "query": q} for r in found] for q, (found, _) in zip(queries, runs)]
        items = _interleave(tagged, num_results * min(len(queries), 3))
        errors = [f"{q}: {e}" if len(queries) > 1 else e for q, (_, errs) in zip(queries, runs) for e in errs]
    if not items:
        raise ToolError(f"No results for {query!r}.\n" + "\n".join(errors))
    if depth == "advanced":
        await _progress(ctx, 1, None, f"reading the top {min(5, len(items))} pages")
        await _add_page_content(query, items)
    out = await _answer(query, items, ctx) if answer else ""
    merged = len({r["via"] for r in items}) > 1
    for n, r in enumerate(items, 1):
        tags = (f" [{r['via']}]" if merged else "") + (f" (q: {r['query']})" if len(queries) > 1 else "")
        date = f"{r['published']} | " if r.get("published") else ""
        block = f"{n}. {r['title']}{tags}\n  {r['url']}\n  {date}{_short(r.get('snippet', ''))}\n"
        if r.get("content"):
            block += "  --- page passages ---\n  " + r["content"].replace("\n", "\n  ") + "\n"
        elif r.get("fetch_error"):
            block += f"  (page not read: {r['fetch_error']})\n"
        if n > 1 and len(out) + len(block) > max_chars:
            out += f"({len(items) - n + 1} more results omitted: raise max_chars or narrow the query)\n"
            break
        out += block
    if errors:
        out += f"\n(note: {len(errors)} provider call(s) failed or found nothing: " + "; ".join(errors)[:400] + ")"
    return out


@mcp.tool(title="News search", annotations=READ_ONLY, structured_output=False)
async def news_search(
    query: Annotated[str, Field(description="Topic or event to find news about.")],
    num_results: Annotated[int, Field(description="Number of articles (1-20).", ge=1, le=20)] = 5,
    recency: Annotated[Literal["day", "week", "month", "year"], Field(description="How far back to look.")] = "day",
    trends: Annotated[bool, Field(description=(
        "Instead: GDELT's worldwide news index, with articles in many languages and daily coverage "
        "volume, to see how much a topic is in the news and when it peaked (up to 3 months back)."))] = False,
) -> str:
    """Recent news articles with source and date, from SearXNG's news engines and Google News
    merged (Tavily if SearXNG is down), or GDELT coverage trends."""
    if trends:
        try:
            timespan = {"day": "1d", "week": "1w", "month": "1m", "year": "3m"}[recency]
            return "(via GDELT)\n" + await datasets.news_trends(query, timespan, num_results)
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"GDELT news trends failed for {query!r}: {e}") from e

    async def engines() -> list[dict]:
        available = {p["name"] for p in _available_search_providers()}
        if "searxng" in available:
            # SearXNG drops every news engine without a date filter when time_range is set (all but
            # Bing), so ask unfiltered and keep the articles dated inside the window
            since = str(date.today() - timedelta(days=_RECENCY_DAYS[recency]))
            found = await _search_one("searxng", query, num_results * 3, {"categories": "news"})
            return [r for r in found if r.get("published", "") >= since]
        if "tavily" in available:
            return await _search_one("tavily", query, num_results, {"topic": "news", "time_range": recency})
        return []
    runs = await asyncio.gather(engines(), datasets.google_news(query, recency, num_results), return_exceptions=True)
    errors = [str(r) for r in runs if isinstance(r, Exception)]
    items, seen = [], set()
    for r in _interleave([run for run in runs if not isinstance(run, Exception)], num_results * 2):
        title = re.sub(r"\W+", "", r["title"].lower())  # Google News links are redirects, so match on title
        if title not in seen:
            seen.add(title)
            items.append(r)
    if not items:
        raise ToolError(f"No news found for {query!r} in the last {recency}.\n" + "\n".join(errors))
    items = items[:num_results]
    await datasets.resolve_google_news(items)
    out = []
    for r in items:
        date = r.get("published") or r.get("date") or ""
        line = " | ".join(x for x in (r.get("source", ""), date, _short(r.get("snippet", ""))) if x)
        out.append(f"{r['title']}\n  {r['url']}" + (f"\n  {line}" if line else ""))
    return "\n\n".join(out)


@mcp.tool(title="Image search", annotations=READ_ONLY, structured_output=False)
async def image_search(
    query: Annotated[str, Field(description="What the images should show.")],
    num_results: Annotated[int, Field(description="Number of images (1-50).", ge=1, le=50)] = 10,
) -> str:
    """Image results: page title, page URL and image URL."""
    try:
        raw = await _search_one("searxng", query, num_results, {"categories": "images"})
    except providers.ProviderError as e:
        raise ToolError(f"Image search failed (is the SearXNG container running on :8888?): {e}") from e
    out = [f"{r['title'] or query}\n  img: {r.get('img_src', r['url'])}\n"
           f"  thumb: {r.get('thumbnail') or r.get('img_src') or r['url']}\n  page: {r['url']}"
           for r in raw[:num_results]]
    return "\n\n".join(out) if out else "No image results."


@mcp.tool(title="Knowledge search (Wikipedia, HN, Stack Overflow, GitHub...)", annotations=READ_ONLY,
          structured_output=False)
async def knowledge_search(
    query: Annotated[str, Field(description="Topic, error message, library, model or concept.")],
    sites: Annotated[list[KnowledgeSource] | None, Field(description=(
        "Which to ask. wikipedia, hackernews (tech discussion), stackoverflow (dev Q&A), github (repos), "
        "openreview (ML conference papers + reviews), huggingface_papers, huggingface_models, lemmy "
        "(Reddit-like forums), packages (npm, crates.io, PyPI exact name), code (source code across GitHub, "
        "via grep.app), wiktionary (definitions), clinicaltrials, openfda (drug labels), courtlistener (US "
        "case law), patents (US, needs USPTO_ODP_API_KEY), history (pages already read through this "
        "server, searched offline). Empty = wikipedia, hackernews, stackoverflow, "
        "github, openreview, huggingface_papers."))] = None,
    num_results: Annotated[int, Field(description="Results per source (1-20).", ge=1, le=20)] = 5,
) -> str:
    """Search sources the web search engines index poorly, straight from their own APIs, in
    parallel. Each result has its URL, date and signals (stars, score, downloads, answers)."""
    return await knowledge.search(query, sites or knowledge.DEFAULT, num_results)


@mcp.tool(title="Live data: stocks, currency, crypto, weather, economy, places, SEC filings", annotations=READ_ONLY,
          structured_output=False)
async def live_data(
    kind: Annotated[Literal["stock", "currency", "crypto", "weather", "economy", "places", "sec_filings"],
                    Field(description="What to look up.")],
    query: Annotated[str, Field(description=(
        'stock: ticker or company ("RELIANCE.NS", "AAPL", "nvidia"; .NS = NSE, .BO = BSE). '
        'currency: "USD INR" or "100 EUR to USD". crypto: coin name or symbol. weather: a place name. '
        'economy: country + indicator ("India GDP growth", "US inflation", "world population"; gdp, gdp growth, '
        'inflation/cpi, unemployment, population, debt), or any US series name with FRED_API_KEY set. '
        'places: a place ("Kreuzberg, Berlin") or "<what> near <place>" ("cafe near Koramangala, Bangalore"). '
        'sec_filings: US ticker or company, optionally with a form: "AAPL 10-K", "tesla 8-K".'))],
) -> str:
    """Current numbers from keyless public APIs: stock quote with day and 52-week range (Yahoo
    Finance), exchange rates (ECB via Frankfurter), crypto price (CoinGecko), weather now and
    a 4-day forecast (Open-Meteo), yearly economic indicators (World Bank, FRED), places and
    what's around them (OpenStreetMap), and a US company's recent SEC EDGAR filings with
    headline financials (needs SEC_USER_AGENT in .env)."""
    try:
        if kind == "places":
            what, _, near = query.partition(" near ")
            return await datasets.places(what.strip(), near.strip())
        if kind == "sec_filings":
            m = re.match(r"^(.*?)\s+(10-K|10-Q|8-K|S-1|20-F|6-K|DEF 14A|13F-HR|4)$", query.strip(), re.IGNORECASE)
            company, form = (m.group(1), m.group(2).upper()) if m else (query.strip(), "")
            return await datasets.sec_filings(company, form)
        return await live.KINDS[kind](query)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"{kind} lookup failed for {query!r}: {e}") from e


@mcp.tool(title="Research paper search", annotations=READ_ONLY, structured_output=False)
async def paper_search(
    query: Annotated[str, Field(description="Topic, title or author, e.g. \"sparse autoencoders interpretability\".")],
    num_results: Annotated[int, Field(description="Number of papers (1-30).", ge=1, le=30)] = 10,
    year_from: Annotated[int, Field(description="Only papers from this year on; 0 = any year.", ge=0)] = 0,
    year_to: Annotated[int, Field(description="Only papers up to this year; 0 = any year.", ge=0)] = 0,
) -> str:
    """Search research papers across arXiv, Semantic Scholar, Google Scholar, PubMed, EuropePMC,
    OpenAIRE, Crossref and bioRxiv/medRxiv (plus CORE with CORE_API_KEY). Returns title,
    year, authors, venue, citations, DOI and PDF link.
    Read one with paper_fetch. For ML conference papers with reviews, also try knowledge_search
    with sites=["openreview"]."""
    try:
        results = await papers.search(query, num_results, ENV["SEARXNG_URL"], TIMEOUT, year_from, year_to)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Paper search failed (is the SearXNG container running?): {e}") from e
    return papers.format_results(results) if results else "No papers found."


@mcp.tool(title="Read a research paper", annotations=WRITES_FILES, structured_output=False)
async def paper_fetch(
    ref: Annotated[str, Field(description=(
        'Which paper: arXiv id ("1706.03762"), arXiv URL, DOI ("10.1038/nature14539"), '
        "doi.org URL, or a direct PDF URL."))],
    save_dir: Annotated[str, Field(description='Also save the PDF in this folder, e.g. "~/Downloads/papers". Empty = don\'t save.')] = "",
    max_chars: Annotated[int, Field(description="Characters of text to return per call.", ge=1000)] = 30000,
    start: Annotated[int, Field(description="Character offset to continue reading a long paper from.", ge=0)] = 0,
) -> str:
    """Read a research paper as plain text. DOIs resolve to a free copy: open access (OpenAlex,
    then Unpaywall), else Anna's Archive SciDB or LibGen. Long papers are paged: the output
    tells you the start= for the next part."""
    try:
        url, note = await papers.resolve(ref, TIMEOUT)
        if save_dir:
            page = await fetch.fetch(url, TIMEOUT)
            fetch.cache_put(url, page.text)
            via, text = page.via, page.text
            if fetch.is_pdf(page.content_type, page.body):
                path = _download_dir(save_dir) / (re.sub(r"[^\w.-]+", "_", ref.split("://")[-1])[:120] + ".pdf")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(page.body)
                note += f"; saved to {path}"
            else:
                note += "; not saved: response was a web page, not a PDF"
        else:
            via, text = await fetch.fetch_text(url, TIMEOUT)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Paper fetch failed for {ref}: {e}") from e
    return f"({note}; via {via}; {url})\n{fetch.window(text, start, max_chars)}"


def _need_llm(ctx: Context | None) -> None:
    if not (llm.llm_available() or llm.can_sample(ctx)):
        raise ToolError("extract needs an LLM and none is configured; use query= or read the page instead.")


async def _extract(text: str, what: str, ctx: Context | None) -> str | None:
    return await llm.ask(f"From the page below, extract: {what}\nReply with JSON only; use null for anything "
                         f"not on the page.\n\n{text}", max_tokens=4000, ctx=ctx)


@mcp.tool(title="Read a web page", annotations=READ_ONLY, structured_output=False)
async def fetch_page(
    url: Annotated[str, Field(description="Full URL of a web page or PDF.")],
    max_chars: Annotated[int, Field(description="Characters of text to return per call.", ge=500)] = 20000,
    start: Annotated[int, Field(description="Character offset to continue reading a long page from.", ge=0)] = 0,
    query: Annotated[str, Field(description=(
        "Return only the passages most relevant to this question (up to max_chars) instead of the "
        "page from start. Good for long pages."))] = "",
    extract: Annotated[str, Field(description=(
        'Have an LLM pull structured data out of the page as JSON, described in words, e.g. '
        '"product name, price, rating" or "every event: date, title, venue".'))] = "",
    max_age: Annotated[int | None, Field(description=(
        "Oldest cached copy to accept, in seconds; 0 = fetch live. Omit = up to 1 hour."), ge=0)] = None,
    method: Annotated[Literal[fetch.METHODS], Field(description=(
        "Force one way of fetching: plain (fast HTTP), browser (stealth browser), chrome (your Chrome), "
        "tor, archive (archive.today, then Wayback). auto tries them in turn."))] = "auto",
    as_of: Annotated[str, Field(description=(
        'Read the Wayback Machine copy closest to this date instead of the live page, e.g. "2019-06-01" '
        'or "2019". Empty = live page.'), pattern=r"^(\d{4}(-\d{2}(-\d{2})?)?)?$")] = "",
    ctx: Context | None = None,
) -> str:
    """Read a web page or PDF as clean markdown, with title/author/date when known.
    Reddit, YouTube, X/Twitter, Bluesky, Telegram and Instagram URLs return the post and
    comments, transcript or feed. Gets past most bot checks by escalating from a plain request
    to stealth browsers (a visible window may ask the user to tick a check once) and Tor;
    paywall stubs and dead pages fall back to archive.today, then the Wayback Machine.
    Long pages are paged: the output tells you the start= for the next part."""
    async def on_stage(stage: str):
        await _progress(ctx, 0, None, f"trying {stage}")
    if as_of:
        page = await fetch.archived_page(url, TIMEOUT, as_of.replace("-", ""))
        if not page:
            raise ToolError(f"No readable Wayback Machine copy of {url} near {as_of} (or archive.org didn't "
                            "answer; it is often slow, so retrying can help). page_history lists the copies.")
        return f"(via {page.via})\n{fetch.window(page.text, start, max_chars)}"
    try:
        via, text = await _read_url(url, max_age=max_age, on_stage=on_stage, method=method)
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Fetch failed for {url}\n{e}") from e
    if extract:
        _need_llm(ctx)
        data = await _extract(fetch.window(text, start, max_chars), extract, ctx)
        if not data:
            raise ToolError("The LLM gave no answer (models down or quota exhausted). Read the page instead.")
        return f"(via {via}; extracted by LLM)\n{data}"
    if query:
        return f"(via {via}; passages matching {query!r} out of {len(text)} chars)\n{await asyncio.to_thread(rerank.best_passages, text, query, max_chars)}"
    return f"(via {via})\n{fetch.window(text, start, max_chars)}"


@mcp.tool(title="Read several web pages", annotations=READ_ONLY, structured_output=False)
async def fetch_pages(
    urls: Annotated[list[str], Field(description="Up to 20 page URLs.", min_length=1, max_length=20)],
    max_chars: Annotated[int, Field(description="Characters of text to return per page.", ge=500)] = 6000,
    query: Annotated[str, Field(description="Return each page's passages most relevant to this instead of its start.")] = "",
    extract: Annotated[str, Field(description=(
        'Have an LLM pull the same fields out of every page as JSON, e.g. "name, price, rating".'))] = "",
    concurrency: Annotated[int, Field(description="How many to fetch at once.", ge=1, le=10)] = 5,
    ctx: Context | None = None,
) -> str:
    """Read several pages at once, one section per URL (same routing as fetch_page). Pages that
    fail show why. Never opens a visible browser window; each page gets at most 45 seconds."""
    if extract:
        _need_llm(ctx)
    sem, done = asyncio.Semaphore(concurrency), 0

    async def one(u: str) -> str:
        nonlocal done
        async with sem:
            try:
                via, text = await asyncio.wait_for(_read_url(u, interactive=False), fetch.FETCH_DEADLINE)
                body = (await asyncio.to_thread(rerank.best_passages, text, query, max_chars) if query
                        else fetch.window(text, 0, max_chars))
                if extract:
                    body = await _extract(body, extract, ctx) or "FAILED: the LLM gave no answer"
                result = f"(via {via})\n{body}"
            except TimeoutError:
                result = f"FAILED: no response within {fetch.FETCH_DEADLINE}s (try fetch_page on it alone)"
            except Exception as e:  # noqa: BLE001 - one bad page shouldn't sink the batch
                result = f"FAILED: {e}"
        done += 1
        await _progress(ctx, done, len(urls), u)
        return result
    results = await asyncio.gather(*(one(u) for u in urls))
    return "\n".join(f"===== {u} =====\n{r}" for u, r in zip(urls, results))


@mcp.tool(title="List a site's pages", annotations=READ_ONLY, structured_output=False)
async def site_map(
    url: Annotated[str, Field(description='A site or section, e.g. "docs.astral.sh/uv". A path limits the map to it.')],
    num_results: Annotated[int, Field(description="Most URLs to return.", ge=1, le=5000)] = 200,
    path_filter: Annotated[str, Field(description='Only URLs containing this text, e.g. "/blog/" or "guides".')] = "",
) -> str:
    """List the pages of a website from its published sitemaps (robots.txt, sitemap.xml,
    nested indexes), or the links on its front page (plus Common Crawl's index) when it has
    none. Use it to find the right pages, then read them with fetch_pages."""
    try:
        return await crawl.site_map(url, num_results, path_filter)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Could not map {url}: {e}") from e


@mcp.tool(title="YouTube transcript", annotations=READ_ONLY, structured_output=False)
async def youtube_transcript(
    url: Annotated[str, Field(description="YouTube video URL (watch, youtu.be, shorts or live).")],
    lang: Annotated[str, Field(description='Preferred caption language code, e.g. "en", "hi", "de".')] = "en",
    max_chars: Annotated[int, Field(description="Characters of transcript to return per call.", ge=1000)] = 30000,
    start: Annotated[int, Field(description="Character offset to continue a long transcript from.", ge=0)] = 0,
) -> str:
    """Get a YouTube video's transcript (captions, or auto-generated), with a [m:ss] mark about
    every 30 seconds. Long transcripts are paged: the output tells you the start= for the next part."""
    try:
        text = await sources.youtube_transcript(url, lang=lang)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Transcript unavailable: {e}") from e
    return fetch.window(text, start, max_chars)


@mcp.tool(title="Crawl a site", annotations=READ_ONLY, structured_output=False)
async def crawl_site(
    url: Annotated[str, Field(description='Where to start, e.g. "docs.astral.sh/uv". A path keeps the crawl inside it.')],
    query: Annotated[str, Field(description="Follow links about this first, and return the matching passages of each page.")] = "",
    num_pages: Annotated[int, Field(description="Most pages to read.", ge=1, le=100)] = 25,
    max_depth: Annotated[int, Field(description="How many links away from the start page to go.", ge=1, le=5)] = 2,
    path_filter: Annotated[str, Field(description='Only URLs containing this text, e.g. "/blog/".')] = "",
    max_chars_each: Annotated[int, Field(description="Characters of text per page.", ge=200, le=10000)] = 1500,
    ctx: Context | None = None,
) -> str:
    """Read a site by following its links (same site only), most relevant links first. Use it
    when site_map finds no sitemap, or to gather a topic spread across many pages of one site."""
    async def on_progress(done: int, total: int, page: str):
        await _progress(ctx, done, total, page)
    try:
        return await crawl.crawl(url, query, num_pages, max_depth, path_filter, max_chars_each, on_progress)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Could not crawl {url}: {e}") from e


@mcp.tool(title="Page history (Wayback Machine)", annotations=READ_ONLY, structured_output=False)
async def page_history(
    url: Annotated[str, Field(description="The page whose archived copies you want.")],
    num_results: Annotated[int, Field(description="Most snapshots to list.", ge=1, le=200)] = 20,
    year_from: Annotated[int, Field(description="Only snapshots from this year on; 0 = any.", ge=0)] = 0,
    year_to: Annotated[int, Field(description="Only snapshots up to this year; 0 = any.", ge=0)] = 0,
) -> str:
    """List a page's archived copies in the Wayback Machine, one per distinct version, with
    dates and replay links. Read an old version with fetch_page(url, as_of="2019-06-01")."""
    try:
        return await wayback.snapshots(url, num_results, year_from or None, year_to or None)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Wayback Machine lookup failed for {url} (archive.org is often slow; retry): {e}") from e


@mcp.tool(title="Read Reddit, X/Twitter, Bluesky, Telegram, Instagram", annotations=READ_ONLY,
          structured_output=False)
async def social_fetch(
    target: Annotated[str, Field(description=(
        'A post or profile URL (reddit.com, x.com, twitter.com, bsky.app, t.me, instagram.com), a subreddit '
        '("r/valheim"), or a handle: "@karpathy" is X, "@jay.bsky.team" is Bluesky.'))],
    num_results: Annotated[int, Field(description=(
        "Recent posts for a profile or subreddit, or top comments/replies on a post."), ge=1, le=100)] = 10,
    sort: Annotated[Literal["hot", "new", "top", "best"], Field(description="Order of a subreddit feed.")] = "hot",
) -> str:
    """Read a public post with its comments, or a profile/subreddit with its recent posts, without
    logging in. Reddit via the Arctic Shift archive and RSS; X via fxtwitter (then X's embed API); Bluesky's public API;
    Telegram's channel preview; Instagram's web API, which rate-limits often."""
    if re.match(r"^/?r/\w+/?$", target.strip()) or sources.classify(target) == "reddit":
        try:
            return await sources.reddit_fetch(target.strip(), sort=sort, limit=num_results)
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"Reddit fetch failed: {e}") from e
    try:
        return await social.read(target, min(num_results, 50))
    except ValueError as e:
        raise ToolError(str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Could not read {target}: {e}. For Instagram, try again in a few minutes "
                        "or fetch_page on the URL.") from e


@mcp.tool(title="Deep research report", annotations=READ_ONLY, structured_output=False)
async def deep_research(
    question: Annotated[str, Field(description="The question to research, in full, e.g. \"Is uv ready to replace poetry for "
                                               "a team monorepo in 2026?\"")],
    depth: Annotated[Literal["standard", "deep"], Field(description=(
        "standard: 2 rounds, up to 8 sources (~4 min). deep: 4 rounds, up to 16 sources (~8 min)."))] = "standard",
    sub_questions: Annotated[list[str] | None, Field(description=(
        "Your own search queries for the first round (up to 5), instead of letting the server plan them."))] = None,
    report: Annotated[bool, Field(description=(
        "false: return the sources with notes and best passages, and write the report yourself."))] = True,
    ctx: Context | None = None,
) -> str:
    """Research a question over several rounds: plan sub-queries, search, read the best pages,
    note what's still missing, search again, then write a report citing every claim as [n]
    with a Sources list. Slow; for a quick answer use web_search. Uses your own model through
    MCP sampling when the client allows it, else the configured LLMs; with no LLM at all it
    returns the best passages per source."""
    async def search(q: str, n: int) -> list[dict]:
        return (await _search(q, n, "fallback", None, None, "any", {}))[0]

    async def read(url: str) -> str:
        return (await _read_url(url, interactive=False))[1]

    async def ask(prompt: str, max_tokens: int) -> str | None:
        return await llm.ask(prompt, max_tokens, ctx=ctx)

    async def progress(done: float, total: float, message: str) -> None:
        await _progress(ctx, done, total, message)
    return await research.deep_research(question, search, read, ask, progress, depth=depth,
                                        sub_questions=sub_questions, report=report)


@mcp.tool(title="Find books, films, anime, games, music", annotations=READ_ONLY, structured_output=False)
async def media_search(
    query: Annotated[str, Field(description='Title, optionally with author/year, e.g. "dune frank herbert".')],
    category: Annotated[MediaCategory, Field(description=(
        "What kind of thing. manga includes manhwa/manhua; tv includes K-drama and episodes (\"show s01e02\"); "
        "subtitles finds subtitle files; torrents searches the general torrent indexes; all = every source."))] = "all",
    num_results: Annotated[int, Field(description="Results per source.", ge=1, le=30)] = 10,
    sites: Annotated[list[MediaSite] | None, Field(description="Only ask these sources. Empty = all sources for the category.")] = None,
) -> str:
    """Find books, comics, manga/manhwa, anime, movies, TV/K-drama, games, audiobooks, music,
    podcasts, software and subtitles, searching many sources in parallel. Returns WHAT IT IS
    (AniList, MangaUpdates, TVmaze, MyDramaList, iTunes: format, episodes, status) and WHERE TO
    GET IT: torrents with magnet link and seeders (download with media_download), books/comics
    with an md5 (download with book_download), direct download links, or download pages."""
    catalog, found, notes = await media.search(query, category, num_results, sites)
    if not catalog and not found:
        return f"Nothing found. sources: {', '.join(notes)}"
    return media.format_results(catalog, found, notes, num_results)


@mcp.tool(title="Watch for a release", annotations=WRITES_FILES, structured_output=False)
async def release_watch(
    action: Annotated[Literal["add", "remove", "list", "check"], Field(description=(
        "add/remove a title, list the watch list, or check every watch now."))],
    query: Annotated[str, Field(description='Title for add/remove, e.g. "spider-man brand new day 2026".')] = "",
    category: Annotated[Literal["movies", "tv", "anime"], Field(description="What kind of release.")] = "movies",
    min_quality: Annotated[Literal[tuple(quality.TIERS)], Field(description=(
        "Lowest acceptable quality; cinema recordings (CAM, TeleSync...) and suspicious files never count."))] = "WEB-DL",
) -> str:
    """Get a macOS notification when a title is released at a watchable quality (e.g. the WEB-DL
    of a film now in cinemas). The shared HTTP server checks every 6 hours; "check" runs it now."""
    if action in ("add", "remove") and not query.strip():
        raise ToolError(f"{action} needs a query (the title to watch).")
    if action == "add":
        return watch.add(query.strip(), category, min_quality)
    if action == "remove":
        return watch.remove(query.strip())
    if action == "list":
        return watch.list_watches()
    return "\n".join(await watch.check_all()) or "no watches"


@mcp.tool(title="Watch a page for changes", annotations=WRITES_FILES, structured_output=False)
async def page_watch(
    action: Annotated[Literal["check", "forget", "list"], Field(description=(
        "check: the first time, remember the page; after that, show what changed since last check. "
        "forget: stop tracking the URL. list: tracked pages."))],
    url: Annotated[str, Field(description="The page, e.g. a pricing, changelog or job listings page.")] = "",
    query: Annotated[str, Field(description=(
        'Only watch paragraphs mentioning these words, e.g. "price" or "python remote", so other '
        "churn on the page (ads, dates) doesn't count as a change."))] = "",
) -> str:
    """Track a web page and see what changed since you last looked, as a diff of added and
    removed lines."""
    if action == "list":
        return monitor.list_pages()
    if not url.strip():
        raise ToolError(f"{action} needs a url.")
    if action == "forget":
        return monitor.forget(url.strip())
    try:
        return await monitor.check(url.strip(), query)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Could not check {url}: {e}") from e


@mcp.tool(title="Download a book", annotations=WRITES_FILES, structured_output=False)
async def book_download(
    md5: Annotated[str, Field(description="The 32-character md5 shown by media_search.",
                              pattern=r"^\s*[0-9a-fA-F]{32}\s*$")],
    save_dir: Annotated[str, Field(description="Folder to save into.")] = "~/Downloads/books",
) -> str:
    """Download a book, comic or paper by md5 and save it with its original file name.
    Tries LibGen, then Z-Library (needs ZLIB_EMAIL/ZLIB_PASSWORD of a free account in .env)."""
    try:
        return await media.book_download(md5.strip().lower(), str(_download_dir(save_dir)))
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Download failed for {md5}: {e}") from e


@mcp.tool(title="Download video or audio (YouTube and more)", annotations=WRITES_FILES, structured_output=False)
async def media_download(
    url: Annotated[str, Field(description=(
        "A video, playlist or track page: YouTube, Vimeo, SoundCloud, Bandcamp, X, Instagram, TikTok, "
        "Twitch, archive.org and ~1800 other sites yt-dlp supports. Or a magnet link from media_search, "
        "downloaded as-is (the format options don't apply)."))],
    format: Annotated[Literal["mp4", "mkv", "webm", "mp3", "m4a", "opus", "flac", "wav"], Field(description=(
        "mp4/mkv/webm = video; mp3/m4a/opus/flac/wav = audio only. mp4 prefers H.264 so it plays everywhere."))] = "mp4",
    max_height: Annotated[int, Field(description="Highest video resolution, e.g. 720, 1080, 2160. 0 = best available.",
                                     ge=0, le=4320)] = 1080,
    save_dir: Annotated[str, Field(description="Folder to save into.")] = "~/Downloads/media",
    playlist: Annotated[bool, Field(description=(
        f"If the URL is a playlist or channel, download its items (at most {download.MAX_PLAYLIST_ITEMS}). "
        "Otherwise only the one video."))] = False,
    subtitles: Annotated[bool, Field(description="Embed English subtitles (video formats only).")] = False,
    ctx: Context | None = None,
) -> str:
    """Download a video or its audio with yt-dlp and ffmpeg, e.g. a YouTube video as mp4 or a
    song as mp3, or a torrent's files from a magnet link with aria2c (no seeding afterwards).
    Skips files already saved. Returns the saved file paths and sizes."""
    folder = _download_dir(save_dir)

    async def on_progress(line: str):
        await _progress(ctx, 0, None, line)
    try:
        if url.startswith("magnet:"):
            return await torrent.download(url, folder, on_progress)
        return await download.download(url, format, max_height, folder, playlist, subtitles, on_progress)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Download failed for {url}: {e}") from e


@mcp.tool(title="Server status", annotations=READ_ONLY, structured_output=False)
async def server_status(
    check_new_sources: Annotated[bool, Field(description=(
        "Also check Prowlarr's ~550 torrent indexer definitions and FMHY's starred picks for sites added, "
        "removed or moved since the last check (the first check records a baseline)."))] = False,
) -> str:
    """This month's usage vs limits per search provider, LLM call counts, which domain currently
    works for each mirrored site, and sources being skipped because they keep failing."""
    out = quota.status_table(CONFIG["search_providers"])
    llm_used = quota.llm_usage()
    if llm_used:
        out += "\n\nLLM calls this month (coding-plan models):\n" + "\n".join(
            f"  {k}: {v}" for k, v in sorted(llm_used.items()))
    mirror_state = mirrors.status()
    if mirror_state:
        out += "\n\nMirrors (working domain first):\n" + mirror_state
    failing = health.report()
    if failing:
        out += "\n\nSources skipped for now (failed repeatedly):\n" + "\n".join(f"  {f}" for f in failing)
    if check_new_sources:
        try:
            out += "\n\n" + await discover.discover()
        except Exception as e:  # noqa: BLE001
            out += f"\n\nSource discovery failed: {e}"
    return out


@mcp.prompt(title="Literature review")
def literature_review(topic: str, years: str = "the last 5 years") -> str:
    """Survey the research on a topic: key papers, methods, open problems."""
    return (f"Write a literature review of {topic!r}, focused on {years}. Use paper_search (several phrasings), "
            "knowledge_search with sites=['openreview', 'huggingface_papers'] for recent ML work, and paper_fetch "
            "to read the 3-5 most cited or most relevant papers. Group the work by approach, say what each found, "
            "note disagreements and open problems, and cite every claim with its paper and link.")


@mcp.prompt(title="Company dossier")
def company_dossier(company: str) -> str:
    """Brief on a company: what it does, numbers, news, filings, people's opinions."""
    return (f"Build a dossier on {company}. Use web_search for what it does and who runs it, live_data kind='stock' "
            "for its quote if listed, news_search (recency='month') for recent events, SEC filings via live_data "
            "if it's a US filer, and social_fetch / knowledge_search (hackernews) for what people say. Sections: "
            "overview, numbers, recent news, risks, sentiment. Cite a URL for every fact and date the numbers.")


@mcp.prompt(title="Compare options")
def compare_options(options: str, criteria: str = "") -> str:
    """Compare products, tools or choices side by side with sources."""
    wanted = f" on: {criteria}" if criteria else " on the criteria that matter most for this kind of choice"
    return (f"Compare {options}{wanted}. For each option run web_search with depth='advanced', and read Reddit "
            "or Hacker News threads (social_fetch, knowledge_search) for real users' experience. Give a table, "
            "then a recommendation with the reasons and the trade-offs. Cite sources and flag anything uncertain.")


def _slim(schema: dict) -> dict:
    """Pydantic adds a title to every property and wraps optionals in anyOf [..., null]: about
    10% of the tool list, telling an agent nothing. Arguments are still validated by the model."""
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
        variants = [v for v in prop.get("anyOf", []) if v != {"type": "null"}]
        if len(variants) == 1:
            del prop["anyOf"]
            prop.update(variants[0])
        if "default" in prop and prop["default"] is None:
            del prop["default"]
    return schema


for _tool in mcp._tool_manager.list_tools():  # the SDK has no public hook for this
    _slim(_tool.parameters)

if __name__ == "__main__":
    if os.environ.get("MCP_TRANSPORT", "stdio") == "http":
        mcp.run(transport="streamable-http",
                host=os.environ.get("MCP_HOST", "127.0.0.1"),
                port=int(os.environ.get("MCP_PORT", "8765")),
                json_response=True)
    else:
        mcp.run()
