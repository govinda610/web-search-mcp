# Task: Fix two bugs in the web-search MCP server

You are working on a Python MCP server at `/Users/govindmittal/datascience-setup/web-search-mcp/`
(entry point: `server.py`, fetchers in `sources.py`, providers in `providers.py`, quotas in `quota.py`).
Run/test it with `uv` (see `pyproject.toml`). Back up any file before editing it.
Keep changes minimal and match existing code style. Do not refactor unrelated code.

The server exposes 5 tools: `web_search`, `fetch_page`, `reddit_fetch`, `youtube_transcript`, `usage_status`.
All were tested and work EXCEPT the two issues below. Fix only these.

---

## Bug 1: `reddit_fetch` fails for individual post URLs (429)

Subreddit feeds (e.g. `target="r/commandline"`) work via `https://www.reddit.com/r/{sub}/{sort}.rss`.
But post URLs (e.g. `https://www.reddit.com/r/commandline/comments/1woks8t/`) consistently fail with
`429 Too Many Requests` on `comments/<id>.rss`. The current code path is in `sources.py`
(`reddit_fetch` → appends `.rss` to the post URL).

### Research findings (verified 2026-10, treat as constraints)

1. **Reddit's `.json` endpoints are dead for unauthenticated access** — they return 403 regardless
   of User-Agent (verified by curl from this machine). Do NOT build a `.json`-based solution.
2. **`.rss` still works but is rate-limited to roughly 1 request/minute per IP** for
   unauthenticated access. Exceeding it → 429. Responses include an `x-ratelimit-reset` header
   and `Retry-After`. Reference: https://picklog.cc/blog/reddit-rss-rate-limit and
   https://dev.to/listwright/reddits-json-returns-403-in-2026-the-rss-feeds-still-answer-1gg5
3. **Arctic Shift** is a free, no-auth, continuously-updated Reddit archive API — VERIFIED LIVE
   from this machine on 2026-10-09:
   - `curl "https://arctic-shift.photon-reddit.com/api/comments/tree?link_id=<post_id>&limit=5"`
     → 200 with full comment bodies (nested tree)
   - `curl "https://arctic-shift.photon-reddit.com/api/posts/search?subreddit=commandline&limit=1"` → 200
   - Docs/code: https://github.com/ArthurHeitmann/arctic_shift/tree/master/api
   - Caveat: archive ingestion may lag live Reddit by hours/days for very recent posts.
4. **Redlib** (https://github.com/redlib-org/redlib) public instances proxy Reddit without auth;
   instance list: https://redlibinstances.com/ . Optional fallback; instances come and go.
5. **Anti-ban rules (hard requirements):**
   - Never retry a 429 immediately — honor `Retry-After` / `x-ratelimit-reset`, else exponential
     backoff with jitter. Max 2-3 attempts.
   - Use a descriptive User-Agent in the form `platform:appid:version (by /u/username)`
     instead of the current generic Chrome UA **for Reddit requests** (Reddit explicitly
     rate-limits/blocks generic browser UAs on API-ish endpoints).
   - Do NOT rotate IPs, do NOT use TLS impersonation (curl_cffi) against Reddit — that's
     treated as malicious and risks an IP ban.
   - Add client-side throttling for Reddit RSS: minimum ~60s between RSS requests per process,
     plus a small in-memory TTL cache (~5 min) so repeated tool calls don't re-hit Reddit.

### Required fix

Rewrite the post-URL branch of `reddit_fetch` in `sources.py` as a fallback chain:
1. **Arctic Shift `/api/comments/tree?link_id=<id>`** (extract id from the post URL with a regex)
   as the PRIMARY source for post comments. Return top-level comments (+ author, score if
   available, body truncated like the current ~350 char style). If the post is too recent for
   the archive (empty data), fall through.
2. **Reddit `.rss`** as fallback, going through the new throttled/cached/backoff-wrapped request
   helper (which the subreddit-feed path should also use).
3. If both fail, return a clear error message (current behavior already does this well — keep it).

---

## Bug 2: `fetch_page` returns near-empty content for JS-rendered pages without escalating

Escalation is currently: direct → curl_cffi (TLS-impersonated) → Jina reader, but it only
escalates on fetch FAILURE. Repro: `fetch_page("https://vibehackers.io/blog/best-terminal-for-mac")`
returns `(via direct)` with ONLY the page title — the raw HTML is 62KB of JS shell.

### Required fix

In `server.py` (or wherever `fetch_page` lives): after extraction, if the extracted text is
below a threshold (suggest < 300 chars, or title-only), treat it as a soft failure and escalate
to the next tier. Keep the `(via <tier>)` prefix so users can see which tier produced the result.
Do not escalate for pages that legitimately have little content unless below the threshold —
example.com (~150 chars) should still pass via direct; tune the threshold so it does
(e.g. escalate only when < 300 chars AND the raw HTML is large/JS-heavy, or simply set the
threshold just below example.com's size — your call, document the choice).

---

## Verification (do all of these, show output)

1. `reddit_fetch` on `https://www.reddit.com/r/commandline/comments/1woks8t/` → returns comments.
2. `reddit_fetch` on `r/commandline` (subreddit feed) → still works; call it twice in a row and
   confirm the second call is served from cache or throttled, NOT a fresh Reddit hit.
3. Simulate/inspect the 429 path: confirm backoff honors `Retry-After` and gives up after ≤3 tries.
4. `fetch_page("https://vibehackers.io/blog/best-terminal-for-mac")` → returns real article text,
   not just the title.
5. `fetch_page("https://example.com")` → still `(via direct)`, no regression.
6. `fetch_page("https://www.youtube.com/watch?v=dQw4w9WgXcQ")` → still routes to transcript.
7. `web_search` and `usage_status` → still work; counters increment.
8. Confirm no curl_cffi/TLS-impersonation is ever used for reddit.com URLs.

You can test tools by running the server over stdio with a JSON-RPC `tools/call`, or by importing
the fetcher functions directly in a Python REPL — whichever is simpler. Report a final
pass/fail table for all 8 checks.
