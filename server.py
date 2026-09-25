"""web-search MCP server — self-owned search/fetch stack (MCP SDK v2).

Tools: web_search, news_search, suggest, image_search, knowledge_search, paper_search,
paper_fetch, fetch_page, fetch_pages, site_map, reddit_fetch, youtube_transcript,
social_fetch, live_data, media_search, book_download, discover_sources, usage_status.
Run: uv run server.py (stdio) | MCP_TRANSPORT=http uv run server.py (shared HTTP instance)
"""
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx
from dotenv import load_dotenv
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

import crawl
import discover
import fetch
import knowledge
import live
import llm
import media
import mirrors
import papers
import providers
import quota
import social
import sources

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
CONFIG = json.loads((ROOT / "config.json").read_text())
ENV = {k: os.environ.get(k, "") for k in [
    "SEARXNG_URL", "TAVILY_API_KEY", "EXA_API_KEY", "FIRECRAWL_API_KEY", "JINA_API_KEY"]}

INSTRUCTIONS = """Local, keyless web research tools. Which to use:
- a question or topic -> web_search (more_queries for several angles, depth="advanced" to read the top pages)
- something that happened recently -> news_search
- facts, code, dev Q&A, ML models/papers, packages -> knowledge_search (Wikipedia, HN, Stack Overflow, GitHub...)
- a stock price, exchange rate, crypto price or weather -> live_data
- a specific URL -> fetch_page (several: fetch_pages; all pages of a site: site_map first)
- Reddit -> reddit_fetch; YouTube -> youtube_transcript; X/Twitter, Bluesky, Telegram, Instagram -> social_fetch
- research papers -> paper_search, then paper_fetch to read one
- a book, comic, manga, anime, film, show, game, audiobook, music, podcast or subtitles -> media_search;
  book_download saves a book by md5
Long outputs are paged: pass start= as the output says. Failed calls return an error saying why."""

mcp = MCPServer("web-search", title="Web search & research", instructions=INSTRUCTIONS)
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
        from datetime import date, timedelta
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


async def _auto_classify(query: str) -> dict:
    """LLM picks strategy/news/recency. Invalid or missing fields are dropped."""
    r = await llm.ask(
        'Classify this web-search query. Reply with JSON only: {"strategy":"fallback|merge|exhaustive",'
        f'"news":true|false,"recency":"|day|week|month|year"}}. Query: {query}', max_tokens=1000)
    m = re.search(r"\{.*\}", r or "", re.S)
    try:
        spec = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}
    return {
        "strategy": spec.get("strategy") if spec.get("strategy") in ("fallback", "merge", "exhaustive") else None,
        "news": spec.get("news") is True,
        "recency": spec.get("recency") if spec.get("recency") in _RECENCY_DAYS else None,
    }


async def _enrich(query, items, want_answer, want_highlights) -> str:
    """LLM answer + highlights; silently skipped if coding-plan models are down/exhausted.
    Token budgets are generous because these are reasoning models: thinking eats the budget first."""
    if not ((want_answer or want_highlights) and items and llm.llm_available()):
        return ""
    ctx = "\n".join(f"[{i + 1}] {r['title']} | {r['url']} | {r.get('content') or r.get('snippet', '')[:220]}"
                    for i, r in enumerate(items[:12]))
    answer_prompt = f"Answer using ONLY the numbered results; cite as [n]. Query: {query}\n\n{ctx}"
    highlights_prompt = f"5 key facts with [n] citations. Query: {query}\n\n{ctx}"
    answer, highlights = await asyncio.gather(  # llm.ask("") returns None without a call
        llm.ask(answer_prompt if want_answer else "", max_tokens=2000),
        llm.ask(highlights_prompt if want_highlights else "", max_tokens=1500))
    out = ""
    if answer:
        out += f"ANSWER (LLM-synthesized, [n] = source):\n{answer}\n\n"
    if highlights:
        out += f"HIGHLIGHTS:\n{highlights}\n\n"
    return out


def _interleave(runs: list[list[dict]], limit: int) -> list[dict]:
    """Each run's best, then each one's second best, ... deduped, so every run is represented."""
    out, seen = [], set()
    for rank in range(max((len(r) for r in runs), default=0)):
        for run in runs:
            if rank < len(run) and _url_key(run[rank]["url"]) not in seen:
                seen.add(_url_key(run[rank]["url"]))
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


def _best_passages(text: str, query: str, limit: int = 1500) -> str:
    """The paragraphs sharing the most words with the query, kept in document order."""
    text = re.sub(r"(?s)\A---\n.*?\n---\n", "", text)  # trafilatura's metadata header
    words = {w for w in re.findall(r"\w+", query.lower()) if len(w) > 2}
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if len(p.strip()) > 40]
    scored = sorted(range(len(paragraphs)), reverse=True,
                    key=lambda i: len(words & set(re.findall(r"\w+", paragraphs[i].lower()))))
    picked, total = [], 0
    for i in scored:
        if total + len(paragraphs[i]) > limit and picked:
            break
        picked.append(i)
        total += len(paragraphs[i])
    return "\n\n".join(paragraphs[i] for i in sorted(picked))[:limit]


