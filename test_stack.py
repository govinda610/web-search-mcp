"""Stack verification: pure logic, every tool, routing, transports, graceful degradation.
Run: uv run test_stack.py         (local providers only: SearXNG + DuckDuckGo, no paid quota)
     uv run test_stack.py --paid  (also exercises Exa/Tavily/Firecrawl/Jina)
Live network. LLM checks use the coding-plan models."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
import discover  # noqa: E402
import fetch  # noqa: E402
import llm  # noqa: E402
import media  # noqa: E402
import mirrors  # noqa: E402
import papers  # noqa: E402
import providers  # noqa: E402
import quota  # noqa: E402
import server  # noqa: E402
import social  # noqa: E402
import sources  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

RESULTS = []
LOCAL = ("searxng", "duckduckgo")
if "--paid" not in sys.argv:
    server.CONFIG["search_providers"] = [p for p in server.CONFIG["search_providers"]
                                         if p["name"] in LOCAL]


def ok(name, cond, detail=""):
    RESULTS.append((name, bool(cond), str(detail)[:110].replace("\n", " | ")))


async def err(coro) -> str:
    """The ToolError message a failing tool call raises, or "" if it didn't fail."""
    try:
        await coro
    except ToolError as e:
        return str(e)
    return ""


class FakeResponse:
    def __init__(self, disposition, content_type="application/epub+zip"):
        self.headers = {"content-disposition": disposition, "content-type": content_type}
        self.content = b"book"


def _reset():
    shutil.rmtree(fetch.CACHE, ignore_errors=True)
    sources._rss_cache.clear()
    sources._rss_last_hit = 0.0


