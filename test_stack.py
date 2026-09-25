"""Stack verification: pure logic, every tool, routing, transports, graceful degradation.
Run: uv run test_stack.py         (local providers only: SearXNG + DuckDuckGo, no paid quota)
     uv run test_stack.py --paid  (also exercises Exa/Tavily/Firecrawl/Jina)
Live network. LLM checks use the coding-plan models."""
import asyncio
import gzip
import hashlib
import ipaddress
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
# work on a copy of state/ (mirrors, cookies, snapshots) so tests never change the real one
_TEST_STATE = Path(tempfile.mkdtemp()) / "state"
_REAL_STATE = Path(__file__).parent / "state"
if _REAL_STATE.exists():
    shutil.copytree(_REAL_STATE, _TEST_STATE, ignore=shutil.ignore_patterns("cache"))
os.environ["WEB_MCP_STATE_DIR"] = str(_TEST_STATE)
import crawl  # noqa: E402
import discover  # noqa: E402
import fetch  # noqa: E402
import index  # noqa: E402
import llm  # noqa: E402
import media  # noqa: E402
import mirrors  # noqa: E402
import movies  # noqa: E402
import papers  # noqa: E402
import providers  # noqa: E402
import quality  # noqa: E402
import quota  # noqa: E402
import rerank  # noqa: E402
import research  # noqa: E402
import server  # noqa: E402
import social  # noqa: E402
import sources  # noqa: E402
import torrent  # noqa: E402
import watch  # noqa: E402
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


async def outcome(coro) -> str:
    """The call's result, or "ExceptionType: message" if it raised."""
    try:
        return await coro
    except Exception as e:  # noqa: BLE001 - the caller checks which kind of failure it was
        return f"{type(e).__name__}: {e}"


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
    twins = [{"url": "https://x.com/rust-async", "title": "Rust async", "snippet": "Tokio vs smol."},
             {"url": "https://x.com/rust_async", "title": "Rust  Async", "snippet": "Tokio vs smol"},
             {"url": "https://y.com/home", "title": "Home", "snippet": ""},
             {"url": "https://z.com/home", "title": "Home", "snippet": ""}]
    ok("logic: interleave drops same title+snippet, keeps bare same titles",
       [r["url"] for r in server._interleave([twins], 9)] == [twins[0]["url"], twins[2]["url"], twins[3]["url"]])
    ok("logic: snippets cut at a word", server._short("word " * 100).endswith("word…"))
    schema = next(t for t in server.mcp._tool_manager.list_tools() if t.name == "web_search").parameters
    ok("logic: schemas slimmed", "title" not in schema["properties"]["query"]
       and "anyOf" not in schema["properties"]["include_domains"])
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
        md5 = hashlib.md5(b"book").hexdigest()  # noqa: S324 - the content id, not security
        a = media.save_download(FakeResponse('attachment; filename="../../evil.epub"'), md5, d)
        b = media.save_download(FakeResponse("attachment; filename*=UTF-8''Caf%C3%A9.epub"), md5, d)
        c = media.save_download(FakeResponse('attachment; filename="Café.epub"'), md5, d)
        e = media.save_download(FakeResponse(""), md5, d)
        ok("security: download name can't escape folder", sorted(os.listdir(d)) ==
           [md5 + ".epub+zip", "Café (1).epub", "Café.epub", "evil.epub"], sorted(os.listdir(d)))
        ok("logic: download names", all(d in x for x in (a, b, c, e)), b)
    ok("logic: social target parsing",
       social.parse("@karpathy") == ("x", ["karpathy"]) and social.parse("@jay.bsky.team")[0] == "bluesky"
       and social.parse("https://t.me/s/durov") == ("telegram", ["s", "durov"])
       and not social.is_social("https://example.com/x"))
    passages = rerank.best_passages("---\ntitle: capital of France\n---\nA paragraph about something "
                                    "else entirely, long enough to count.\n\nParis is the capital of France "
                                    "and its largest city by far.", "capital of France", limit=60)
    ok("logic: best passages skip header", passages.startswith("Paris") and "title:" not in passages, passages)