async def _read_url(url: str, interactive: bool = True, fresh: bool = False, on_stage=None) -> tuple[str, str]:
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
    via, text = await fetch.fetch_text(url, TIMEOUT, fresh=fresh, interactive=interactive, on_stage=on_stage)
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
            r["content"] = _best_passages(text, query)
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
    language: Annotated[str, Field(description='Result language code, e.g. "en", "de", "hi" (local SearXNG only). '
                                               'Empty = any.')] = "",
    safesearch: Annotated[Literal["off", "moderate", "strict"], Field(description=(
        "Adult-content filter (local SearXNG only)."))] = "off",
    answer: Annotated[bool, Field(description="Add an LLM-written answer citing results as [n].")] = False,
    highlights: Annotated[bool, Field(description="Add LLM-extracted key facts as bullets.")] = False,
    auto: Annotated[bool, Field(description="Let an LLM pick strategy, recency and news routing for you.")] = False,
    ctx: Context | None = None,
) -> str:
    """Search the web. Returns title, URL, date (when known) and snippet per result, tagged with
    the provider that found it. For recent events use news_search; for papers paper_search; for
    books, films, anime, games media_search. LLM options fall back to plain results if no model answers."""
    queries = [query] + [q for q in (more_queries or []) if q.strip()][:9]
    if auto and llm.llm_available():
        spec = await _auto_classify(query)
        strategy = spec.get("strategy") or strategy
        recency = spec.get("recency") or recency
        # a news search can't honour the other options, so only route there when none are set
        plain = (len(queries) == 1 and not include_domains and not exclude_domains and not filetype
                 and page == 1 and not language and depth == "basic")
        if spec.get("news") and plain:
            return await news_search(query, num_results, recency if recency in _RECENCY_DAYS else "week")
    if filetype:
        queries = [f"{q} filetype:{filetype.strip('. ').lower()}" for q in queries]
    extra = {k: v for k, v in (("pageno", page if page > 1 else None), ("language", language or None),
                               ("safesearch", _SAFESEARCH[safesearch] if safesearch != "off" else None)) if v}
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
    out = await _enrich(query, items, answer, highlights)
    for r in items:
        tag = f"[{r['via']}]" + (f" (q: {r['query']})" if len(queries) > 1 else "")
        date = f"{r['published']} | " if r.get("published") else ""
        out += f"{tag} {r['title']}\n  {r['url']}\n  {date}{r.get('snippet', '')}\n"
        if r.get("content"):
            out += "  --- page passages ---\n  " + r["content"].replace("\n", "\n  ") + "\n"
        elif r.get("fetch_error"):
            out += f"  (page not read: {r['fetch_error']})\n"
    return out


@mcp.tool(title="News search", annotations=READ_ONLY, structured_output=False)
async def news_search(
    query: Annotated[str, Field(description="Topic or event to find news about.")],
    num_results: Annotated[int, Field(description="Number of articles (1-20).", ge=1, le=20)] = 5,
    recency: Annotated[Literal["day", "week", "month", "year"], Field(description="How far back to look.")] = "day",
) -> str:
    """Recent news articles with source and date (SearXNG news, then Tavily)."""
    available = {p["name"] for p in _available_search_providers()}
    errors = []
    for name in ("searxng", "tavily"):
        if name not in available:
            continue
        opts = ({"categories": "news", "time_range": recency} if name == "searxng"
                else {"topic": "news", "time_range": recency})
        try:
            results = await _search_one(name, query, num_results, opts)
        except providers.ProviderError as e:
            errors.append(str(e))
            continue
        if results:
            out = [f"{r['title']}\n  {r['url']}\n  " + (f"{r['published']} | " if r.get("published") else "")
                   + r.get("snippet", "") for r in results[:num_results]]
            return f"(via {name} news)\n" + "\n\n".join(out)
        errors.append(f"{name}: 0 results")
    raise ToolError(f"No news found for {query!r} in the last {recency}.\n" + "\n".join(errors))