async def unit_tests():
    ok("logic: domain include subdomain", server._domain_ok("https://old.reddit.com/x", ["reddit.com"], None))
    ok("logic: domain exclude", not server._domain_ok("https://a.reddit.com/x", None, ["reddit.com"]))
    ok("logic: domain no substring match", not server._domain_ok("https://dropbox.com/x", ["x.com"], None))
    ok("logic: url key drops tracking, keeps ids",
       server._url_key("https://www.youtube.com/watch?v=abc&utm_source=x#t")
       == server._url_key("http://youtube.com/watch?v=abc")
       and server._url_key("https://a.com/p?id=1") != server._url_key("https://a.com/p?id=2"))
    ok("logic: interleave takes each run's best first",
       [r["url"] for r in server._interleave([[{"url": "a"}, {"url": "b"}], [{"url": "c"}]], 3)] == ["a", "c", "b"])
    ok("logic: recency searxng", server._recency_opts("searxng", "week") == {"time_range": "week"})
    ok("logic: recency firecrawl", server._recency_opts("firecrawl", "day") == {"tbs": "qdr:d"})
    ok("logic: recency duckduckgo", server._recency_opts("duckduckgo", "month") == {"df": "m"})
    ok("logic: recency invalid", server._recency_opts("searxng", "bogus") == {})
    ok("logic: classify reddit", sources.classify("https://old.reddit.com/r/x") == "reddit")
    ok("logic: classify lookalike host", sources.classify("https://notreddit.com/r/x") == "generic")
    ok("logic: classify youtube", sources.classify("https://youtu.be/abc") == "youtube")
    ok("logic: classify generic", sources.classify("https://example.com") == "generic")
    ok("logic: yt id watch", sources._yt_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ")
    ok("logic: yt id live", sources._yt_id("https://www.youtube.com/live/dQw4w9WgXcQ") == "dQw4w9WgXcQ")
    ok("logic: arxiv id bare", papers.arxiv_id("1706.03762") == "1706.03762")
    ok("logic: arxiv id url", papers.arxiv_id("https://arxiv.org/abs/1706.03762v7") == "1706.03762v7")
    ok("logic: arxiv id doi", papers.arxiv_id("10.48550/arXiv.1706.03762") == "1706.03762")
    ok("logic: arxiv id none for doi", papers.arxiv_id("10.1038/nature14539") is None)
    ok("logic: challenge detect CF", fetch.is_challenge(b"<html><title>Just a moment...</title>"))
    ok("logic: challenge ignores normal", not fetch.is_challenge(b"<html><title>Blog</title>just a moment"))
    long_text = "x" * 100
    ok("logic: window paging", "start=40" in fetch.window(long_text, 0, 40)
       and fetch.window(long_text, 80, 40) == "x" * 20)
    ok("logic: quota unlimited", quota.remaining("searxng", None) is None)
    ok("logic: quota limit=0 means skip", quota.remaining("exa", 0) == 0)
    ok("logic: base32 infohash -> hex", media._hex_hash("RX44RBWVDFTWCZOTNEQGU3WMY7IVXTWO")
       == "8df9c886d519676165d369206a6eccc7d15bcece")
    ok("logic: challenge detect DDoS-Guard", fetch.is_challenge(b"<html><title>DDoS-Guard</title>"))

    class CurlError(Exception):
        def __init__(self, code, msg):
            super().__init__(msg)
            self.code = code
    ok("logic: ISP block = unreachable", fetch.is_unreachable(CurlError(28, "Connection timed out after 15000 ms")))
    ok("logic: slow site != unreachable", not fetch.is_unreachable(CurlError(28, "Operation timed out after 15000 ms")))
    ok("logic: DNS failure = unreachable", fetch.is_unreachable(CurlError(6, "Could not resolve host")))
    for url in ("file:///etc/passwd", "http://127.0.0.1:8888/search", "http://192.168.1.1/", "http://localhost:9222/json"):
        try:
            await fetch.check_url(url)
            ok(f"security: rejects {url}", False, "allowed")
        except fetch.FetchError as e:
            ok(f"security: rejects {url}", True, e)
    with tempfile.TemporaryDirectory() as d:
        a = media.save_download(FakeResponse('attachment; filename="../../evil.epub"'), "0" * 32, d)
        b = media.save_download(FakeResponse("attachment; filename*=UTF-8''Caf%C3%A9.epub"), "0" * 32, d)
        c = media.save_download(FakeResponse('attachment; filename="Café.epub"'), "0" * 32, d)
        e = media.save_download(FakeResponse(""), "1" * 32, d)
        ok("security: download name can't escape folder", sorted(os.listdir(d)) ==
           ["1" * 32 + ".epub+zip", "Café (1).epub", "Café.epub", "evil.epub"], sorted(os.listdir(d)))
        ok("logic: download names", all(d in x for x in (a, b, c, e)), b)
    ok("logic: social target parsing",
       social.parse("@karpathy") == ("x", ["karpathy"]) and social.parse("@jay.bsky.team")[0] == "bluesky"
       and social.parse("https://t.me/s/durov") == ("telegram", ["s", "durov"])
       and not social.is_social("https://example.com/x"))
    passages = server._best_passages("---\ntitle: capital of France\n---\nA paragraph about something "
                                     "else entirely, long enough to count.\n\nParis is the capital of France "
                                     "and its largest city by far.", "capital of France", limit=60)
    ok("logic: best passages skip header", passages.startswith("Paris") and "title:" not in passages, passages)


async def search_tests():
    r = await server.web_search("valheim 1.0 seeds", 5)
    ok("search: fallback", r.startswith("[searxng]"), r[:50])
    r = await server.web_search("valheim 1.0 seeds", 3, more_queries=["valheim ashlands boss"])
    ok("search: parallel queries tagged", "(q: valheim ashlands boss)" in r, r[:60])
    r = await server.web_search("python asyncio gather", 3, depth="advanced")
    ok("search: depth=advanced adds passages", "--- page passages ---" in r, r[:60])
    r = await server.web_search("valheim 1.0 seeds", 6, strategy="merge")
    ok("search: merge", len(r) > 100, r[:50])
    r = await server.web_search("quantum computing 2026 results", 8, strategy="exhaustive")
    ok("search: exhaustive", r.count("\n  http") >= 6, r[:50])
    r = await server.web_search("valheim seeds", 5, include_domains=["reddit.com"])
    urls = [line.strip() for line in r.splitlines() if line.startswith("  http")]
    ok("search: include_domains", urls and all("reddit.com" in u for u in urls), r[:60])
    r = await server.web_search("valheim seeds", 5, exclude_domains=["reddit.com"])
    ok("search: exclude_domains", "reddit.com" not in r, r[:60])
    r = await server.web_search("attention is all you need", 5, filetype="pdf")
    urls = [line.strip() for line in r.splitlines() if line.startswith("  http")]
    ok("search: filetype", urls and sum(".pdf" in u or "/pdf/" in u for u in urls) >= len(urls) // 2, urls[:2])
    p1 = await server.web_search("valheim seeds", 5)
    p2 = await server.web_search("valheim seeds", 5, page=2)
    ok("search: page 2 differs", p2 != p1 and p2.startswith("[searxng]"), p2[:50])
    r = await server.web_search("openai news", 4, recency="week")
    ok("search: recency", len(r) > 50 and "[jina]" not in r, r[:50])
    r = await server.suggest("valheim see")
    ok("search: suggest is a clean list", r.startswith("valheim") and "[" not in r, r[:40])
    r = await server.news_search("claude anthropic", 3, "month")
    ok("search: news_search", r.startswith("(via"), r[:50])
    r = await server.knowledge_search("mixture of experts", None, 2)
    ok("search: knowledge default sources", all(f"{s}: 2" in r for s in ("wikipedia", "github", "openreview")), r[:110])
    r = await server.knowledge_search("pandas merge", ["stackoverflow", "packages", "huggingface_models", "lemmy"], 2)
    ok("search: knowledge other sources", "failed" not in r.split("\n")[0], r[:110])
    for kind, q, want in [("stock", "RELIANCE.NS", "INR"), ("stock", "nvidia", "NVDA"),
                          ("currency", "100 USD to INR", "INR"), ("crypto", "bitcoin", "BTC"), ("weather", "Jaipur", "°C")]:
        try:
            r = await server.live_data(kind, q)
            ok(f"live: {kind} {q}", want in r, r[:80])
        except ToolError as e:
            ok(f"live: {kind} {q}", False, e)
    r = await server.image_search("valheim world", 4)
    ok("search: image_search", "img:" in r, r[:60])


async def paper_tests():
    r = await server.paper_search("attention is all you need transformer", 5)
    ok("papers: search", "1." in r and ("pdf:" in r or "doi:" in r), r[:80])
    r = await server.paper_search("large language model agents", 5, year_from=2025)
    years = [int(line.split("(")[-1][:4]) for line in r.splitlines()
             if line[:1].isdigit() and line.rstrip().endswith(")") and line.split("(")[-1][:4].isdigit()]
    ok("papers: year_from filter", years and min(years) >= 2025, years)
    with tempfile.TemporaryDirectory() as d:
        r = await server.paper_fetch("1706.03762", save_dir=d, max_chars=3000)
        saved = [f for f in os.listdir(d) if f.endswith(".pdf")]
        ok("papers: fetch arXiv + save pdf", "Attention" in r and saved
           and open(os.path.join(d, saved[0]), "rb").read(5) == b"%PDF-", r[:80])
    ok("papers: long paper is paged", "call again with start=3000" in r, r[-80:])
    r = await server.paper_fetch("10.48550/arXiv.1706.03762", max_chars=2000)
    ok("papers: fetch via arXiv DOI", "arXiv 1706.03762" in r, r[:60])
    url, note = await papers.resolve("10.1371/journal.pone.0000308", 15)
    ok("papers: DOI -> open-access url", url.startswith("http") and "open-access" in note, note)


async def fetch_tests():
    r = await server.fetch_page("https://example.com")
    ok("fetch: example.com", r.startswith("(via curl_cffi)") and "Example Domain" in r, r[:40])
    r = await server.fetch_page("https://example.com")
    ok("fetch: cache hit", r.startswith("(via cache)"), r[:30])
    r = await server.fetch_page("https://arxiv.org/pdf/1706.03762", max_chars=2000)
    ok("fetch: PDF -> text", "Attention Is All You Need" in r and "%PDF" not in r, r[:80])
    r = await server.fetch_page("https://en.wikipedia.org/wiki/Attention_(machine_learning)")
    ok("fetch: HTML -> markdown w/ metadata", "title:" in r[:400] and "Jump to content" not in r, r[:80])
    r = await server.fetch_page("https://www.nowsecure.nl")
    ok("fetch: anti-bot page passes", r.startswith("(via") and "Fetch failed" not in r, r[:60])
    status, _, body = await fetch._camoufox_visible("https://example.com", 15)
    ok("fetch: visible browser stage", status == 200 and b"Example Domain" in body, len(body))
    os.environ["FETCH_VISIBLE_BROWSER"] = "0"
    try:
        await fetch._camoufox_visible("https://example.com", 15)
        ok("fetch: visible browser can be disabled", False, "stage ran while disabled")
    except fetch.FetchError as e:
        ok("fetch: visible browser can be disabled", "disabled" in str(e), e)
    del os.environ["FETCH_VISIBLE_BROWSER"]
    r = await err(server.fetch_page("https://example.com/definitely-missing-page-404"))
    ok("fetch: 404 fails fast", "HTTP 404" in r, r[:60])
    r = await server.fetch_page("https://en.wikipedia.org/wiki/Attention_(machine_learning)", 800,
                                query="multi-head attention")
    ok("fetch: query returns relevant passages", "passages matching" in r and "head" in r.lower(), r[:80])
    r = await server.fetch_page("https://x.com/karpathy/status/1886192184808149383")
    ok("fetch: routes X posts", r.startswith("(via social)") and "vibe coding" in r, r[:60])
    r = await server.fetch_page("https://www.limetorrents.fun/search/all/dune/", 300)
    ok("fetch: ISP-blocked site via Tor", "+tor)" in r or "(via cache)" in r, r[:60])
    r = await server.fetch_page("https://youtu.be/dQw4w9WgXcQ")
    ok("fetch: routes youtube", "(video dQw4w9WgXcQ" in r, r[:50])
    r = await server.fetch_page("https://www.reddit.com/r/commandline/comments/1woks8t/")
    ok("fetch: routes reddit (post + comments)", "POST:" in r and "[u/" in r, r[:60])
    r = await server.fetch_pages(["https://example.com", "https://httpbin.org/html", "file:///etc/passwd"])
    ok("fetch: fetch_pages multi",
       all(f"===== {u}" in r for u in ("https://example.com", "https://httpbin.org/html")), r[:60])
    ok("fetch: fetch_pages reports a bad url inline", "FAILED: only http(s)" in r, r[-80:])
    r = await server.site_map("https://docs.astral.sh/uv/", 5, "guides")
    ok("fetch: site_map from sitemap.xml", "sitemap file" in r and r.count("https://docs.astral.sh/uv/guides") == 5, r[:80])
    r = await server.site_map("news.ycombinator.com", 5)
    ok("fetch: site_map falls back to links", "no sitemap" in r and "/newest" in r, r[:80])
    ok("security: site_map rejects local", "local/private" in await err(server.site_map("http://127.0.0.1:8888")))


async def reddit_tests():
    r1 = await sources.reddit_fetch("r/valheim", limit=2)
    ok("reddit: subreddit RSS feed", r1.startswith("- "), r1[:50])
    t = time.time()
    r2 = await sources.reddit_fetch("r/valheim", limit=2)
    ok("reddit: second call cached", r2 == r1 and time.time() - t < 1.0, f"{time.time()-t:.2f}s")


async def social_tests():
    for target, want in [("@karpathy", "followers"), ("https://bsky.app/profile/jay.bsky.team", "@jay.bsky.team"),
                         ("https://t.me/durov", "https://t.me/durov/"),
                         ("https://x.com/karpathy/status/1886192184808149383", "vibe coding")]:
        try:
            r = await server.social_fetch(target, 3)
            ok(f"social: {target}", want in r, r[:80])
        except ToolError as e:
            ok(f"social: {target}", False, e)
    ok("social: unknown site is a clear error", "not an X, Bluesky" in await err(server.social_fetch("https://example.com/a")))


async def discover_tests():
    saved = discover.SNAPSHOT
    discover.SNAPSHOT = Path(tempfile.mkdtemp()) / "discovery.json"
    try:
        r1 = await server.discover_sources()
        ok("discover: first run saves baseline", r1.startswith("Baseline saved") and discover.SNAPSHOT.exists(), r1[:80])
        snap = json.loads(discover.SNAPSHOT.read_text())
        snap["prowlarr"].remove("eztv")
        snap["fmhy"].pop(next(iter(snap["fmhy"])))
        discover.SNAPSHOT.write_text(json.dumps(snap))
        r2 = await server.discover_sources()
        ok("discover: reports new sources", "EZTV [public]" in r2 and "New FMHY starred sites (1)" in r2
           and "No longer starred" not in r2, r2[:110])
    finally:
        discover.SNAPSHOT = saved


async def llm_tests():
    r = await server.web_search("who won nobel physics 2025", 5, answer=True)
    ok("llm: answer synthesis", "ANSWER" in r, r[:60])
    r = await server.web_search("best programming language 2026", 5, highlights=True)
    ok("llm: highlights", "HIGHLIGHTS" in r, r[:60])
    spec = await server._auto_classify("tesla stock news this week")
    ok("llm: auto classify returns a spec", spec.get("news") is True or spec.get("strategy"), spec)


async def degradation_tests():
    orig = server.ENV["SEARXNG_URL"]
    server.ENV["SEARXNG_URL"] = "http://127.0.0.1:1"
    try:
        r = await server.web_search("valheim seeds", 3)
    except ToolError as e:
        r = str(e)
    server.ENV["SEARXNG_URL"] = orig
    ok("degrade: searxng down -> next provider or clear error",
       "[searxng]" not in r and ("\n  http" in r or "searxng:" in r), r[:60])
    op = llm._providers
    llm._providers = lambda: []
    r = await server.web_search("valheim seeds", 3, answer=True, highlights=True, auto=True)
    llm._providers = op
    ok("degrade: llm down -> plain results", "ANSWER" not in r and "\n  http" in r, r[:60])
    saved = dict(providers.REGISTRY)
    providers.REGISTRY.clear()
    r = await err(server.web_search("x", 3))
    providers.REGISTRY.update(saved)
    ok("degrade: no providers message", "No search providers" in r, r[:60])
    r = await err(server.fetch_page("https://this-domain-really-does-not-exist-93847.com/"))
    ok("degrade: fetch bad domain message", r.startswith("Fetch failed"), r[:60])
    ok("degrade: failed fetch not cached",
       fetch.cache_get("https://this-domain-really-does-not-exist-93847.com/") is None)
    r = await err(server.youtube_transcript("https://youtu.be/aiqDb3abSsc"))
    ok("degrade: youtube captionless message", r.startswith("Transcript unavailable") or r == "", r[:60])


async def media_tests():
    for name, query, category in [("knaben", "dune part two", "movies"), ("piratebay", "dune part two", "movies"),
                                  ("torrents_csv", "dune part two", ""), ("yts", "dune", ""),
                                  ("nyaa", "berserk", "anime"), ("subsplease", "one piece", ""),
                                  ("animetosho", "frieren", ""), ("fitgirl", "elden ring", ""),
                                  ("libgen", "dune frank herbert", "books"), ("getcomics", "saga", ""),
                                  ("mangadex", "solo leveling", ""), ("anilist", "frieren", "anime"),
                                  ("tvmaze", "crash landing on you", ""), ("eztv", "the bear", "tv"),
                                  ("limetorrents", "dune part two", "movies"), ("zlibrary", "dune frank herbert", "books"),
                                  ("openlibrary", "dune frank herbert", "books"), ("gutenberg", "pride and prejudice", ""),
                                  ("weebcentral", "solo leveling", ""), ("mangaupdates", "solo leveling", ""),
                                  ("kuryana", "crash landing on you", ""), ("kisskh", "crash landing on you", ""),
                                  ("opensubtitles", "dune part two", ""), ("itunes", "sapiens", "audiobooks"),
                                  ("audiobookbay", "dune", ""),
                                  ("archive_org", "moby dick", "audiobooks")]:
        try:
            res = await getattr(media, name)(query, 3, category)
            ok(f"media: {name}", res and res[0]["title"], res[0]["title"] if res else "no results")
        except Exception as e:  # noqa: BLE001
            ok(f"media: {name}", False, f"{type(e).__name__}: {e}")
    r = await server.media_search("frieren", "anime", 3)
    ok("media: search = catalog + magnets", "WHAT IT IS" in r and "magnet:?xt=urn:btih:" in r, r[:60])
    with tempfile.TemporaryDirectory() as d:
        r = await server.book_download("92651ea7d95073ba4c8d345285b6bf74", d)  # an 832 kB epub
        ok("media: book_download saves epub", r.startswith("saved") and ".epub" in r, r[:60])
    # self-healing: the only known domain is dead and the list is stale -> refresh from Prowlarr
    saved_path, saved_state = mirrors.STATE, mirrors._state
    mirrors.STATE, mirrors._state = Path(tempfile.mkdtemp()) / "m.json", {
        "yts": {"domains": ["https://yts.invalid"], "refreshed": 0}}
    try:
        await mirrors.call("yts", [], {"prowlarr": "yts"},
                           lambda base: media.http(f"{base}/api/v2/list_movies.json?query_term=dune&limit=1"))
        ok("mirrors: dead domain healed via Prowlarr", not mirrors.status().startswith("  yts: https://yts.invalid"),
           mirrors.status())
    except Exception as e:  # noqa: BLE001
        ok("mirrors: dead domain healed via Prowlarr", False, e)
    finally:
        mirrors.STATE, mirrors._state = saved_path, saved_state


async def usage_test():
    u = server.usage_status()
    ok("usage: provider table", "provider" in u and "searxng" in u, u[:40])


async def transport_stdio():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=["server.py"], cwd=os.getcwd())
    async with stdio_client(params) as (rw, ww):
        async with ClientSession(rw, ww) as s:
            init = await s.initialize()
            ok("transport: server instructions sent", "media_search" in (init.instructions or ""), init.instructions)
            tools = (await s.list_tools()).tools
            names = sorted(t.name for t in tools)
            schema = next(t for t in tools if t.name == "web_search").input_schema["properties"]
            ok("transport: choices exposed as enums", schema["strategy"].get("enum") == ["fallback", "merge", "exhaustive"]
               and all("description" in v for v in schema.values()), schema["strategy"])
            ok("transport: stdio 18 tools", len(names) == 18, str(names))
            ok("transport: no redundant output schemas", not any(t.output_schema for t in tools))
            res = await s.call_tool("usage_status", {})
            ok("transport: stdio call works", "provider" in res.content[0].text and not res.structured_content)
            res = await s.call_tool("fetch_page", {"url": "file:///etc/passwd"})
            ok("transport: failure is isError with reason", res.is_error and "only http(s)" in res.content[0].text,
               res.content[0].text)
            res = await s.call_tool("media_search", {"query": "x", "category": "bogus"})
            ok("transport: bad choice rejected", res.is_error, res.content[0].text)


async def transport_http():
    import subprocess
    env = dict(os.environ, MCP_TRANSPORT="http", MCP_PORT="8766")
    proc = subprocess.Popen([sys.executable, "server.py"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await asyncio.sleep(4)
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post("http://127.0.0.1:8766/mcp", headers=h, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "1"}}})
            ok("transport: http initialize", r.status_code == 200 and "result" in r.text,
               r.text[:60])
    finally:
        proc.terminate()


async def main():
    _reset()
    for group in (unit_tests, search_tests, paper_tests, fetch_tests, reddit_tests, social_tests, discover_tests,
                  llm_tests, degradation_tests, media_tests, usage_test, transport_stdio, transport_http):
        try:
            await group()
        except Exception as e:  # noqa: BLE001 - one broken group shouldn't hide the others
            ok(f"{group.__name__}: crashed", False, f"{type(e).__name__}: {e}")
    n_ok = sum(1 for _, p, _ in RESULTS if p)
    print(f"\n{'=' * 76}")
    for name, passed, detail in RESULTS:
        print(f"{name:<42}{'PASS' if passed else 'FAIL':<7}{detail}")
    print(f"{'=' * 76}\n{n_ok}/{len(RESULTS)} PASSED")
    sys.exit(0 if n_ok == len(RESULTS) else 1)


asyncio.run(main())