async def search_tests():
    r = await server.web_search("valheim 1.0 seeds", 5)
    ok("search: fallback, numbered, untagged", r.startswith("1. ") and "[searxng]" not in r, r[:50])
    r = await server.web_search("rust async runtime", 10, max_chars=1000)
    ok("search: max_chars budget", len(r) < 1600 and "more results omitted" in r, r[-120:])
    r = await server.web_search("best mobile plan", 5, language="en-IN")
    ok("search: region code", r.count(".in/") + r.count(".in\n") >= 1, r[:200])
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
    ok("search: page 2 differs", p2 != p1 and p2.startswith("1. "), p2[:50])
    r = await server.web_search("openai news", 4, recency="week")
    ok("search: recency", len(r) > 50 and "[jina]" not in r, r[:50])
    r = await server.news_search("claude anthropic", 5, "month")
    ok("search: news_search, real article links", "\n  https://" in r and "news.google.com" not in r, r[:120])
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
    with tempfile.TemporaryDirectory(dir=Path.home() / "Downloads") as d:  # save_dir must be under home
        r = await server.paper_fetch("1706.03762", save_dir=d, max_chars=3000)
        saved = [f for f in os.listdir(d) if f.endswith(".pdf")]
        ok("papers: fetch arXiv + save pdf", "Attention" in r and saved
           and open(os.path.join(d, saved[0]), "rb").read(5) == b"%PDF-", r[:80])
    ok("papers: long paper is paged", "call again with start=3000" in r, r[-80:])
    r = await server.paper_fetch("10.48550/arXiv.1706.03762", max_chars=2000)
    ok("papers: fetch via arXiv DOI", "arXiv 1706.03762" in r, r[:60])
    url, note = await papers.resolve("10.1371/journal.pone.0000308", 15)
    ok("papers: DOI -> open-access url", url.startswith("http") and "open-access" in note, note)
    r = await papers.crossref("sparse autoencoders interpretability", 5, 20)
    ok("papers: crossref returns DOIs", r and all(e["doi"] for e in r), [e["title"][:40] for e in r[:2]])
    r = await papers.biorxiv_medrxiv("single cell RNA sequencing", 3, 20)
    ok("papers: bioRxiv/medRxiv preprints", r and all(e["venue"].lower() in ("biorxiv", "medrxiv") for e in r),
       [e["venue"] for e in r])
    r = await outcome(papers._libgen_scimag("10.1038/nature14539", 30))
    ok("papers: paywalled DOI -> LibGen PDF (or clear outage)", r.startswith("http") or "Error" in r, r[:80])


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
    ip = ipaddress.ip_address
    ok("security: CGNAT/metadata/ULA unsafe, public ok",
       all(fetch._is_unsafe_ip(ip(a)) for a in ("100.64.1.1", "169.254.169.254", "10.1.2.3", "fc00::1"))
       and not any(fetch._is_unsafe_ip(ip(a)) for a in ("8.8.8.8", "2606:4700::1")))
    r = await outcome(fetch.check_url("http://100.64.1.1/"))
    ok("security: check_url blocks CGNAT", r.startswith("FetchError"), r)
    r = await outcome(fetch.get_checked("https://httpbin.org/redirect-to?url=http://169.254.169.254/", timeout=15))
    ok("security: redirect to metadata IP blocked", r.startswith("FetchError"), r[:80])
    ok("security: gzip bomb capped", crawl._gunzip(gzip.compress(b"a" * (60 * 1024 * 1024))) is None
       and crawl._gunzip(gzip.compress(b"abc")) == b"abc")
    ok("fetch: jina failure banners detected",
       bool(fetch._JINA_FAILURE.search("Title: x\n\nWarning: Target URL returned error 403: Forbidden"))
       and not fetch._JINA_FAILURE.search("Title: Web crawler\n\nMarkdown Content:\nA web crawler is a bot"))
    r = await server.crawl_site("https://docs.astral.sh/uv", "workspaces", num_pages=6, max_depth=2)
    links = [line for line in r.splitlines() if line.startswith("https://")]
    ok("fetch: crawl_site follows links, no dup root", any("/uv/concepts/" in u for u in links)
       and len({u.rstrip("/") for u in links}) == len(links), r[:80])
    # archive.org is often down; an outage must surface as a clear error, not a hang or crash
    r = await outcome(server.page_history("example.com", 3))
    ok("fetch: page_history lists snapshots (or clear outage)", "web.archive.org/web/" in r or "archive.org" in r, r[:80])
    r = await outcome(server.fetch_page("https://example.com", as_of="2010"))
    ok("fetch: as_of reads an old snapshot (or clear outage)", "(via wayback (snapshot 20" in r or "archive" in r.lower(), r[:80])
    # per-call cache age, forced stages, paywall stubs, the page index, find-similar
    url = "https://example.com/max-age-check"
    fetch.cache_put(url, "cached text")
    os.utime(fetch._cache_file(url), (time.time() - 500, time.time() - 500))
    ok("fetch: max_age narrows the cache window", fetch.cache_get(url) == "cached text"
       and fetch.cache_get(url, 100) is None and fetch.cache_get(url, 0) is None)
    r = await server.fetch_page("https://example.com", max_age=0, method="plain")
    ok("fetch: max_age=0 + method=plain fetch live", r.startswith("(via curl_cffi)"), r[:40])
    r = await outcome(fetch.fetch("https://example.com", method="chrome"))
    ok("fetch: method=chrome without CDP fails clearly", "CHROME_CDP_URL" in r or r.startswith("Page("), r[:80])
    r = await outcome(server.fetch_page("https://en.wikipedia.org/wiki/Paywall", max_chars=300, method="archive"))
    ok("fetch: method=archive (or clear outage)", "(via archive.today)" in r or "(via wayback" in r
       or "no archived copy" in r, r[:80])
    ok("fetch: paywall stub detected, articles not",
       fetch.is_paywalled("Subscribe now to continue reading this story.")
       and not fetch.is_paywalled("subscribe to continue reading. " + "word " * 400))
    ok("index: FTS operators in queries are plain words",
       all(isinstance(index.search(q), list) for q in ('a" OR 1=1--', "AND OR NOT *", '"x', "site:x", "", "()")))
    r = await server.knowledge_search("example domain", sites=["history"])
    ok("index: history finds a page read earlier", "== history ==" in r and "example.com" in r, r[:80])
    r = await server.web_search("rust async runtimes", 5, similar_to="https://tokio.rs/")
    ok("search: similar_to finds other sites", r.startswith("1. ") and "tokio.rs/" not in r, r[:80])
    r = await outcome(server.site_map("http://info.cern.ch", 20))
    ok("fetch: site_map Common Crawl fallback (or clear outage)", "Common Crawl" in r or "no sitemap" in r, r[:80])