@mcp.tool(title="Search suggestions", annotations=READ_ONLY, structured_output=False)
async def suggest(query: Annotated[str, Field(description="A partial query, e.g. \"how to learn rus\".")]) -> str:
    """Autocomplete suggestions for a partial query: what people commonly search for. Use it to
    discover better phrasings before searching."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://duckduckgo.com/ac/", params={"q": query, "type": "list"},
                            headers={"User-Agent": providers.UA})
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Suggestions unavailable: {e}") from e
    items = data[1] if len(data) > 1 and isinstance(data[1], list) else data
    return "\n".join(str(s) for s in items[:10]) or "No suggestions."


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
        "(Reddit-like forums), packages (npm, crates.io, PyPI exact name). Empty = wikipedia, hackernews, "
        "stackoverflow, github, openreview, huggingface_papers."))] = None,
    num_results: Annotated[int, Field(description="Results per source (1-20).", ge=1, le=20)] = 5,
) -> str:
    """Search sources the web search engines index poorly, straight from their own APIs, in
    parallel. Each result has its URL, date and signals (stars, score, downloads, answers)."""
    return await knowledge.search(query, sites or knowledge.DEFAULT, num_results)


@mcp.tool(title="Live data: stocks, currency, crypto, weather", annotations=READ_ONLY, structured_output=False)
async def live_data(
    kind: Annotated[Literal["stock", "currency", "crypto", "weather"], Field(description="What to look up.")],
    query: Annotated[str, Field(description=(
        'stock: ticker or company ("RELIANCE.NS", "AAPL", "nvidia"; .NS = NSE, .BO = BSE). '
        'currency: "USD INR" or "100 EUR to USD". crypto: coin name or symbol. weather: a place name.'))],
) -> str:
    """Current numbers from keyless public APIs: stock quote with day and 52-week range (Yahoo
    Finance), exchange rates (ECB via Frankfurter), crypto price (CoinGecko), weather now and
    a 4-day forecast (Open-Meteo)."""
    try:
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
    """Search research papers across arXiv, Semantic Scholar, Google Scholar, PubMed,
    EuropePMC and OpenAIRE. Returns title, year, authors, venue, citations, DOI and PDF link.
    Read one with paper_fetch. For ML conference papers with reviews, also try knowledge_search
    with sources=["openreview"]."""
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
    """Read a research paper as plain text. DOIs resolve to a free open-access copy (OpenAlex,
    then Unpaywall). Long papers are paged: the output tells you the start= for the next part."""
    try:
        url, note = await papers.resolve(ref, TIMEOUT)
        if save_dir:
            page = await fetch.fetch(url, TIMEOUT)
            fetch.cache_put(url, page.text)
            via, text = page.via, page.text
            if fetch.is_pdf(page.content_type, page.body):
                path = Path(save_dir).expanduser() / (re.sub(r"[^\w.-]+", "_", ref.split("://")[-1])[:120] + ".pdf")
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
    fresh: Annotated[bool, Field(description="Ignore the 1-hour cache and fetch again.")] = False,
    ctx: Context | None = None,
) -> str:
    """Read a web page or PDF as clean markdown, with title/author/date when known.
    Reddit, YouTube, X/Twitter, Bluesky, Telegram and Instagram URLs return the post and
    comments, transcript or feed. Gets past most bot checks: Chrome-fingerprinted request ->
    stealth browser -> your logged-in Chrome (if configured) -> a visible browser window (you
    may be asked to tick a check once) -> Jina reader; sites your ISP blocks are retried
    through Tor. Long pages are paged: the output tells you the start= for the next part."""
    async def on_stage(stage: str):
        await _progress(ctx, 0, None, f"trying {stage}")
    try:
        via, text = await _read_url(url, fresh=fresh, on_stage=on_stage)
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Fetch failed for {url}\n{e}") from e
    if extract:
        if not llm.llm_available():
            raise ToolError("extract needs an LLM and none is configured; use query= or read the page instead.")
        data = await llm.ask(f"From the page below, extract: {extract}\nReply with JSON only; use null for "
                             f"anything not on the page.\n\n{fetch.window(text, start, max_chars)}", max_tokens=4000)
        if not data:
            raise ToolError("The LLM gave no answer (models down or quota exhausted). Read the page instead.")
        return f"(via {via}; extracted by LLM)\n{data}"
    if query:
        return f"(via {via}; passages matching {query!r} out of {len(text)} chars)\n{_best_passages(text, query, max_chars)}"
    return f"(via {via})\n{fetch.window(text, start, max_chars)}"


@mcp.tool(title="Read several web pages", annotations=READ_ONLY, structured_output=False)
async def fetch_pages(
    urls: Annotated[list[str], Field(description="Up to 20 page URLs.", min_length=1, max_length=20)],
    max_chars: Annotated[int, Field(description="Characters of text to return per page.", ge=500)] = 6000,
    query: Annotated[str, Field(description="Return each page's passages most relevant to this instead of its start.")] = "",
    concurrency: Annotated[int, Field(description="How many to fetch at once.", ge=1, le=10)] = 5,
    ctx: Context | None = None,
) -> str:
    """Read several pages at once, one section per URL (same routing as fetch_page). Pages that
    fail show why. Never opens a visible browser window; each page gets at most 45 seconds."""
    sem, done = asyncio.Semaphore(concurrency), 0

    async def one(u: str) -> str:
        nonlocal done
        async with sem:
            try:
                via, text = await asyncio.wait_for(_read_url(u, interactive=False), fetch.FETCH_DEADLINE)
                body = _best_passages(text, query, max_chars) if query else fetch.window(text, 0, max_chars)
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
    nested indexes), or the links on its front page when it has none. Use it to find the
    right pages, then read them with fetch_pages."""
    try:
        return await crawl.site_map(url, num_results, path_filter)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Could not map {url}: {e}") from e


