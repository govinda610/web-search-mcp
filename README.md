# web-search-mcp

Self-owned web search + fetch MCP server. Any MCP-capable coding agent (pi, Claude Code,
Cursor, Codex CLI) can use it. Runs fully locally: self-hosted SearXNG for search, local
extraction for pages and PDFs, a stealth browser for bot-protected sites. Cloud search APIs
are optional fallbacks, used only when local search fails, and every one is quota-tracked.

## Tools

| Tool | What it does |
|---|---|
| `web_search(query, num_results, strategy, include_domains, exclude_domains, recency, depth, more_queries, filetype, page, language, safesearch, answer, highlights, auto)` | `fallback` (local first) / `merge` / `exhaustive` (all providers parallel, interleaved, deduped). Domain lists, recency, file type, result page, language and safe-search filters. `more_queries`: up to 9 extra phrasings run in parallel. `depth="advanced"`: reads the top 5 pages and adds their most relevant passages. `answer`: LLM synthesis with [n] citations. `highlights`: key-fact bullets. `auto`: LLM picks strategy/recency/news routing. Results show publish dates when known |
| `news_search(query, num_results, recency)` | Recent news via SearXNG news vertical, Tavily as fallback |
| `suggest(query)` | Autocomplete suggestions (DuckDuckGo, free) |
| `image_search(query, num_results)` | Image results via SearXNG image vertical |
| `knowledge_search(query, sites, num_results)` | Straight from the sources' own APIs, in parallel: Wikipedia, Hacker News, Stack Overflow, GitHub repos, OpenReview, Hugging Face papers and models, Lemmy, packages (npm, crates.io, PyPI) |
| `live_data(kind, query)` | Stock quotes incl. NSE/BSE (Yahoo Finance), exchange rates (ECB via Frankfurter), crypto (CoinGecko), weather + 4-day forecast (Open-Meteo) |
| `paper_search(query, num_results, year_from, year_to)` | Papers from arXiv, Semantic Scholar, Google Scholar, PubMed, EuropePMC, OpenAIRE (via SearXNG): authors, venue, citations, DOI, PDF link |
| `paper_fetch(ref, save_dir, max_chars, start)` | arXiv id / DOI / URL → full text. DOIs resolve to open-access copies via OpenAlex, then Unpaywall. Optionally saves the PDF |
| `fetch_page(url, max_chars, start, query, extract, fresh)` | Page or PDF → markdown. Reddit/YouTube/X/Bluesky/Telegram/Instagram auto-route. `query`: only the most relevant passages. `extract`: LLM pulls structured JSON. Long documents are paged with `start` |
| `fetch_pages(urls, max_chars, query, concurrency)` | Up to 20 pages concurrently; a failed page shows why without sinking the batch |
| `site_map(url, num_results, path_filter)` | A site's pages from its sitemaps (robots.txt, sitemap.xml, nested indexes, .gz), or its front-page links when it has none |
| `reddit_fetch(target, sort, num_results)` | Post body + comments via Arctic Shift archive, subreddit feeds via throttled RSS. No key, ban-safe |
| `youtube_transcript(url, lang, max_chars, start)` | Captions/auto-generated transcripts with [m:ss] marks, paged |
| `social_fetch(target, num_results)` | Public posts and profiles without login: X/Twitter (fxtwitter, X embed API), Bluesky, Telegram channels, Instagram |
| `media_search(query, category, num_results, sites)` | Books, comics, manga/manhwa, anime, movies, TV/K-drama, games, audiobooks, music, podcasts, software, subtitles. Parallel across sources; returns what the title is and where to get it (magnets with seeders, md5s, direct downloads) |
| `book_download(md5, save_dir)` | Downloads a book/comic/paper by md5: LibGen, then Z-Library with a free account |
| `discover_sources()` | What's new or moved since the last check in Prowlarr's indexer list and FMHY's starred sites |
| `usage_status()` | Monthly usage per search provider, LLM call counts, current working mirror per site |

## How fetching works

1. **curl_cffi** with a Chrome TLS fingerprint (plain HTTP clients get 403s from Wikipedia, Medium, …)
2. **Camoufox** stealth Firefox, only when step 1 is blocked (401/403/429/503 or a challenge
   page) or gets a JavaScript app shell. Solves Cloudflare's JS challenge.
3. **Your own Chrome over the DevTools protocol**, only if `CHROME_CDP_URL` is set. The page
   opens in a new tab of a Chrome you started, with its logins, then the tab closes. Use it for
   sites that need an account (Instagram, X, LinkedIn) or reject Firefox. Start Chrome with a
   dedicated profile (Chrome refuses remote debugging on your default profile), log in once:
   ```bash
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
     --remote-debugging-port=9333 --user-data-dir="$HOME/.web-search-chrome"
   # .env: CHROME_CDP_URL=http://127.0.0.1:9333
   ```