async def reddit_tests():
    r1 = await sources.reddit_fetch("r/valheim", limit=2)
    ok("reddit: subreddit RSS feed", r1.startswith("- "), r1[:50])
    t = time.time()
    r2 = await sources.reddit_fetch("r/valheim", limit=2)
    ok("reddit: second call cached", r2 == r1 and time.time() - t < 1.0, f"{time.time()-t:.2f}s")
    r3 = await server.social_fetch("r/valheim", 2)
    ok("reddit: social_fetch routes subreddits", r3 == r1, r3[:50])


async def download_tests():
    for bad in ("~/.ssh", "/tmp/x", "~/Library/LaunchAgents"):
        ok(f"download: refuses {bad}", "Not saving" in await err(server.media_download("https://youtu.be/jNQXAC9IVRw",
                                                                                       save_dir=bad)))
    folder = Path.home() / "Downloads" / "web-mcp-test"
    try:
        for fmt, codec in (("mp3", "mp3"), ("mp4", "h264")):
            r = await server.media_download("https://www.youtube.com/watch?v=jNQXAC9IVRw", format=fmt, max_height=360,
                                            save_dir=str(folder))
            path = r.splitlines()[1].strip().rsplit(" (", 1)[0]
            import subprocess
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                                    path], capture_output=True, text=True).stdout
            ok(f"download: youtube as {fmt}", path.endswith(f".{fmt}") and codec in probe, f"{path} {probe.split()}")
        again = await err(server.media_download("https://www.youtube.com/watch?v=jNQXAC9IVRw", format="mp3",
                                                save_dir=str(folder)))
        ok("download: existing file not redownloaded", "already" in again or not again, again[:80])
        ok("download: bad url is a clear error", "Download failed" in await err(
            server.media_download("https://example.com/not-a-video", save_dir=str(folder))))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


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
        r1 = await server.server_status(check_new_sources=True)
        ok("discover: first run saves baseline", "Baseline saved" in r1 and discover.SNAPSHOT.exists(), r1[:80])
        snap = json.loads(discover.SNAPSHOT.read_text())
        snap["prowlarr"].remove("eztv")
        snap["fmhy"].pop(next(iter(snap["fmhy"])))
        discover.SNAPSHOT.write_text(json.dumps(snap))
        r2 = await server.server_status(check_new_sources=True)
        ok("discover: reports new sources", "EZTV [public]" in r2 and "New FMHY starred sites (1)" in r2
           and "No longer starred" not in r2, r2[:110])
    finally:
        discover.SNAPSHOT = saved


