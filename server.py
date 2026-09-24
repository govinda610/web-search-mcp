"""web-search MCP server — self-owned search/fetch stack (MCP SDK v2).

Tools: web_search (fallback/merge/exhaustive, LLM answer/highlights/auto), news_search,
suggest, image_search, paper_search, paper_fetch, fetch_page (smart router), fetch_pages,
reddit_fetch, youtube_transcript, media_search, book_download, usage_status.
Run: uv run server.py (stdio) | MCP_TRANSPORT=http uv run server.py (shared HTTP instance)
"""
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

import fetch
import llm
import media
import mirrors
import papers
import providers
import quota
import sources

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
CONFIG = json.loads((ROOT / "config.json").read_text())
ENV = {k: os.environ.get(k, "") for k in [
    "SEARXNG_URL", "TAVILY_API_KEY", "EXA_API_KEY", "FIRECRAWL_API_KEY", "JINA_API_KEY"]}

INSTRUCTIONS = """Local, keyless web research tools. Which to use:
- a question or topic -> web_search (add more_queries for several angles, depth="advanced" to read the top pages)
- something that happened recently -> news_search
- a specific URL -> fetch_page (several: fetch_pages); Reddit -> reddit_fetch; YouTube -> youtube_transcript
- research papers -> paper_search, then paper_fetch to read one
- a book, comic, manga, anime, film, show or game -> media_search; book_download to save a book
Long outputs are paged: pass start= as the output says. Failures return a plain message saying why."""

mcp = MCPServer("web-search", title="Web search & research", instructions=INSTRUCTIONS)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITES_FILES = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
MediaSite = Literal[tuple(f.__name__ for f in media.SOURCES["all"])]
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


def _domain_list(domains: str) -> list[str]:
    return [d.strip().lower().removeprefix("www.") for d in domains.split(",") if d.strip()]


