# web-search-mcp

Self-owned web search + fetch MCP server. Any MCP-capable coding agent (pi, Claude Code,
Cursor, Codex CLI) can use it. SearXNG (self-hosted, unlimited) is the primary engine;
cloud search APIs are fallbacks and result-diversity boosters. Every provider is
quota-tracked.

## Tools

| Tool | What it does |
|---|---|
| `web_search(query, num_results, strategy, include_domains, exclude_domains, recency)` | `fallback`: first working provider (SearXNG → DuckDuckGo → Exa → Tavily → Firecrawl → Jina). `merge`: top 3 in parallel, deduped, max 2 per domain, source-tagged. Domain filters are comma-separated substrings; recency: `day\|week\|month\|year` (provider-native) |
| `news_search(query, num_results, recency)` | Recent news via SearXNG news vertical, Tavily news topic as fallback |
| `suggest(query)` | Autocomplete suggestions (DuckDuckGo, free, no key) |
| `fetch_page(url)` | Smart router: Reddit/YouTube auto-delegate; generic pages via direct → curl_cffi (TLS-impersonated) → Jina reader; JS-shell soft-failure escalation; 1h cache |
| `reddit_fetch(target, sort, limit)` | Subreddit feeds + posts with comments. Arctic Shift archive (primary) → Reddit RSS behind 60s throttle + 5-min cache + Retry-After backoff (no key, ban-safe) |
| `youtube_transcript(url, lang)` | Captions/auto-generated transcripts (free) |
| `usage_status()` | This month's usage vs per-provider limits |

## Setup

```bash
uv sync                                   # creates .venv, installs deps
cp .env.example .env                      # then fill in keys (or sync from shell)
docker compose -f deploy/docker-compose.yml up -d   # SearXNG on 127.0.0.1:8888
uv run server.py                          # MCP stdio server
```

### Required env vars (`.env`, never committed)

`SEARXNG_URL`, `JINA_API_KEY`, `EXA_API_KEY`, `TAVILY_API_KEY`, `FIRECRAWL_API_KEY`,
optional: `SERPDIVE_API_KEY`, `QUERIT_API_KEY`, `TINYFISH_API_KEY`, `GEMINI_API_KEY`,
`CRAWL4AI_URL`, `CRAWL4AI_TOKEN`.

Only DuckDuckGo works with zero keys; everything else degrades gracefully when a key is
missing.

## Wiring agents

```bash
# Claude Code
claude mcp add web-search -- /Users/$USER/.local/bin/uv --directory $HOME/dev-path/web-search-mcp run server.py
```

pi (`~/.pi/agent/mcp.json`), Cursor (`.cursor/mcp.json`), Codex (`~/.codex/config.toml`)
use the same command/args shape — see `mcp/` snippets.

## Design notes

- Provider order, monthly limits, merge set: `config.json`
- Usage ledger: `state/usage-YYYY-MM.json` (human-readable)
- SearXNG binds to loopback only; JSON API enabled; limiter off (loopback is private)
- Add a provider: implement `async def search_x(...)` in `providers.py` + register in
  `REGISTRY` + add to `config.json`