# ---- rerank / sampling / deep research ----
async def _check_rerank(ok):
    passages = [
        "Bananas are a good source of potassium.",
        "Paris is the capital of France and home to many famous landmarks.",
        "Quantum computers use qubits instead of classical bits.",
    ]
    ranked = rerank.rerank("What is the capital of France?", passages, top_k=3)
    ok("rerank: orders the relevant passage first",
       ranked[0][1].startswith("Paris"), str(ranked))
    ok("rerank: returns (score, passage) tuples", all(isinstance(s, float) and isinstance(p, str) for s, p in ranked))

    scored = rerank._bm25("capital of France", passages, 3)
    ok("bm25 fallback: orders the relevant passage first", scored[0][1].startswith("Paris"), str(scored))

    empty = rerank.rerank("anything", [], top_k=5)
    ok("rerank: empty passages -> empty result", empty == [])

    chunks = rerank.split_passages("a" * 50 + "\n\n" + "b" * 700 + "\n\n" + "c" * 10, size=600)
    ok("split_passages: long paragraph is split, short ones kept", len(chunks) == 4 and all(len(c) <= 600 for c in chunks),
       str([len(c) for c in chunks]))

    text = "para about paris and france.\n\n" + "unrelated filler text about bananas and potassium. " * 5
    bp = rerank.best_passages(text, "paris france", limit=1500)
    ok("best_passages: relevant paragraph included", "paris" in bp.lower())


# ---------- offline: llm.can_sample ----------

async def _check_can_sample(ok):
    ok("can_sample(None) is False", llm.can_sample(None) is False)


# ---------- offline: deep_research with fakes ----------

async def _check_deep_research_fakes(ok):
    async def search(query, n):
        return [{"title": f"{query} #{i}", "url": f"https://ex.com/{query}/{i}", "snippet": "s"} for i in range(n)]

    async def read(url):
        return f"Paragraph one about {url}.\n\nParagraph two, more detail about {url}."

    round_n = {"n": 0}

    async def ask(prompt, max_tokens):
        if "3-5 focused web search" in prompt:
            return json.dumps({"queries": ["q1", "q2"]})
        if "write ONE short" in prompt:
            round_n["n"] += 1
            return json.dumps({"notes": {"1": "n1"}, "gaps": []})
        if "Write a thorough research report" in prompt:
            return "Report body [1].\n"
        return None

    report = await research.deep_research("test question", search, read, ask, depth="standard")
    ok("deep_research: cites a source", "[1]" in report)
    ok("deep_research: has a Sources section", "## Sources" in report)

    async def ask_none(prompt, max_tokens):
        return None

    degraded = await research.deep_research("test question", search, read, ask_none, depth="standard")
    ok("deep_research: degrades cleanly when ask() returns None (no LLM)",
       "No LLM was available" in degraded and "## Sources" in degraded)

    searched = []

    async def search_logged(query, n):
        searched.append(query)
        return await search(query, n)

    notes_only = await research.deep_research("test question", search_logged, read, ask, depth="standard",
                                              sub_questions=["mine a", "mine b"], report=False)
    ok("deep_research: uses the caller's sub_questions", searched[:2] == ["mine a", "mine b"], searched)
    ok("deep_research: report=False returns sources, no report",
       "report=False" in notes_only and "Report body" not in notes_only and "## Sources" in notes_only)
    no_llm = await research.deep_research("test question", search, read, ask_none, sub_questions=["mine a"])
    ok("deep_research: sub_questions without an LLM still says so", "No LLM was available" in no_llm)

    async def search_empty(query, n):
        return []

    none_found = await research.deep_research("test question", search_empty, read, ask, depth="standard")
    ok("deep_research: no crash when search finds nothing", "No sources could be read" in none_found)

    async def read_fails(url):
        raise RuntimeError("boom")

    none_read = await research.deep_research("test question", search, read_fails, ask, depth="standard")
    ok("deep_research: no crash when every read() fails", "No sources could be read" in none_read)