4. **Camoufox in a visible window**, only when everything before it was blocked.
   DataDome (e.g. G2) detects headless browsers but lets a real window through. If the site
   shows a check that needs a person (DDoS-Guard's captcha on Anna's Archive, an "I'm not a
   robot" box), the window waits up to 2 minutes for you to solve it. The cookies it earns are
   saved to `state/browser-cookies.json` and reused by every later browser fetch, so each
   site's check is solved once. Set `FETCH_VISIBLE_BROWSER=0` to turn this stage off.
5. **Jina reader**, only if `JINA_API_KEY` is set.

If the connection itself fails (how an ISP block looks: timeouts, resets, DNS failures), the
whole chain runs again through Tor and the site is remembered as Tor-only for the session.

HTML → markdown with title/author/date metadata via trafilatura; PDF → text via pymupdf.
Real 404s and unresolvable domains fail immediately instead of escalating. Challenge pages
are never returned as content. Extracted text is cached for an hour in `state/cache/`.

## Media search

| Category | Sources |
|---|---|
| books | LibGen, Anna's Archive, Z-Library, Open Library, Project Gutenberg, Knaben (ebook torrents) |
| comics | LibGen comics, GetComics, Anna's Archive, Z-Library |
| manga | AniList, MangaUpdates, MangaDex, WeebCentral, Nyaa, LibGen |
| anime | AniList, SubsPlease, AnimeTosho, Nyaa, Knaben |
| movies | YTS, Knaben, The Pirate Bay, Torrents-CSV, LimeTorrents |
| tv | TVmaze, MyDramaList (Kuryana), EZTV (episodes: "show s01e02"), Kisskh, Knaben, The Pirate Bay, Torrents-CSV, LimeTorrents |
| subtitles | OpenSubtitles |
| audiobooks | iTunes, Internet Archive (incl. every LibriVox recording), AudioBookBay |
| music | iTunes, Internet Archive, Knaben, LimeTorrents |
| podcasts | iTunes |
| games | FitGirl and Internet Archive only: games run code on your machine, so no random uploaders |
| software | Internet Archive |
| torrents | Knaben, The Pirate Bay, Torrents-CSV, Nyaa, LimeTorrents |

Each source uses the site's lightest endpoint (JSON API, RSS, or its search page): one
request per search, at most one request per second per host, results cached for an hour.
Sites your ISP blocks are retried through Tor (`brew install tor && brew services start tor`;
`TOR_PROXY` overrides the default `socks5h://127.0.0.1:9050`).

**Domains that move.** Shadow libraries and trackers change domains often. `mirrors.py`
remembers which domain last worked and tries it first. When every known domain fails, it
refreshes the list (at most every 6 hours) from Prowlarr's indexer definitions (updated
almost daily) or from [SLUM](https://open-slum.org), the shadow-library uptime monitor. A new
domain is kept only if the adapter parses real results from it, so parked domains and
look-alike clones are rejected. State lives in `state/mirrors.json`.

**New sources.** `discover_sources` compares Prowlarr's indexer definitions and FMHY's starred
picks against the last check (`state/discovery.json`) and lists additions, removals and sites
whose domains changed.

**Z-Library.** Search works without an account. Downloads need a free account: set
`ZLIB_EMAIL` and `ZLIB_PASSWORD` in `.env`. The login is cached in `state/zlibrary-login.json`
(readable only by you) because Z-Library rate-limits logins.

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
Missing keys are skipped. Also optional: `ZLIB_EMAIL`/`ZLIB_PASSWORD` (Z-Library downloads),
`UNPAYWALL_EMAIL` (a contact address Unpaywall asks for; finds more open-access papers). LLM features (`answer`/`highlights`/`auto`) read coding-plan
credentials from `~/.pi/agent/models.json` (zai → qwen → minimax chain); if none respond,
searches return plain result lists.

## For agents

The server sends a short "which tool when" guide as MCP instructions. Every parameter has a
description, fixed choices are enums (`strategy`, `recency`, `depth`, `category`, `sort`,
`sites`), numbers have ranges, and tools carry read-only / writes-files hints so clients
can auto-approve the safe ones. Failed calls are MCP errors (`isError`) with a sentence saying
what went wrong; batch tools report each failed item inline instead. Slow tools (`fetch_page`,
`fetch_pages`, `web_search` with `depth="advanced"`) send progress notifications.

**Safety.** Page content can contain prompt injections, so the fetch tools only open public
`http(s)` URLs: `file://` and addresses on this machine or network (SearXNG, the Chrome debug
port, your router) are refused. Set `FETCH_ALLOW_PRIVATE=1` to allow private addresses.
Downloads are saved under their own file name only, inside `save_dir`, never over an existing
file. Batch reads never open a visible browser window and give each page at most 45 seconds.

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
