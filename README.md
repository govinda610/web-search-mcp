# web-search-mcp

Self-owned web search + fetch MCP server. Any MCP-capable coding agent (pi, Claude Code,
Cursor, Codex CLI) can use it. Runs fully locally: self-hosted SearXNG for search, local
extraction for pages and PDFs, a stealth browser for bot-protected sites. Cloud search APIs
are optional fallbacks, used only when local search fails, and every one is quota-tracked.

## Tools

| Tool | What it does |
|---|---|
| `web_search(query, num_results, strategy, include_domains, exclude_domains, recency, answer, highlights, auto)` | `fallback` (local first) / `merge` / `exhaustive` (all providers parallel, deduped). Domain + recency filters. `answer`: LLM synthesis with [n] citations. `highlights`: key-fact bullets. `auto`: LLM picks strategy/recency/news routing |
| `news_search(query, num_results, recency)` | Recent news via SearXNG news vertical, Tavily as fallback |
| `suggest(query)` | Autocomplete suggestions (DuckDuckGo, free) |
| `image_search(query, num_results)` | Image results via SearXNG image vertical |
| `paper_search(query, num_results, year_from)` | Papers from arXiv, Semantic Scholar, Google Scholar, PubMed, EuropePMC, OpenAIRE (via SearXNG): authors, venue, citations, DOI, PDF link |
| `paper_fetch(ref, save_dir, max_chars, start)` | arXiv id / DOI / URL → full text. DOIs resolve to open-access copies via OpenAlex. Optionally saves the PDF |
| `fetch_page(url, max_chars, start)` | Page or PDF → markdown. Reddit/YouTube auto-route. Long documents are paged with `start` |
| `fetch_pages(urls, concurrency, max_chars_each)` | Concurrent multi-page fetch |
| `reddit_fetch(target, sort, limit)` | Post body + comments via Arctic Shift archive, subreddit feeds via throttled RSS. No key, ban-safe |
| `youtube_transcript(url, lang)` | Captions/auto-generated transcripts |
| `usage_status()` | Monthly usage per search provider + LLM call counts |

## How fetching works

1. **curl_cffi** with a Chrome TLS fingerprint (plain HTTP clients get 403s from Wikipedia, Medium, …)
2. **Camoufox** stealth Firefox, only when step 1 is blocked (401/403/429/503 or a challenge
   page) or gets a JavaScript app shell. Solves Cloudflare's JS challenge.
3. **Camoufox in a visible window**, only when the headless browser is still blocked.
   DataDome (e.g. G2) detects headless browsers but lets a real window through, so a
   Firefox window appears for a few seconds. Set `FETCH_VISIBLE_BROWSER=0` to turn it off.
4. **Jina reader**, only if `JINA_API_KEY` is set.

HTML → markdown with title/author/date metadata via trafilatura; PDF → text via pymupdf.
Real 404s and unresolvable domains fail immediately instead of escalating. Challenge pages
are never returned as content. Extracted text is cached for an hour in `state/cache/`.

Known limit: pages behind a login (Instagram, LinkedIn) need a logged-in browser such as
agent-browser.

## Setup

```bash
uv sync                                   # creates .venv, installs deps
uv run python -m camoufox fetch           # one-time: downloads the stealth browser
cp deploy/searxng/settings.yml.example deploy/searxng/settings.yml   # set secret_key
docker compose -f deploy/docker-compose.yml up -d   # SearXNG on 127.0.0.1:8888
cp .env.example .env                      # SEARXNG_URL is the only required value
```

SearXNG's defaults leave general search depending on DuckDuckGo alone. The example settings
enable Google, Bing, Yahoo and Mojeek too, so one upstream throttling you doesn't take search down.

Optional keys in `.env`: `EXA_API_KEY`, `TAVILY_API_KEY`, `FIRECRAWL_API_KEY`, `JINA_API_KEY`.
Missing keys are skipped. LLM features (`answer`/`highlights`/`auto`) read coding-plan
credentials from `~/.pi/agent/models.json` (zai → qwen → minimax chain); if none respond,
searches return plain result lists.

## Transports (MCP SDK v2)

```bash
uv run server.py                          # stdio (per-harness subprocess)
MCP_TRANSPORT=http uv run server.py       # ONE shared instance on 127.0.0.1:8765/mcp
```

Prefer HTTP mode when running multiple agents: one process = shared rate limiter,
cache, and quota ledger across all of them.

## Wiring agents

```bash
# Claude Code (all projects)
claude mcp add -s user web-search -- ~/.local/bin/uv --directory /path/to/web-search-mcp run server.py
```

pi (`~/.pi/agent/mcp.json`), Cursor (`.cursor/mcp.json`) and Codex (`~/.codex/config.toml`)
take the same command and args.

## Testing

```bash
uv run test_stack.py          # local providers only, no paid quota used
uv run test_stack.py --paid   # also exercises the cloud providers
```

## Design notes

- Provider order, monthly limits, merge set: `config.json`
- Usage ledger: `state/usage-YYYY-MM.json`
- SearXNG binds to loopback only, JSON API enabled, limiter off
- Add a search provider: write `async def search_x(...)` in `providers.py`, register it in
  `REGISTRY`, add it to `config.json`