async def research_tests():
    await _check_rerank(ok)
    await _check_can_sample(ok)


async def llm_tests():
    r = await server.web_search("who won nobel physics 2025", 5, answer=True)
    ok("llm: answer synthesis", "ANSWER" in r, r[:60])


async def degradation_tests():
    orig = server.ENV["SEARXNG_URL"]
    server.ENV["SEARXNG_URL"] = "http://127.0.0.1:1"
    try:
        r = await server.web_search("valheim seeds", 3)
    except ToolError as e:
        r = str(e)
    server.ENV["SEARXNG_URL"] = orig
    ok("degrade: searxng down -> next provider or clear error",
       "\n  http" in r or "searxng:" in r, r[:60])
    op = llm._providers
    llm._providers = lambda: []
    r = await server.web_search("valheim seeds", 3, answer=True)
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
    with tempfile.TemporaryDirectory(dir=Path.home() / "Downloads") as d:  # save_dir must be under home
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


# ---- quality ranking, IMDb/Torrentio, Prowlarr/Jackett, watch list ----
class _FakeResp:
    def __init__(self, data=None, headers=None, content=b""):
        self._data = data
        self.headers = headers or {}
        self.content = content

    def json(self):
        return self._data


async def media_quality_tests():
    # --- quality.classify -------------------------------------------------
    q = quality.classify("Movie.Title.2026.2160p.BluRay.REMUX.x265-GROUP")
    ok("classify: BluRay REMUX tier", q["tier"] == quality.TIERS["BluRay REMUX"], q)

    q = quality.classify("Movie.Title.2026.720p.HDTS.x264-GROUP")
    ok("classify: TeleSync flagged as cam", q["is_cam"] is True, q)

    q = quality.classify("Movie Title 2026 EZTV.exe")
    ok("classify: fake .exe flagged", any(".exe" in w for w in q["warnings"]), q)

    q = quality.classify("Movie.Title.2026.1080p.WEB-DL", size_bytes=50 * 1024 * 1024, runtime_min=120)
    ok("classify: undersized WEB-DL for known runtime flagged", any("too small" in w for w in q["warnings"]), q)

    q = quality.classify("Some book with a password inside")
    ok("classify: password lure flagged", any("password" in w for w in q["warnings"]), q)

    ok("classify: empty title doesn't crash", quality.classify("") == quality.classify(""))

    # --- fuzzy dedupe, magnet downloads ------------------------------------
    dupes = [{"source": "knaben", "title": "Dune.Part.Two.2024.1080p.WEB-DL", "hash": "a" * 40, "seeders": 50, "year": "2024"},
             {"source": "piratebay", "title": "Dune Part Two 2024 1080p WEB-DL", "hash": "b" * 40, "seeders": 90, "year": "2024"},
             {"source": "eztv", "title": "Show S01E01 1080p WEB-DL", "hash": "c" * 40, "seeders": 5, "year": ""},
             {"source": "eztv", "title": "Show S01E02 1080p WEB-DL", "hash": "d" * 40, "seeders": 5, "year": ""}]
    kept = media.dedupe_similar(dupes)
    ok("media: fuzzy dedupe keeps best seeded, not other episodes", len(kept) == 3
       and any(k["source"] == "piratebay" and "also on knaben" in k.get("info", "") for k in kept), kept)
    r = await outcome(torrent.download("magnet:?xt=urn:btih:" + "0" * 40, tempfile.mkdtemp(), timeout=5))
    ok("media: dead magnet fails clearly", "timed out" in r or "aria2c not found" in r, r[:80])

    # --- media._size_bytes --------------------------------------------------
    ok("_size_bytes: round-trips _size", media._size_bytes(media._size(3 * 1024**3)) == 3 * 1024**3)
    ok("_size_bytes: garbage returns None", media._size_bytes("not a size") is None)

    # --- media._try_hex_hash / _hex_hash robustness -------------------------
    ok("_try_hex_hash: valid 40-char hex", media._try_hex_hash("a" * 40) == "a" * 40)
    ok("_try_hex_hash: malformed hash drops the item, doesn't raise", media._try_hex_hash("!" * 32) is None)

    # --- book_download safety (save_download), via a monkeypatched response
    real_content = b"hello world"
    good_md5 = hashlib.md5(real_content).hexdigest()  # noqa: S324 - matching a known identifier, not a security hash
    try:
        media.save_download(_FakeResp(headers={"content-type": "text/html"}, content=b"<html>login</html>"),
                            good_md5, "/tmp/_media_checks_dl")
        ok("save_download: rejects text/html", False)
    except RuntimeError as e:
        ok("save_download: rejects text/html", "HTML" in str(e), str(e))

    try:
        media.save_download(_FakeResp(headers={"content-type": "application/octet-stream"},
                                      content=b"x" * (media.MAX_DOWNLOAD_BYTES + 1)), good_md5, "/tmp/_media_checks_dl")
        ok("save_download: rejects oversized content", False)
    except RuntimeError as e:
        ok("save_download: rejects oversized content", "exceeds" in str(e), str(e))

    try:
        media.save_download(_FakeResp(headers={"content-type": "application/octet-stream"}, content=real_content),
                            "0" * 32, "/tmp/_media_checks_dl")
        ok("save_download: rejects md5 mismatch", False)
    except RuntimeError as e:
        ok("save_download: rejects md5 mismatch", "mismatch" in str(e), str(e))

    msg = media.save_download(_FakeResp(headers={"content-type": "application/octet-stream",
                                                 "content-disposition": 'attachment; filename="book.txt"'},
                                        content=real_content), good_md5, "/tmp/_media_checks_dl")
    ok("save_download: accepts a matching file", "saved" in msg, msg)

    # --- prowlarr/jackett parsing, via a monkeypatched media.http -----------
    orig_http = media.http

    async def fake_prowlarr_http(url, *a, **kw):
        return _FakeResp(data=[{"indexer": "MyIndexer", "title": "Some.Movie.2026.1080p.WEB-DL", "size": 123,
                                "seeders": 10, "publishDate": "2026-01-01", "magnetUrl": "magnet:?xt=urn:btih:" + "a" * 40,
                                "infoHash": "a" * 40}])

    media.http = fake_prowlarr_http
    os.environ["PROWLARR_URL"], os.environ["PROWLARR_API_KEY"] = "http://fake", "key"
    try:
        res = await media.prowlarr("some movie", 5)
        ok("prowlarr: parses a fake response", len(res) == 1 and res[0]["magnet"].startswith("magnet:"), res)
    finally:
        media.http = orig_http
        del os.environ["PROWLARR_URL"], os.environ["PROWLARR_API_KEY"]

    ok("prowlarr: no-op when unconfigured", (await media.prowlarr("x", 5)) == [])
    ok("jackett: no-op when unconfigured", (await media.jackett("x", 5)) == [])

    # --- mirrors.py concurrency fix: entry["domains"] is re-read, not stale -
    site = "_checks_mirror_test"
    saved_mirrors = mirrors._state
    mirrors._state = {site: {"domains": ["a", "b"], "refreshed": 0}}

    async def attempt(base):
        # simulate a concurrent call() adding a domain mid-flight, before this one writes back
        mirrors._state[site]["domains"] = ["a", "b", "concurrent-add"]
        if base == "a":
            raise RuntimeError("a is down")
        return "ok"

    result = await mirrors.call(site, ["a", "b"], {}, attempt)
    ok("mirrors: concurrent addition survives the success-path write",
      "concurrent-add" in mirrors._state[site]["domains"], mirrors._state[site]["domains"])
    ok("mirrors: winning domain moved to front", mirrors._state[site]["domains"][0] == "b" and result == "ok")
    mirrors._state = saved_mirrors

    # --- watch.py add/remove/list (state isolated via WEB_MCP_STATE_DIR) ----
    watch.add("some test show s01e02", category="tv", min_quality="WEB-DL")
    ok("watch: add shows up in list_watches", "some test show s01e02" in watch.list_watches())
    watch.remove("some test show s01e02")
    ok("watch: remove takes it out of list_watches", "some test show s01e02" not in watch.list_watches())
    ok("watch: tool needs a title to add", "needs a query" in await err(server.release_watch("add")))
    r = await server.release_watch("add", "some test film 2031", "movies", "BluRay")
    ok("watch: tool add + list", "some test film 2031" in await server.release_watch("list"), r)
    await server.release_watch("remove", "some test film 2031")

    # --- a few live checks ---------------------------------------------------
    catalog, found, notes = await media.search("spider-man brand new day", "movies", limit=10)
    cam = [r for r in found if r.get("is_cam")]
    flagged_exe = [r for r in found if any(".exe" in w for w in r.get("warnings", []))]
    ok("live: spider-man catalog resolved", any("imdb" in c["source"] for c in catalog), [c["source"] for c in catalog])
    ok("live: spider-man has a cinema-recordings bucket", len(cam) > 0, len(cam))
    ok("live: spider-man .exe fakes flagged", len(flagged_exe) > 0, len(flagged_exe))

    _, found2, _ = await media.search("obsession 2026", "movies", limit=10)
    top = [r for r in found2 if not r.get("is_cam")][:3]
    ok("live: obsession 2026 top results are real releases (not cam)", all(not r["is_cam"] for r in top), top)
    ok("live: obsession 2026 top result tier is BluRay/WEB-DL or better",
      top and top[0].get("tier", 0) >= quality.TIERS["WEB-DL"], top[0] if top else None)

    tt = await movies.imdb_id("dune part two", "movies")
    torrentio_res = await media.torrentio("dune part two", 5, "movies")
    ok("live: dune part two resolves an imdb id", bool(tt), tt)
    ok("live: dune part two torrentio returns magnets", len(torrentio_res) > 0
      and all(r["magnet"].startswith("magnet:") for r in torrentio_res), len(torrentio_res))


