# web-search-mcp

Self-owned web search + fetch MCP server. Any MCP-capable coding agent (pi, Claude Code,
Cursor, Codex CLI) can use it. Runs fully locally: self-hosted SearXNG for search, local
extraction for pages and PDFs, a stealth browser for bot-protected sites. Cloud search APIs
are optional fallbacks, used only when local search fails, and every one is quota-tracked.

## Tools

| Tool | What it does |
|---|---|
| `web_search(query, num_results, strategy, include_domains, exclude_domains, recency, depth, more_queries, filetype, page, language, safesearch, max_chars, answer, similar_to)` | `fallback` (local first) / `merge` / `exhaustive` (all providers parallel, interleaved, deduped by URL and by title + snippet). Domain lists, recency, file type, result page, language or region (`en-IN`) and safe-search filters. `more_queries`: up to 9 extra phrasings run in parallel. `depth="advanced"`: reads the top 5 pages and adds their most relevant passages. `max_chars`: output budget; results past it are counted, not shown. `answer`: LLM synthesis with [n] citations. `similar_to`: pages like a given URL, from other sites (its title and key terms searched, reranked against its opening text). Results are numbered, with publish dates when known |
| `news_search(query, num_results, recency, trends)` | Recent news from SearXNG's news engines and Google News in parallel (Google's redirect links resolved to the publisher's URL), Tavily as fallback. `trends`: daily coverage volume plus articles from GDELT |
| `image_search(query, num_results)` | Image results via SearXNG image vertical |
| `knowledge_search(query, sites, num_results)` | Straight from the sources' own APIs, in parallel: Wikipedia, Hacker News, Stack Overflow, GitHub repos, OpenReview, Hugging Face papers and models, Lemmy, Wikidata (structured facts), packages (npm, crates.io, PyPI), code (grep.app, across ~1M GitHub repos), Wiktionary, ClinicalTrials.gov, openFDA drug labels, CourtListener (US case law), US patents (needs `USPTO_ODP_API_KEY`), and `history`: every page this server has fetched, from a local SQLite full-text index |
| `live_data(kind, query)` | Stock quotes incl. NSE/BSE (Yahoo Finance), exchange rates (ECB via Frankfurter), crypto (CoinGecko), weather + 4-day forecast (Open-Meteo), economic indicators (`India GDP growth`, `US inflation`; World Bank, or any FRED series with `FRED_API_KEY`), places (`cafe near Koramangala, Bangalore`, OpenStreetMap), SEC filings (`AAPL 10-K`, EDGAR, needs `SEC_USER_AGENT`) |
| `paper_search(query, num_results, year_from, year_to)` | Papers from arXiv, Semantic Scholar, Google Scholar, PubMed, EuropePMC, OpenAIRE (via SearXNG), plus Crossref, bioRxiv/medRxiv and CORE (with `CORE_API_KEY`) in parallel, merged by title: authors, venue, citations, DOI, PDF link |
| `paper_fetch(ref, save_dir, max_chars, start)` | arXiv id / DOI / URL → full text. DOIs resolve to open-access copies via OpenAlex, then Unpaywall, then Anna's Archive SciDB and LibGen. Optionally saves the PDF |
| `fetch_page(url, max_chars, start, query, extract, max_age, method, as_of)` | Page or PDF → markdown. Reddit/YouTube/X/Bluesky/Telegram/Instagram auto-route. `query`: only the most relevant passages, ranked by a small local cross-encoder. `extract`: LLM pulls structured JSON. `max_age`: oldest cached copy to accept in seconds (`0` = live). `method`: force one stage (`plain`, `browser`, `chrome`, `tor`, `archive`). `as_of` (`2019`, `2019-06`, `2019-06-01`): the Wayback Machine copy closest to that date. Long documents are paged with `start` |
| `fetch_pages(urls, max_chars, query, extract, concurrency)` | Up to 20 pages concurrently; a failed page shows why without sinking the batch. `extract` pulls the same fields out of every page |
| `site_map(url, num_results, path_filter)` | A site's pages from its sitemaps (robots.txt, sitemap.xml, nested indexes, .gz), or its front-page links when it has none, topped up from Common Crawl's URL index when those are few |
| `crawl_site(url, query, num_pages, max_depth, path_filter, max_chars_each)` | Reads up to 100 pages of one site (sitemap first, then links, breadth-first, staying under the start path) and returns each page's passages most relevant to `query` |
| `page_history(url, num_results, year_from, year_to)` | A page's Wayback Machine snapshots, newest first, one per distinct version |
| `page_watch(action, url, query)` | `check` a page: the first call saves it, later calls show a diff of what changed. `list` and `forget` |
| `youtube_transcript(url, lang, max_chars, start)` | Captions/auto-generated transcripts with [m:ss] marks, paged |
| `social_fetch(target, num_results, sort)` | Public posts and profiles without login. Reddit posts + comments via the Arctic Shift archive and subreddit feeds (`r/name`) via throttled RSS, ban-safe; X/Twitter (fxtwitter, X embed API), Bluesky, Telegram channels, Instagram |
| `media_search(query, category, num_results, sites)` | Books, comics, manga/manhwa, anime, movies, TV/K-drama, games, audiobooks, music, podcasts, software, subtitles. Parallel across sources; returns what the title is and where to get it (magnets with seeders, md5s, direct downloads) |
| `book_download(md5, save_dir)` | Downloads a book/comic/paper by md5: LibGen, then Z-Library with a free account |
| `media_download(url, format, max_height, save_dir, playlist, subtitles)` | Video or audio from YouTube and ~1800 other sites (yt-dlp + ffmpeg): mp4 (H.264, plays everywhere), mkv, webm, or audio only as mp3, m4a, opus, flac, wav. Resolution cap, playlists (first 50 items), embedded English subtitles, progress notifications. A magnet link from `media_search` downloads the torrent with aria2c, without seeding afterwards; one that stalls for 5 minutes is abandoned |
| `release_watch(action, query, category, min_quality)` | Watches for a movie/show/anime release at or above a quality (`WEB-DL` by default, so cam copies don't count) and sends a macOS notification when one appears. Checked every `WATCH_INTERVAL_HOURS` (6) by the HTTP server, or on `action="check"` |
| `deep_research(question, depth, sub_questions, report)` | Multi-round research: plans sub-queries (or takes yours in `sub_questions`), searches, reads the best pages, notes gaps, searches again, then writes a report citing every claim as [n]. `report=False` returns the sources and their best passages without the write-up, for an agent that writes its own. `standard` ~4 min / 8 sources, `deep` ~8 min / 16 |
| `server_status(check_new_sources)` | Monthly usage per search provider, LLM call counts, current working mirror per site, sources skipped after repeated failures. `check_new_sources`: what's new or moved in Prowlarr's indexer list and FMHY's starred sites since the last check |

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
6. **archive.today, then the Wayback Machine**, when every live stage fails or the page is a
   paywall teaser (a short page with "subscribe to continue reading" or similar).

`fetch_page(method=...)` runs one of these on its own when you know what a site needs:
`plain` (1), `browser` (2 and 4), `chrome` (3), `tor` (1 through Tor), `archive` (6).

If the connection itself fails (how an ISP block looks: timeouts, resets, DNS failures), the
whole chain runs again through Tor and the site is remembered as Tor-only for the session.

HTML → markdown with title/author/date metadata via trafilatura; PDF → text via pymupdf.
Real 404s and unresolvable domains fail immediately instead of escalating. Challenge pages
are never returned as content. Extracted text is cached for an hour in `state/cache/`
(`max_age` narrows that per call) and added to a full-text index, `state/index.db` (SQLite
FTS5, newest 20,000 pages), which `knowledge_search(sites=["history"])` searches offline.

## Media search

| Category | Sources |
|---|---|
| books | LibGen, Anna's Archive, Z-Library, Open Library, Project Gutenberg, Knaben (ebook torrents) |
| comics | LibGen comics, GetComics, Anna's Archive, Z-Library |
| manga | AniList, MangaUpdates, MangaDex, WeebCentral, Nyaa, LibGen |
| anime | AniList, SubsPlease, AnimeTosho, Nyaa, Knaben |
| movies | IMDb (what the title is, runtime, where it's streaming), YTS, Knaben, The Pirate Bay, Torrents-CSV, LimeTorrents, 1337x, Torrentio, your Prowlarr/Jackett |
| tv | IMDb, TVmaze, MyDramaList (Kuryana), EZTV (episodes: "show s01e02"), Kisskh, Knaben, The Pirate Bay, Torrents-CSV, LimeTorrents, 1337x, Torrentio, your Prowlarr/Jackett |
| subtitles | OpenSubtitles |
| audiobooks | iTunes, Internet Archive (incl. every LibriVox recording), AudioBookBay |
| music | iTunes, Internet Archive, Knaben, LimeTorrents |
| podcasts | iTunes |
| games | FitGirl and Internet Archive only: games run code on your machine, so no random uploaders |
| software | Internet Archive |
| torrents | Knaben, The Pirate Bay, Torrents-CSV, Nyaa, LimeTorrents, 1337x, your Prowlarr/Jackett |

Each source uses the site's lightest endpoint (JSON API, RSS, or its search page): one
request per search, at most one request per second per host, results cached for an hour.
Sites your ISP blocks are retried through Tor (`brew install tor && brew services start tor`;
`TOR_PROXY` overrides the default `socks5h://127.0.0.1:9050`).

**Domains that move.** Shadow libraries and trackers change domains often. `mirrors.py`
remembers which domain last worked and tries it first. When every known domain fails, it
refreshes the list (at most every 6 hours) from Prowlarr's indexer definitions (updated
almost daily), [SLUM](https://open-slum.org), the shadow-library uptime monitor, or
annas-archive.info for Anna's Archive. A new
domain is kept only if the adapter parses real results from it, so parked domains and
look-alike clones are rejected. State lives in `state/mirrors.json`.

**Quality.** Video results are tagged with a release tier (BluRay REMUX > BluRay > WEB-DL > WEBRip > HDTV/DVD > unknown > screener/telecine > telesync > cam) and sorted best first. Cinema recordings get their own section, and fakes (`.exe` files, password-protected archives, files far too small for the runtime) carry warnings. The same release re-uploaded under a slightly different name is shown once (fuzzy title match within the same year, episode and resolution), noting which other sources carry it. A film or show search that finds nothing downloadable is retried without the year, then under an alternate title. Book downloads are checked against their md5 and rejected if the server returns a login page instead.

**Your own indexers.** Set `PROWLARR_URL`/`PROWLARR_API_KEY` or `JACKETT_URL`/`JACKETT_API_KEY` and their results join movies, TV and torrent searches.

**New sources.** `server_status(check_new_sources=True)` compares Prowlarr's indexer definitions and FMHY's starred
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
enable Google, Bing, Yahoo, Mojeek, Yep and Mwmbl too, so one upstream throttling you doesn't
take search down, plus Crossref, OpenAlex and Open Library for papers and books.

Optional keys in `.env`: `EXA_API_KEY`, `TAVILY_API_KEY`, `FIRECRAWL_API_KEY`, `JINA_API_KEY`.
Missing keys are skipped. Also optional: `ZLIB_EMAIL`/`ZLIB_PASSWORD` (Z-Library downloads),
`UNPAYWALL_EMAIL` (a contact address Unpaywall asks for; finds more open-access papers), `SEC_USER_AGENT`
(`"Your Name you@example.com"`, which EDGAR requires), `WATCH_COUNTRY` (where `media_search` checks
legal streaming, default `IN`), `WATCH_INTERVAL_HOURS`, the Prowlarr/Jackett pairs above, and free
keys that each switch on one source: `FRED_API_KEY`, `USPTO_ODP_API_KEY`, `COURTLISTENER_TOKEN`
(higher rate limit only), `CORE_API_KEY`.
`media_download` needs `ffmpeg` (`brew install ffmpeg`), and `aria2` for magnet links. LLM features (`answer`, `extract`, `deep_research`) read coding-plan
credentials from `~/.pi/agent/models.json` (zai → qwen → minimax chain); if none respond,
searches return plain result lists.

## For agents

The server sends a short "which tool when" guide as MCP instructions. Every parameter has a
description, fixed choices are enums (`strategy`, `recency`, `depth`, `category`, `sort`,
`sites`), numbers have ranges, and tools carry read-only / writes-files hints so clients
can auto-approve the safe ones. Failed calls are MCP errors (`isError`) with a sentence saying
what went wrong; batch tools report each failed item inline instead. Slow tools (`fetch_page`,
`fetch_pages`, `web_search` with `depth="advanced"`, `crawl_site`, `deep_research`,
`media_download`) send progress notifications. When the client supports MCP sampling, LLM steps
(`answer`, `extract`, `deep_research`) use the client's own model instead of the coding-plan chain.

Three prompts are included: `literature_review(topic, years)`, `company_dossier(company)` and
`compare_options(options, criteria)`.

**Safety.** Page content can contain prompt injections, so the fetch tools only open public
`http(s)` URLs: `file://` and addresses on this machine or network (SearXNG, the Chrome debug
port, your router) are refused. Set `FETCH_ALLOW_PRIVATE=1` to allow private addresses.
Downloads are saved under their own file name only, inside `save_dir`, never over an existing
file, and `save_dir` must be inside your home folder but not a hidden folder or `~/Library`
(so a page can't talk an agent into writing to `~/.ssh` or a LaunchAgent). Set
`DOWNLOAD_ALLOW_ANY_DIR=1` to lift that. Batch reads never open a visible browser window and give each page at most 45 seconds.

## Transports (MCP SDK v2)

```bash
uv run server.py                          # stdio (per-harness subprocess)
MCP_TRANSPORT=http uv run server.py       # ONE shared instance on 127.0.0.1:8765/mcp
```

Prefer HTTP mode when running multiple agents: one process = shared rate limiter,
cache, and quota ledger across all of them. It listens on loopback only and rejects requests
whose `Host` isn't localhost, so a web page can't reach it through DNS rebinding.

### Start at login (macOS)

Save as `~/Library/LaunchAgents/com.web-search-mcp.plist` (fix the two paths), then
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.web-search-mcp.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.web-search-mcp</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/.local/bin/uv</string><string>--directory</string>
        <string>/path/to/web-search-mcp</string><string>run</string><string>server.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MCP_TRANSPORT</key><string>http</string>
        <key>MCP_PORT</key><string>8765</string>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>StandardOutPath</key><string>/tmp/web-search-mcp.log</string>
    <key>StandardErrorPath</key><string>/tmp/web-search-mcp.log</string>
</dict>
</plist>
```

`PATH` must reach ffmpeg, deno and tor. After pulling changes:
`launchctl kickstart -k gui/$(id -u)/com.web-search-mcp`.

## Search page

In HTTP mode the same server also serves a search page for people at `http://127.0.0.1:8765/`.
The tools merge their sources into one answer for an agent; the page shows what every source
returned on its own, as each one finishes, next to the merged list.

- **Tabs.** Web (any SearXNG category: general, news, images, videos, science, IT, files, music,
  social media), Papers, Knowledge and Media (movies, TV, books, anime, games, torrents…).
- **One panel per source.** Each SearXNG engine, knowledge source, paper source and media site
  gets its own panel. The status bar shows each one's result count and time, or why it failed
  ("google: Suspended: CAPTCHA", "duckduckgo: timeout") or that it's being skipped for failing repeatedly.
- **Pick sources.** Click a source to leave it out; the choice is remembered per tab and category.
- **Merged view.** For Media, the tool's own merge: quality grades, fake and cinema-recording
  warnings, and near-duplicate releases shown once. A film with nothing downloadable offers the
  year-less and alternate-title searches as buttons.
- **Filter and sort** without searching again: text, minimum seeders, resolution, hide cinema
  recordings or flagged results; sort by seeders, size, date or title.
- **Actions.** Read a page or PDF as clean text (same fetching as `fetch_page`), copy a magnet,
  download a torrent, book or video/audio (same as the download tools), with progress.
- Searches go into the address bar, so back/forward and bookmarks work; `/` focuses the search box.

It answers only as `127.0.0.1`/`localhost`, and its API calls need a header a cross-site page
can't send without a CORS preflight, which these routes never answer, so a web page you visit
can't make your server search or download things.

## Wiring agents

```bash
# Claude Code (all projects), shared HTTP instance
claude mcp add -s user --transport http web-search http://127.0.0.1:8765/mcp
# or one private subprocess per session
claude mcp add -s user web-search -- ~/.local/bin/uv --directory /path/to/web-search-mcp run server.py
```

pi (`~/.pi/agent/mcp.json`), Cursor (`.cursor/mcp.json`) and Codex (`~/.codex/config.toml`)
take the same URL, or the same command and args.

## Testing

```bash
uv run test_stack.py          # local providers only, no paid quota used
uv run test_stack.py --paid   # also exercises the cloud providers
```

`eval.py` measures answer quality on public benchmarks, graded by an LLM judge: SimpleQA short
facts through `web_search(answer=True)`, FRAMES multi-hop questions through `deep_research`.
A fixed seed picks the same questions every run, so scores compare across changes; per-question
results go to `state/eval/`.

```bash
uv run eval.py simpleqa 50
uv run eval.py frames 20
```

## Design notes

- Provider order, monthly limits, merge set: `config.json`
- Usage ledger: `state/usage-YYYY-MM.json`
- SearXNG binds to loopback only, JSON API enabled, limiter off
- Add a search provider: write `async def search_x(...)` in `providers.py`, register it in
  `REGISTRY`, add it to `config.json`
