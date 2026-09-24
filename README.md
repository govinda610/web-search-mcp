# web-search-mcp

Self-owned web search + fetch MCP server. Any MCP-capable coding agent (pi, Claude Code,
Cursor, Codex CLI) can use it. SearXNG (self-hosted, unlimited) is the primary engine;
cloud search APIs are fallbacks and result-diversity boosters. Every provider is
quota-tracked.

## Tools

| Tool | What it does |
|---|---|
| `web_search(query, num_results, strategy, include_domains, exclude_domains, recency, answer, highlights, auto)` | `fallback`/`merge`/`exhaustive` (all providers parallel). Domain+recency filters. `answer`: LLM synthesis with [n] citations. `highlights`: key-fact bullets. `auto`: LLM picks strategy/recency/news routing. LLM = coding-plan models, degrades to plain results if unavailable |
| `news_search(query, num_results, recency)` | Recent news via SearXNG news vertical, Tavily news topic as fallback |
| `suggest(query)` | Autocomplete suggestions (DuckDuckGo, free, no key) |
| `image_search(query, num_results)` | Image results via SearXNG image vertical (self-hosted, unlimited) |
| `fetch_page(url)` | Smart router: Reddit/YouTube auto-delegate; direct → curl_cffi → Jina with JS-shell escalation; 1h cache |
| `fetch_pages(urls, concurrency)` | Concurrent multi-page fetch (semaphore-limited) |
| `reddit_fetch(target, sort, limit)` | Arctic Shift archive (primary) → RSS behind throttle/cache/backoff. No key, ban-safe |
| `youtube_transcript(url, lang)` | Captions/auto-generated transcripts (free) |
| `instagram_fetch(url)` | Best-effort public IG (IG gates anonymous hard — see note) |
| `usage_status()` | Monthly usage: search providers + LLM call counts |

## Transports (MCP SDK v2)

```bash
uv run server.py                          # stdio (per-harness subprocess)
MCP_TRANSPORT=http uv run server.py       # ONE shared instance on 127.0.0.1:8765/mcp
```

Prefer HTTP mode when running multiple agents: one process = shared rate-limiter,
cache, and quota ledger across all of them. Point any MCP client at
`http://127.0.0.1:8765/mcp` (streamable HTTP). SDK 2.2.0 implements spec 2025-11-25.

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
missing. LLM features (`answer`/`highlights`/`auto`) read coding-plan credentials from
`~/.pi/agent/models.json` (zai → qwen → minimax chain) — no extra keys needed; if all
models fail, searches proceed as plain result lists.

## Testing

```bash
uv run test_stack.py   # 43 checks: transports, all tools, routing, degradation
```

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