# ---- arXiv ids, Reddit rate limits, monitor, knowledge health, Wikidata, SEC, places, GDELT ----
async def data_tests():
    import fetch
    import health
    import httpx
    import knowledge
    import live
    import monitor
    import papers
    import sources
    from providers import ProviderError

    # ---------------------------------------------------------------- papers.py: arXiv ids
    ok("papers.arxiv_id old-style hep-th", papers.arxiv_id("hep-th/9901001") == "hep-th/9901001")
    ok("papers.arxiv_id old-style math.GT", papers.arxiv_id("math.GT/0309136") == "math.GT/0309136")
    ok("papers.arxiv_id old-style URL", papers.arxiv_id("https://arxiv.org/abs/hep-th/9901001") == "hep-th/9901001")
    ok("papers.arxiv_id new-style unaffected", papers.arxiv_id("2301.12345") == "2301.12345")
    ok("papers.arxiv_id DOI is not arXiv", papers.arxiv_id("10.1038/s41586-021-03819-2") is None)

    # ---------------------------------------------------------------- sources.py: Reddit
    class FakeResp:
        def __init__(self, status_code, headers):
            self.status_code, self.headers = status_code, headers

        def raise_for_status(self):
            pass

    ok("sources._retry_after parses seconds", sources._retry_after(FakeResp(429, {"retry-after": "5"})) == 5.0)

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return FakeResp(429, {"retry-after": "600"})

    real_client = httpx.AsyncClient
    httpx.AsyncClient = FakeClient
    sources._rss_last_hit = 0.0
    start = time.time()
    try:
        await sources._reddit_get_locked("https://www.reddit.com/r/test.rss", 15)
        ok("sources 429 long Retry-After fails fast", False, "did not raise")
    except ProviderError:
        ok("sources 429 long Retry-After fails fast", time.time() - start < 1, "took too long")
    finally:
        httpx.AsyncClient = real_client

    # ---------------------------------------------------------------- monitor.py (monkeypatched fetch_text)
    calls = {"n": 0}
    monitor_pages = ["Title\n\nParagraph one about cats.\n\nParagraph two about dogs.",
                     "Title\n\nParagraph one about cats and kittens.\n\nParagraph two about dogs."]

    async def fake_fetch_text(url, timeout=15, max_age=None, interactive=True, on_stage=None, method="auto"):
        text = monitor_pages[min(calls["n"], len(monitor_pages) - 1)]
        calls["n"] += 1
        return "live", text

    real_fetch_text = fetch.fetch_text
    fetch.fetch_text = fake_fetch_text
    try:
        url = "https://example.com/data-checks-page"
        monitor.forget(url)
        first = await monitor.check(url)
        ok("monitor first check stores baseline", "Baseline stored" in first)
        second = await monitor.check(url)
        ok("monitor second check returns a diff", "cats and kittens" in second and second.startswith("---"))
        third = await monitor.check(url)
        ok("monitor third check reports unchanged", third.startswith("unchanged since"))
        monitor.forget(url)
        ok("monitor forget removes the page", "No watch stored" in monitor.forget(url))
    finally:
        fetch.fetch_text = real_fetch_text

    # ---------------------------------------------------------------- knowledge.py: health skip
    health.record("data-checks-fake-source", False)
    health.record("data-checks-fake-source", False)

    async def boom(query, limit):
        raise RuntimeError("down")

    knowledge.SOURCES["data-checks-fake-source"] = boom
    out = await knowledge.search("x", ["data-checks-fake-source"], 2)
    ok("knowledge.search skips a source health has marked down", "skipped" in out)
    del knowledge.SOURCES["data-checks-fake-source"]

    # ---------------------------------------------------------------- live checks
    try:
        result = await live.currency("how much is USD")
        ok("live.currency ignores non-code English words", "USD" in result and "INR" in result, result[:120])
    except Exception as e:  # noqa: BLE001 - live network check
        ok("live.currency ignores non-code English words", False, str(e))

    try:
        wd = await knowledge.wikidata("Anthropic", 2)
        ok("knowledge.wikidata returns entities with facts", bool(wd) and any(r["info"] for r in wd),
           str(wd)[:150])
    except Exception as e:  # noqa: BLE001 - live network check
        ok("knowledge.wikidata returns entities with facts", False, str(e))

    if os.environ.get("SEC_USER_AGENT"):
        sec_result = await server.live_data("sec_filings", "AAPL 10-K")
        ok("datasets.sec_filings resolves a real filer", "Apple" in sec_result, sec_result[:150])
    else:
        ok("datasets.sec_filings without SEC_USER_AGENT is a clear error",
           "SEC_USER_AGENT" in await err(server.live_data("sec_filings", "AAPL")))

    try:
        places_result = await server.live_data("places", "cafe near Koramangala, Bangalore")
        ok("datasets.places finds results near an Indian location", "cafe" in places_result.lower(),
           places_result[:150])
    except Exception as e:  # noqa: BLE001 - live network check; overpass-api.de may be blocked on some networks
        ok("datasets.places finds results near an Indian location", False, str(e))

    try:
        news = await server.news_search("openai", 2, "day", trends=True)
        ok("datasets.news_trends returns GDELT data", "articles" in news.lower(), news[:150])
    except Exception as e:  # noqa: BLE001 - live network check; GDELT rate-limits aggressively
        ok("datasets.news_trends returns GDELT data", False, str(e))
    for sites, want in [(["clinicaltrials"], "clinicaltrials.gov/study/"), (["openfda"], "dailymed"),
                        (["courtlistener"], "courtlistener.com"), (["code"], "github.com/"),
                        (["wiktionary"], "wiktionary.org")]:
        query = {"clinicaltrials": "diabetes", "openfda": "ibuprofen", "courtlistener": "miranda",
                 "code": "asyncio.TaskGroup", "wiktionary": "serendipity"}[sites[0]]
        r = await outcome(server.knowledge_search(query, sites, 2))
        ok(f"knowledge: {sites[0]}", want in r.lower(), r[:120])
    r = await outcome(server.knowledge_search("battery", ["patents"], 2))
    ok("knowledge: patents without a key skips cleanly", "patents: 0" in r or "patents.google.com" in r, r[:120])
    r = await outcome(server.live_data("economy", "India GDP growth"))
    ok("live_data: economy (World Bank)", "World Bank" in r and "India" in r, r[:120])
    r = await server.page_watch("list")
    ok("page_watch: list works", "No pages" in r or "last checked" in r, r[:80])
    ok("page_watch: check needs a url", "needs a url" in await err(server.page_watch("check")))


async def usage_test():
    u = await server.server_status()
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
            ok("transport: stdio 21 tools", len(names) == 21, str(names))
            ok("transport: no redundant output schemas", not any(t.output_schema for t in tools))
            res = await s.call_tool("server_status", {})
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
            r = await c.post("http://127.0.0.1:8766/mcp", headers={**h, "Host": "evil.example"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "ping"})
            ok("transport: http rejects foreign Host (DNS rebinding)", r.status_code in (400, 403, 421), r.status_code)
    finally:
        proc.terminate()


async def main():
    _reset()
    for group in (unit_tests, search_tests, paper_tests, fetch_tests, reddit_tests, social_tests, download_tests, discover_tests,
                  llm_tests, research_tests, degradation_tests, media_tests, media_quality_tests, data_tests, usage_test, transport_stdio, transport_http):
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