def _domain_ok(url: str, include: str, exclude: str) -> bool:
    """include/exclude match a host or any of its subdomains: 'reddit.com' matches old.reddit.com."""
    host = (urlparse(url).hostname or "").lower()

    def matches(domain: str) -> bool:
        return host == domain or host.endswith("." + domain)
    inc, exc = _domain_list(include), _domain_list(exclude)
    if inc and not any(matches(d) for d in inc):
        return False
    return not any(matches(d) for d in exc)


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
    ctx = "\n".join(f"[{i + 1}] {r['title']} | {r['url']} | {r.get('content') or r['snippet'][:220]}"
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


async def _search(query, num_results, strategy, include_domains, exclude_domains, recency):
    """One query through the providers. Returns (items, errors)."""
    avail = _available_search_providers()
    if recency in _RECENCY_DAYS:
        avail = [p for p in avail if p["name"] in _RECENCY_PROVIDERS]
    if not avail:
        return [], ["No search providers available (quota exhausted or none enabled)."]

    # Tell the engines which sites we want; filtering afterwards alone often leaves nothing.
    sites = _domain_list(include_domains)
    if sites:
        query = f"{query} " + " OR ".join(f"site:{d}" for d in sites)

    items, errors = [], []
    if strategy in ("merge", "exhaustive"):
        names = [p["name"] for p in avail
                 if strategy == "exhaustive" or p["name"] in CONFIG["merge_providers"]]
        results = await asyncio.gather(
            *[_search_one(n, query, num_results, _recency_opts(n, recency)) for n in names],
            return_exceptions=True)
        seen, per_domain = set(), {}
        for name, res in zip(names, results):
            if isinstance(res, Exception):
                errors.append(str(res))
                continue
            for r in res:
                key = _url_key(r["url"])
                dom = urlparse(r["url"]).netloc
                # the per-domain cap is for diversity; skip it when the caller asked for specific domains
                if key in seen or (per_domain.get(dom, 0) >= 2 and not include_domains) \
                        or not _domain_ok(r["url"], include_domains, exclude_domains):
                    continue
                seen.add(key)
                per_domain[dom] = per_domain.get(dom, 0) + 1
                items.append({**r, "via": name})
    else:
        for p in avail:
            try:
                raw = await _search_one(p["name"], query, num_results,
                                        _recency_opts(p["name"], recency))
            except providers.ProviderError as e:
                errors.append(str(e))
                continue
            items = [{**r, "via": p["name"]} for r in raw
                     if _domain_ok(r["url"], include_domains, exclude_domains)]
            if items:
                break
            errors.append(f"{p['name']}: 0 results after filtering")
    return items[:num_results], errors


def _url_key(url: str) -> str:
    return re.sub(r"[?#].*$", "", url.rstrip("/")).lower()


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


async def _add_page_content(query: str, items: list[dict], top: int = 5) -> None:
    """depth=advanced: fetch the top pages in parallel and attach their most relevant passages."""
    async def one(r):
        try:
            kind = sources.classify(r["url"])
            if kind == "reddit":  # never scrape reddit directly: bans the IP
                text = await sources.reddit_fetch(r["url"])
            elif kind == "youtube":
                text = await sources.youtube_transcript(r["url"])
            else:
                _, text = await fetch.fetch_text(r["url"], TIMEOUT)
            r["content"] = _best_passages(text, query)
        except Exception as e:  # noqa: BLE001 - a failed page keeps its search snippet
            r["content"] = ""
            r["fetch_error"] = str(e)[:120]
    await asyncio.gather(*(one(r) for r in items[:top]))


@mcp.tool(title="Web search", annotations=READ_ONLY)
async def web_search(
    query: Annotated[str, Field(description="What to search for, as you'd type it into a search engine.")],
    num_results: Annotated[int, Field(description="Results per query (1-20).", ge=1, le=20)] = 8,
    strategy: Annotated[Literal["fallback", "merge", "exhaustive"], Field(description=(
        "fallback: first provider that answers, local SearXNG first (fast, free). "
        "merge: a few providers in parallel. exhaustive: every provider in parallel, deduped."))] = "fallback",
    include_domains: Annotated[str, Field(description=(
        'Only these sites, comma-separated; subdomains count. e.g. "reddit.com,arxiv.org"'))] = "",
    exclude_domains: Annotated[str, Field(description='Never these sites, comma-separated. e.g. "pinterest.com"')] = "",
    recency: Annotated[Literal["any", "day", "week", "month", "year"], Field(description=(
        "Only results published within this window."))] = "any",
    depth: Annotated[Literal["basic", "advanced"], Field(description=(
        "basic: titles + snippets. advanced: also reads the top 5 pages and adds their most "
        "relevant passages (slower, much more content)."))] = "basic",
    more_queries: Annotated[list[str] | None, Field(description=(
        "Up to 9 extra phrasings or sub-questions, searched in parallel with query and merged."))] = None,
    answer: Annotated[bool, Field(description="Add an LLM-written answer citing results as [n].")] = False,
    highlights: Annotated[bool, Field(description="Add LLM-extracted key facts as bullets.")] = False,
    auto: Annotated[bool, Field(description="Let an LLM pick strategy, recency and news routing for you.")] = False,
) -> str:
    """Search the web. Returns title, URL and snippet per result, tagged with the provider
    that found it. For recent events use news_search; for papers paper_search; for books,
    films, anime, games media_search. LLM options fall back to plain results if no model answers."""
    if auto and llm.llm_available():
        spec = await _auto_classify(query)
        strategy = spec.get("strategy") or strategy
        recency = spec.get("recency") or recency
        if spec.get("news"):
            return await news_search(query, num_results, recency if recency in _RECENCY_DAYS else "week")
    queries = [query] + [q for q in (more_queries or []) if q.strip()][:9]
    runs = await asyncio.gather(*(_search(q, num_results, strategy, include_domains,
                                          exclude_domains, recency) for q in queries))
    # Interleave so every query's best hits make the cut, not just the first query's.
    items, errors, seen = [], [], set()
    for rank in range(num_results):
        for q, (found, _) in zip(queries, runs):
            if rank < len(found) and _url_key(found[rank]["url"]) not in seen:
                seen.add(_url_key(found[rank]["url"]))
                items.append({**found[rank], "query": q})
    for q, (_, errs) in zip(queries, runs):
        errors += [f"{q}: {e}" if len(queries) > 1 else e for e in errs]
    if not items:
        return f"All providers failed for: {query}\n" + "\n".join(errors)
    items = items[:num_results * min(len(queries), 3)]
    if depth == "advanced":
        await _add_page_content(query, items)
    out = await _enrich(query, items, answer, highlights)
    for r in items:
        tag = f"[{r['via']}]" + (f" (q: {r['query']})" if len(queries) > 1 else "")
        out += f"{tag} {r['title']}\n  {r['url']}\n  {r['snippet']}\n"
        if r.get("content"):
            out += "  --- page passages ---\n  " + r["content"].replace("\n", "\n  ") + "\n"
    return out


@mcp.tool(title="News search", annotations=READ_ONLY)
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
            out = [f"{r['title']}\n  {r['url']}\n  {r['snippet']}" for r in results[:num_results]]
            return f"(via {name} news)\n" + "\n\n".join(out)
        errors.append(f"{name}: 0 results")
    return "No news found.\n" + "\n".join(errors)


@mcp.tool(title="Search suggestions", annotations=READ_ONLY)
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
        return f"Suggestions unavailable: {e}"
    items = data[1] if len(data) > 1 and isinstance(data[1], list) else data
    return "\n".join(str(s) for s in items[:10]) or "No suggestions."


@mcp.tool(title="Image search", annotations=READ_ONLY)
async def image_search(
    query: Annotated[str, Field(description="What the images should show.")],
    num_results: Annotated[int, Field(description="Number of images (1-50).", ge=1, le=50)] = 10,
) -> str:
    """Image results: page title, page URL and image URL."""
    try:
        raw = await _search_one("searxng", query, num_results, {"categories": "images"})
    except providers.ProviderError as e:
        return f"Image search failed (is the SearXNG container running on :8888?): {e}"
    out = [f"{r['title'] or query}\n  img: {r.get('img_src', r['url'])}\n"
           f"  thumb: {r.get('thumbnail') or r.get('img_src') or r['url']}\n  page: {r['url']}"
           for r in raw[:num_results]]
    return "\n\n".join(out) if out else "No image results."


@mcp.tool(title="Research paper search", annotations=READ_ONLY)
async def paper_search(
    query: Annotated[str, Field(description="Topic, title or author, e.g. \"sparse autoencoders interpretability\".")],
    num_results: Annotated[int, Field(description="Number of papers (1-30).", ge=1, le=30)] = 10,
    year_from: Annotated[int, Field(description="Only papers from this year on; 0 = any year.")] = 0,
) -> str:
    """Search research papers across arXiv, Semantic Scholar, Google Scholar, PubMed,
    EuropePMC and OpenAIRE. Returns title, year, authors, venue, citations, DOI and PDF link.
    Read one with paper_fetch."""
    try:
        results = await papers.search(query, num_results, ENV["SEARXNG_URL"], TIMEOUT, year_from)
    except Exception as e:  # noqa: BLE001
        return f"Paper search failed (is the SearXNG container running?): {e}"
    return papers.format_results(results) if results else "No papers found."


@mcp.tool(title="Read a research paper", annotations=WRITES_FILES)
async def paper_fetch(
    ref: Annotated[str, Field(description=(
        'Which paper: arXiv id ("1706.03762"), arXiv URL, DOI ("10.1038/nature14539"), '
        "doi.org URL, or a direct PDF URL."))],
    save_dir: Annotated[str, Field(description='Also save the PDF in this folder, e.g. "~/Downloads/papers". Empty = don\'t save.')] = "",
    max_chars: Annotated[int, Field(description="Characters of text to return per call.", ge=1000)] = 30000,
    start: Annotated[int, Field(description="Character offset to continue reading a long paper from.", ge=0)] = 0,
) -> str:
    """Read a research paper as plain text. DOIs resolve to a free open-access copy
    (via OpenAlex). Long papers are paged: the output tells you the start= for the next part."""
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
        return f"Paper fetch failed for {ref}: {e}"
    return f"({note}; via {via}; {url})\n{fetch.window(text, start, max_chars)}"


@mcp.tool(title="Read a web page", annotations=READ_ONLY)
async def fetch_page(
    url: Annotated[str, Field(description="Full URL of a web page or PDF.")],
    max_chars: Annotated[int, Field(description="Characters of text to return per call.", ge=500)] = 20000,
    start: Annotated[int, Field(description="Character offset to continue reading a long page from.", ge=0)] = 0,
) -> str:
    """Read a web page or PDF as clean markdown, with title/author/date when known.
    Reddit and YouTube URLs return the post with comments / the transcript. Gets past most
    bot checks: Chrome-fingerprinted request -> stealth browser -> your logged-in Chrome (if
    configured) -> a visible browser window (you may be asked to tick a check once) ->
    Jina reader; sites your ISP blocks are retried through Tor. Long pages are paged:
    the output tells you the start= for the next part."""
    kind = sources.classify(url)
    if kind == "reddit":
        try:
            return "(reddit)\n" + await sources.reddit_fetch(url)
        except Exception as e:  # noqa: BLE001
            return f"Reddit fetch failed: {e}"  # never scrape reddit directly: bans the IP
    if kind == "youtube":
        try:
            return await sources.youtube_transcript(url)
        except Exception as e:  # noqa: BLE001
            return f"Transcript unavailable ({e})."
    try:
        via, text = await fetch.fetch_text(url, TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return f"Fetch failed for {url}\n{e}"
    if kind == "instagram" and re.search(r"log ?in|sign up", text[:1500], re.I):
        return ("Instagram showed its login wall to an anonymous request. For indexed content use "
                "web_search(include_domains='instagram.com'); for logged-in pages use agent-browser.\n\n"
                + text[:1500])
    return f"(via {via})\n{fetch.window(text, start, max_chars)}"


@mcp.tool(title="Read several web pages", annotations=READ_ONLY)
async def fetch_pages(
    urls: Annotated[str, Field(description="Up to 20 URLs separated by spaces or commas.")],
    concurrency: Annotated[int, Field(description="How many to fetch at once.", ge=1, le=10)] = 5,
    max_chars_each: Annotated[int, Field(description="Characters of text to return per page.", ge=500)] = 6000,
) -> str:
    """Read several pages at once (same fetching as fetch_page), one section per URL."""
    targets = [u for u in re.split(r"[,\s]+", urls) if u.startswith("http")][:20]
    if not targets:
        return "No URLs given."
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(u: str) -> str:
        async with sem:
            return await fetch_page(u, max_chars=max_chars_each)
    results = await asyncio.gather(*(one(u) for u in targets))
    return "\n".join(f"===== {u} =====\n{r}" for u, r in zip(targets, results))


@mcp.tool(title="Read Reddit", annotations=READ_ONLY)
async def reddit_fetch(
    target: Annotated[str, Field(description="A post URL, or a subreddit: \"r/valheim\" or \"valheim\".")],
    sort: Annotated[Literal["hot", "new", "top", "best"], Field(description="Order for a subreddit feed.")] = "hot",
    limit: Annotated[int, Field(description="Posts in a feed, or top comments on a post.", ge=1, le=100)] = 15,
) -> str:
    """Read a Reddit post with its body and top comments, or a subreddit's post list."""
    try:
        return await sources.reddit_fetch(target, sort=sort, limit=limit)
    except Exception as e:  # noqa: BLE001
        return f"Reddit fetch failed: {e}"


@mcp.tool(title="YouTube transcript", annotations=READ_ONLY)
async def youtube_transcript(
    url: Annotated[str, Field(description="YouTube video URL (watch, youtu.be, shorts or live).")],
    lang: Annotated[str, Field(description='Preferred caption language code, e.g. "en", "hi", "de".')] = "en",
) -> str:
    """Get a YouTube video's transcript (captions, or auto-generated), with timestamps."""
    try:
        return await sources.youtube_transcript(url, lang=lang)
    except Exception as e:  # noqa: BLE001
        return f"Transcript unavailable: {e}"


@mcp.tool(title="Find books, films, anime, games", annotations=READ_ONLY)
async def media_search(
    query: Annotated[str, Field(description='Title, optionally with author/year, e.g. "dune frank herbert".')],
    category: Annotated[Literal["books", "comics", "manga", "anime", "movies", "tv", "games", "torrents", "all"],
                        Field(description=(
                            "What kind of thing. manga includes manhwa/manhua; tv includes K-drama; "
                            "torrents searches the general torrent indexes; all = every source."))] = "all",
    num_results: Annotated[int, Field(description="Results per source.", ge=1, le=30)] = 10,
    sites: Annotated[list[MediaSite] | None, Field(description="Only ask these sources. Empty = all sources for the category.")] = None,
) -> str:
    """Find books, comics, manga/manhwa, anime, movies, TV/K-drama and games, searching many
    sources in parallel. Returns WHAT IT IS (AniList, MangaDex, TVmaze: format, episodes,
    status) and WHERE TO GET IT: torrents with magnet link and seeders, books/comics with an
    md5 (download with book_download), or download pages."""
    if category not in media.SOURCES:
        return f"Unknown category {category!r}. Use one of: {', '.join(media.SOURCES)}"
    catalog, found, notes = await media.search(query, category, num_results, sites)
    if not catalog and not found:
        return f"Nothing found. sources: {', '.join(notes)}"
    return media.format_results(catalog, found, notes, num_results)


@mcp.tool(title="Download a book", annotations=WRITES_FILES)
async def book_download(
    md5: Annotated[str, Field(description="The 32-character md5 shown by media_search.")],
    save_dir: Annotated[str, Field(description="Folder to save into.")] = "~/Downloads/books",
) -> str:
    """Download a book, comic or paper by md5 and save it with its original file name."""
    try:
        return await media.book_download(md5.strip().lower(), save_dir)
    except Exception as e:  # noqa: BLE001
        return f"Download failed for {md5}: {e}"


@mcp.tool(title="Usage and mirror status", annotations=READ_ONLY)
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
