"""Thorough stack verification: pure logic, all 10 tools, routing, transports,
graceful degradation. Run: uv run test_stack.py  (live network; increments quota ledger)"""
import asyncio
import os
import shutil
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
import llm  # noqa: E402
import providers  # noqa: E402
import quota  # noqa: E402
import server  # noqa: E402
import sources  # noqa: E402

RESULTS = []


def ok(name, cond, detail=""):
    RESULTS.append((name, bool(cond), str(detail)[:110]))


def _reset():
    shutil.rmtree(server.CACHE, ignore_errors=True)
    sources._rss_cache.clear()
    sources._rss_last_hit = 0.0


async def unit_tests():
    ok("logic: domain include", server._domain_ok("https://a.reddit.com/x", "reddit.com", ""))
    ok("logic: domain exclude", not server._domain_ok("https://a.reddit.com/x", "", "reddit.com"))
    ok("logic: recency searxng", server._recency_opts("searxng", "week") == {"time_range": "week"})
    ok("logic: recency firecrawl", server._recency_opts("firecrawl", "day") == {"tbs": "qdr:d"})
    ok("logic: recency unsupported", server._recency_opts("duckduckgo", "day") == {})
    ok("logic: recency invalid", server._recency_opts("searxng", "bogus") == {})
    ok("logic: classify reddit", sources.classify("https://reddit.com/r/x") == "reddit")
    ok("logic: classify youtube", sources.classify("https://youtu.be/abc") == "youtube")
    ok("logic: classify generic", sources.classify("https://example.com") == "generic")
    ok("logic: yt id parse", sources._yt_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ")
    ok("logic: quota unlimited", quota.remaining("searxng", None) is None)
    ok("logic: quota math", quota.remaining("exa", 1000) is None or 0 <= quota.remaining("exa", 1000) <= 1000)


async def search_tests():
    r = await server.web_search("valheim 1.0 seeds", 5)
    ok("search: fallback strategy", len(r) > 100 and "[" in r, r[:50])
    r = await server.web_search("valheim 1.0 seeds", 6, strategy="merge")
    ok("search: merge strategy", len(r) > 100, r[:50])
    r = await server.web_search("quantum computing 2026 results", 8, strategy="exhaustive")
    ok("search: exhaustive strategy", len(r) > 200, r[:50])
    r = await server.web_search("valheim seeds", 5, include_domains="reddit.com")
    ok("search: include_domains", "reddit.com" in r, r[:60])
    r = await server.web_search("valheim seeds", 5, exclude_domains="reddit.com")
    ok("search: exclude_domains", "reddit.com" not in r, r[:60])
    r = await server.web_search("openai news", 4, recency="week")


async def search_tests2():
    r = await server.web_search("openai news", 4, recency="week")
    ok("search: recency param", len(r) > 50, r[:50])
    r = await server.suggest("valheim see")
    ok("search: suggest", "valheim" in r, r[:40])
    r = await server.news_search("claude anthropic", 3, "month")
    ok("search: news_search", len(r) > 50, r[:50])
    r = await server.image_search("valheim world", 4)
    ok("search: image_search", "img:" in r, r[:60])
    r = await server.web_search("who won nobel physics 2025", 5, answer=True)
    ok("llm: answer synthesis", "ANSWER" in r, r[:60])
    r, tries = "", 0
    while "HIGHLIGHTS" not in r and tries < 3:  # coding-plan models may hiccup; degrade is by design
        r = await server.web_search("best programming language 2026", 5, highlights=True)
        tries += 1
    ok("llm: highlights", "HIGHLIGHTS" in r, r[:80] if "HIGHLIGHTS" in r else "results-only (LLM skipped x3)")
    r = await server.web_search("tesla stock news this week", 5, auto=True)
    ok("llm: auto routing", len(r) > 50, r[:60])


async def fetch_tests():
    r = await server.fetch_page("https://example.com")
    ok("fetch: example.com direct", r.startswith("(via direct)"), r[:40])
    r2 = await server.fetch_page("https://example.com")
    ok("fetch: cache hit", r2.startswith("(cached)"), r2[:30])
    r = await server.fetch_page("https://youtu.be/dQw4w9WgXcQ")
    ok("fetch: routes youtube", "segments" in r, r[:50])
    r = await server.fetch_page("https://www.reddit.com/r/commandline/comments/1woks8t/")
    ok("fetch: routes reddit", r.startswith("(reddit)"), r[:60])
    r = await server.fetch_page("https://vibehackers.io/blog/best-terminal-for-mac")
    first = r.splitlines()[0]
    ok("fetch: JS-shell escalation", r.startswith("(via") and "direct" not in first
       and len(r) > 1000, first[:50])
    r = await server.fetch_pages("https://example.com, https://httpbin.org/html", 2)
    ok("fetch: fetch_pages multi", all(f"===== {u}" in r for u in ("https://example.com", "https://httpbin.org/html")), r[:60])


async def reddit_tests():
    r = await sources.reddit_fetch("https://www.reddit.com/r/commandline/comments/1woks8t/")
    ok("reddit: arctic post+comments", "arctic-shift" in r and "[u/" in r, r[:60])
    r1 = await sources.reddit_fetch("r/valheim", limit=2)
    ok("reddit: subreddit RSS feed", len(r1) > 30, r1[:50])
    t = time.time()
    r2 = await sources.reddit_fetch("r/valheim", limit=2)
    ok("reddit: second call cached", r2 == r1 and time.time() - t < 1.0, f"{time.time()-t:.2f}s")


async def instagram_test():
    r = await server.instagram_fetch("https://www.instagram.com/nasa/")
    ok("instagram: best-effort returns", isinstance(r, str) and len(r) > 10, r[:50])



async def degradation_tests():
    orig = server.ENV["SEARXNG_URL"]
    server.ENV["SEARXNG_URL"] = "http://127.0.0.1:1"
    r = await server.web_search("valheim seeds", 3)
    server.ENV["SEARXNG_URL"] = orig
    ok("degrade: searxng-down falls through", "failed" not in r[:30].lower(), r[:60])
    op = llm._providers
    llm._providers = lambda: []
    r = await server.web_search("valheim seeds", 3, answer=True, highlights=True, auto=True)
    llm._providers = op
    ok("degrade: llm-down graceful", "ANSWER" not in r and len(r) > 50, r[:60])
    saved = dict(providers.REGISTRY)
    providers.REGISTRY.clear()
    r = await server.web_search("x", 3)
    providers.REGISTRY.update(saved)
    ok("degrade: no providers message", "No search providers" in r, r[:60])
    ok("degrade: limit=0 means skip", quota.remaining("exa", 0) == 0)
    r = await server.fetch_page("https://this-domain-really-does-not-exist-93847.com/")
    ok("degrade: fetch bad domain message", "failed" in r.lower(), r[:60])
    r = await server.youtube_transcript("https://youtu.be/aiqDb3abSsc")
    ok("degrade: youtube captionless message", isinstance(r, str), r[:60])


async def usage_test():
    u = server.usage_status()
    ok("usage: provider table", "provider" in u and "searxng" in u, u[:40])
    ok("usage: llm section present", "LLM calls" in u, u[-50:].replace(chr(10), " | "))



async def transport_stdio():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=["server.py"], cwd=os.getcwd())
    async with stdio_client(params) as (rw, ww):
        async with ClientSession(rw, ww) as s:
            await s.initialize()
            names = sorted(t.name for t in (await s.list_tools()).tools)
            ok("transport: stdio 10 tools", len(names) == 10, str(names))
            res = await s.call_tool("usage_status", {})
            ok("transport: stdio call works", "provider" in res.content[0].text)


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
    await unit_tests()
    await search_tests()
    await search_tests2()
    await fetch_tests()
    await reddit_tests()
    await instagram_test()
    await degradation_tests()
    await transport_stdio()
    await transport_http()
    n_ok = sum(1 for _, p, _ in RESULTS if p)
    print(f"\n{'=' * 76}")
    for name, passed, detail in RESULTS:
        print(f"{name:<38}{'PASS' if passed else 'FAIL':<7}{detail}")
    print(f"{'=' * 76}\n{n_ok}/{len(RESULTS)} PASSED")
    sys.exit(0 if n_ok == len(RESULTS) else 1)


asyncio.run(main())