@mcp.tool(title="Read Reddit", annotations=READ_ONLY, structured_output=False)
async def reddit_fetch(
    target: Annotated[str, Field(description="A post URL, or a subreddit: \"r/valheim\" or \"valheim\".")],
    sort: Annotated[Literal["hot", "new", "top", "best"], Field(description="Order for a subreddit feed.")] = "hot",
    num_results: Annotated[int, Field(description="Posts in a feed, or top comments on a post.", ge=1, le=100)] = 15,
) -> str:
    """Read a Reddit post with its body and top comments, or a subreddit's post list."""
    try:
        return await sources.reddit_fetch(target, sort=sort, limit=num_results)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Reddit fetch failed: {e}") from e


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


@mcp.tool(title="Read X/Twitter, Bluesky, Telegram, Instagram", annotations=READ_ONLY, structured_output=False)
async def social_fetch(
    target: Annotated[str, Field(description=(
        'A post or profile URL (x.com, twitter.com, bsky.app, t.me, instagram.com), or a handle: '
        '"@karpathy" is X, "@jay.bsky.team" is Bluesky.'))],
    num_results: Annotated[int, Field(description="Recent posts for a profile, or replies for a Bluesky post.",
                                      ge=1, le=50)] = 10,
) -> str:
    """Read a public post, or a profile with its recent posts, without logging in. X via
    fxtwitter (then X's embed API); Bluesky's public API; Telegram's channel preview;
    Instagram's web API, which rate-limits often."""
    try:
        return await social.read(target, num_results)
    except ValueError as e:
        raise ToolError(str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Could not read {target}: {e}. For Instagram, try again in a few minutes "
                        "or fetch_page on the URL.") from e


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
    GET IT: torrents with magnet link and seeders, books/comics with an md5 (download with
    book_download), direct download links, or download pages."""
    catalog, found, notes = await media.search(query, category, num_results, sites)
    if not catalog and not found:
        return f"Nothing found. sources: {', '.join(notes)}"
    return media.format_results(catalog, found, notes, num_results)


@mcp.tool(title="Download a book", annotations=WRITES_FILES, structured_output=False)
async def book_download(
    md5: Annotated[str, Field(description="The 32-character md5 shown by media_search.",
                              pattern=r"^\s*[0-9a-fA-F]{32}\s*$")],
    save_dir: Annotated[str, Field(description="Folder to save into.")] = "~/Downloads/books",
) -> str:
    """Download a book, comic or paper by md5 and save it with its original file name.
    Tries LibGen, then Z-Library (needs ZLIB_EMAIL/ZLIB_PASSWORD of a free account in .env)."""
    try:
        return await media.book_download(md5.strip().lower(), save_dir)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Download failed for {md5}: {e}") from e


@mcp.tool(title="Discover new sources", annotations=READ_ONLY, structured_output=False)
async def discover_sources() -> str:
    """Check two community-maintained lists for new or moved sites since the last check:
    Prowlarr's ~550 torrent indexer definitions and FMHY's starred picks (books, audio, video,
    games, torrents, AI...). Reports additions, removals and domain changes. The first run
    records a baseline."""
    try:
        return await discover.discover()
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Discovery failed: {e}") from e


@mcp.tool(title="Usage and mirror status", annotations=READ_ONLY, structured_output=False)
def usage_status() -> str:
    """This month's usage vs limits per search provider, LLM call counts, and which domain
    currently works for each mirrored site."""
    out = quota.status_table(CONFIG["search_providers"])
    llm_used = quota.llm_usage()
    if llm_used:
        out += "\n\nLLM calls this month (coding-plan models):\n" + "\n".join(
            f"  {k}: {v}" for k, v in sorted(llm_used.items()))
    mirror_state = mirrors.status()
    if mirror_state:
        out += "\n\nMirrors (working domain first):\n" + mirror_state
    return out


if __name__ == "__main__":
    if os.environ.get("MCP_TRANSPORT", "stdio") == "http":
        mcp.run(transport="streamable-http",
                host=os.environ.get("MCP_HOST", "127.0.0.1"),
                port=int(os.environ.get("MCP_PORT", "8765")),
                json_response=True)
    else:
        mcp.run()
