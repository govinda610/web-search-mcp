"""Page fetching + local text extraction.

Chain: curl_cffi (Chrome TLS fingerprint) -> Camoufox headless (stealth Firefox, solves
JS/Cloudflare challenges) -> your own Chrome over the DevTools protocol (only if CHROME_CDP_URL
is set; logged-in sites) -> Camoufox in a visible window (passes DataDome) -> Jina reader
(only if JINA_API_KEY is set). A site your ISP blocks (the connection itself fails) is retried
through Tor with the same chain, and remembered as Tor-only. HTML -> markdown via trafilatura,
PDF -> text via pymupdf. Extracted text is cached on disk for an hour.
"""
import asyncio
import hashlib
import html as htmllib
import ipaddress
import os
import re
import socket
import time
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

import httpx
import pymupdf
import trafilatura
from curl_cffi import AsyncSession

import index
from store import STATE

CACHE = STATE / "cache"
# Cookies earned in the visible window (a check you clicked through, a login) are reused by
# every later browser fetch, so you only solve a site's check once.
COOKIES = STATE / "browser-cookies.json"
HUMAN_WAIT = 120  # seconds the visible window waits for you to finish a manual check
CACHE_TTL = 3600

# Markers that only appear on bot-challenge interstitials, never on real content pages.
CHALLENGE_MARKERS = (
    "<title>just a moment",                     # Cloudflare JS challenge
    "<title>attention required! | cloudflare",  # Cloudflare block page
    "_cf_chl_opt",                               # Cloudflare challenge script
    "captcha-delivery.com",                      # DataDome
    "px-captcha",                                # PerimeterX
    "please enable js and disable any ad blocker",
    "<title>prove your humanity",                # Reddit bot wall
    "<title>ddos-guard",                         # DDoS-Guard JS check + manual captcha (Anna's Archive)
    "<title>error 1015",                          # Cloudflare rate limit
    "<title>sci-hub: are you are robot",         # Sci-Hub captcha
    "<title>making sure you",                    # Anubis proof-of-work (HAL, many open-source sites)
)
RETRYABLE = (401, 403, 429, 503)  # statuses a stealthier fetcher may get past
BROWSER_SLOTS = asyncio.Semaphore(2)  # each Camoufox instance is a full browser
TOR = os.environ.get("TOR_PROXY", "socks5h://127.0.0.1:9050")
VIA_TOR: set[str] = set()  # hosts that only answer through Tor, learned this session
# curl errors that mean the connection was cut before any HTTP happened: an ISP block, not the site.
# 6 DNS, 7 refused, 35 TLS handshake cut, 56 reset. 28 counts only while connecting: an ISP block
# drops the connection attempt, while a slow site times out mid-response ("Operation timed out").
UNREACHABLE_CODES = (6, 7, 35, 56)
FETCH_DEADLINE = 45  # seconds for one page in a batch (depth=advanced, fetch_pages)
FETCH_TOTAL_DEADLINE = 90  # overall wall-clock budget for one fetch() call: every stage, direct + Tor retry
MAX_REDIRECTS = 5  # hops followed by hand so each one can be SSRF-checked
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
BROWSER_SLOT_WAIT = 30  # seconds to wait for a free browser slot when no overall deadline applies


def is_unreachable(e: Exception) -> bool:
    code = getattr(e, "code", None)
    return code in UNREACHABLE_CODES or (code == 28 and "Connection timed out" in str(e))


class FetchError(Exception):
    pass


class Page(NamedTuple):
    via: str
    content_type: str
    body: bytes
    text: str


def is_challenge(body: bytes) -> bool:
    head = body[:60000].decode("utf-8", errors="ignore").lower()
    return any(m in head for m in CHALLENGE_MARKERS)


def is_pdf(content_type: str, body: bytes) -> bool:
    return "pdf" in content_type or body[:5] == b"%PDF-"


def is_binary(content_type: str, body: bytes) -> bool:
    """Images, video, archives: nothing an agent can read as text. PDFs are binary too, but readable."""
    if is_pdf(content_type, body):
        return False
    return content_type.startswith(("image/", "audio/", "video/")) or b"\0" in body[:2000]


def _is_unsafe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Anything that isn't a globally-routable address: RFC1918, loopback, link-local,
    reserved, and CGNAT (100.64.0.0/10, e.g. Tailscale) all have is_global=False."""
    return not ip.is_global


def _check_ip(ip_str: str, hostname: str) -> None:
    """Checks the address a request actually connected to (curl_cffi's Response.primary_ip),
    not just the one check_url resolved beforehand. Catches DNS rebinding: a hostname that
    resolves to a public IP when checked and a private one moments later when connected to."""
    if not ip_str or os.environ.get("FETCH_ALLOW_PRIVATE") == "1":
        return
    if _is_unsafe_ip(ipaddress.ip_address(ip_str)):
        raise FetchError(f"{hostname} resolved to a local/private address ({ip_str})")


async def check_url(url: str) -> None:
    """Only public http(s) pages. Stops a prompt-injected page from steering the agent to local
    files (file://) or to services on this machine or network (SearXNG, the Chrome debug port)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchError(f"only http(s) URLs can be fetched, not {url[:80]!r}")
    if os.environ.get("FETCH_ALLOW_PRIVATE") == "1":
        return
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, None)
    except OSError:
        return  # not resolvable here (an ISP DNS block); Tor resolves it remotely
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if _is_unsafe_ip(ip):
            raise FetchError(f"{parsed.hostname} is a local/private address; set FETCH_ALLOW_PRIVATE=1 to allow")


def strip_html(html: str) -> str:
    """Last-resort extraction when trafilatura finds no main content (index pages, apps)."""
    html = re.sub(r"(?s)<(script|style|noscript|svg)\b.*?</\1>", " ", html)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", htmllib.unescape(html)).strip()


def to_text(url: str, content_type: str, body: bytes) -> str:
    if is_pdf(content_type, body):
        with pymupdf.open(stream=body, filetype="pdf") as doc:
            return "\n\n".join(page.get_text() for page in doc).strip()
    if "html" in content_type or body.lstrip()[:1] == b"<":
        markdown = trafilatura.extract(body, url=url, output_format="markdown",
                                       with_metadata=True, include_comments=False)
        return markdown or strip_html(body.decode("utf-8", errors="replace"))
    return body.decode("utf-8", errors="replace")


def _cap(timeout: int, deadline: float | None) -> int:
    """A stage's own per-request timeout should never outlast what's left of the overall
    fetch() budget."""
    if deadline is None:
        return timeout
    return max(1, min(timeout, int(deadline - time.monotonic())))


# without a browser fingerprint curl_cffi sends no User-Agent at all, which archive.org answers with 400
PLAIN_UA = "web-search-mcp/1.0 (+https://github.com/govinda610/web-search-mcp)"


async def _curl_cffi(url: str, timeout: int, proxy: str | None = None, deadline: float | None = None,
                     impersonate: str | None = "chrome"):
    """Follows redirects by hand (allow_redirects=False) so every hop is SSRF-checked before
    it's requested, not just the URL the caller passed in."""
    for _ in range(MAX_REDIRECTS + 1):
        await check_url(url)
        kwargs = {"impersonate": impersonate} if impersonate else {"headers": {"User-Agent": PLAIN_UA}}
        async with AsyncSession() as s:
            r = await s.get(url, timeout=_cap(timeout, deadline),
                            allow_redirects=False, proxy=proxy, **kwargs)
        _check_ip(r.primary_ip, urlparse(url).hostname or "")
        location = r.headers.get("location")
        if r.status_code in REDIRECT_STATUSES and location:
            url = urljoin(url, location)
            continue
        return r.status_code, r.headers.get("content-type", ""), r.content
    raise FetchError(f"too many redirects (>{MAX_REDIRECTS})")


async def get_checked(url: str, timeout: int = 15, max_bytes: int = 8_000_000,
                      impersonate: str | None = "chrome") -> tuple[str, str, bytes]:
    """Plain GET with the same SSRF protections as fetch() -- redirects followed by hand, every
    hop and the actually-connected IP checked -- plus a body-size cap. For callers (crawl.py)
    that fetch arbitrary URLs directly without running the full stage chain.
    impersonate=None sends a plain curl request with no browser fingerprint; some hosts (e.g.
    archive.org) hang for the length of the timeout under curl_cffi's chrome/firefox/safari
    TLS fingerprints, so wayback.py passes None.
    Returns (final_url, content_type, body)."""
    for _ in range(MAX_REDIRECTS + 1):
        await check_url(url)
        kwargs = {"impersonate": impersonate} if impersonate else {"headers": {"User-Agent": PLAIN_UA}}
        async with AsyncSession() as s, s.stream("GET", url, timeout=timeout,
                                                 allow_redirects=False, **kwargs) as r:
            _check_ip(r.primary_ip, urlparse(url).hostname or "")
            location = r.headers.get("location")
            if r.status_code in REDIRECT_STATUSES and location:
                url = urljoin(url, location)
                continue
            content_type = r.headers.get("content-type", "")
            body = bytearray()
            async for chunk in r.aiter_content():
                body += chunk
                if len(body) > max_bytes:
                    if r.quit_now:  # tells curl to abort the transfer instead of finishing
                        r.quit_now.set()  # the download into memory before we discard it
                    break
            return url, content_type, bytes(body)
    raise FetchError(f"too many redirects (>{MAX_REDIRECTS})")


async def _camoufox(url: str, timeout: int, proxy: str | None = None, headless: bool = True,
                    deadline: float | None = None):
    from camoufox.async_api import AsyncCamoufox

    # The visible window uses the settings verified against DataDome (G2): real-location
    # fingerprint + human-like cursor movement. os is pinned so saved cookies match the fingerprint.
    options = {"os": "macos"} if headless else {"os": "macos", "humanize": True, "geoip": True}
    if proxy:  # Firefox takes socks5:// and resolves hostnames through the proxy itself
        options["proxy"] = {"server": proxy.replace("socks5h://", "socks5://")}
    slot_wait = max(1.0, deadline - time.monotonic()) if deadline is not None else BROWSER_SLOT_WAIT
    try:
        await asyncio.wait_for(BROWSER_SLOTS.acquire(), slot_wait)
    except TimeoutError as e:
        raise FetchError("no free browser slot (2 already busy)") from e
    try:
        async with AsyncCamoufox(headless=headless, **options) as browser:
            page = await browser.new_page(storage_state=COOKIES if COOKIES.exists() else None)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=_cap(timeout, deadline) * 2000)
            await check_url(page.url)  # a redirect the browser followed could land on a private address
            # Automatic challenges clear within ~5s. A visible window also waits for you to click
            # through a manual check (DDoS-Guard captcha, "I'm not a robot" box), bounded by
            # whatever's left of the overall fetch budget so it can't hold the browser slot forever.
            loops = 15
            if not headless:
                loops = HUMAN_WAIT if deadline is None else max(1, min(HUMAN_WAIT, int(deadline - time.monotonic())))
            for _ in range(loops):
                if not is_challenge((await page.content()).encode()):
                    break
                await page.wait_for_timeout(1000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:  # noqa: BLE001 - pages with long-polling never go idle; content is fine
                pass
            body = (await page.content()).encode()
            if not headless and not is_challenge(body):
                COOKIES.parent.mkdir(parents=True, exist_ok=True)
                await page.context.storage_state(path=COOKIES)
                COOKIES.chmod(0o600)  # logged-in session cookies
    finally:
        BROWSER_SLOTS.release()
    # A solved challenge still reports its 403/503, so only a hard status (404, 410, ...) is kept;
    # otherwise report success and let fetch() judge the final content with is_challenge().
    status = response.status if response else 200
    return (status if status >= 400 and status not in RETRYABLE else 200), "text/html", body


_JINA_FAILURE = re.compile(
    r"Warning: Target URL returned error [45]\d\d|^Title: Just a moment\.\.\.", re.MULTILINE)


async def _jina(url: str, timeout: int, proxy: str | None = None,
                deadline: float | None = None):  # Jina fetches from its own servers
    key = os.environ.get("JINA_API_KEY")
    if not key:
        raise FetchError("JINA_API_KEY not set")
    t = max(timeout, 30) if deadline is None else _cap(timeout, deadline)
    async with httpx.AsyncClient(timeout=t) as c:
        r = await c.get(f"https://r.jina.ai/{url}", headers={"Authorization": f"Bearer {key}"})
    text = r.content.decode("utf-8", errors="replace")
    match = _JINA_FAILURE.search(text)
    if match:
        raise FetchError(f"jina: {match.group(0)}")
    return r.status_code, "text/markdown", r.content


async def _camoufox_visible(url: str, timeout: int, proxy: str | None = None, deadline: float | None = None):
    """DataDome catches headless browsers but not a real window, so a Firefox window opens
    briefly. Only reached when the headless browser was blocked. FETCH_VISIBLE_BROWSER=0 disables."""
    if os.environ.get("FETCH_VISIBLE_BROWSER", "1") == "0":
        raise FetchError("disabled (FETCH_VISIBLE_BROWSER=0)")
    return await _camoufox(url, timeout, proxy, headless=False, deadline=deadline)


async def _chrome_cdp(url: str, timeout: int, proxy: str | None = None, deadline: float | None = None):
    """Open the page in a tab of your own running Chrome, with its logins and cookies, over the
    DevTools protocol. For sites that need an account (Instagram, X, LinkedIn) or reject Firefox.
    Opt-in: start Chrome with --remote-debugging-port=9222 and set CHROME_CDP_URL=http://127.0.0.1:9222."""
    endpoint = os.environ.get("CHROME_CDP_URL")
    if not endpoint:
        raise FetchError("CHROME_CDP_URL not set")
    if proxy:
        raise FetchError("skipped: your Chrome can't be routed through Tor per tab")
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(endpoint, timeout=5000)
        page = await browser.contexts[0].new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=_cap(timeout, deadline) * 2000)
            await check_url(page.url)  # a redirect the tab followed could land on a private address
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:  # noqa: BLE001, S110 - pages with long-polling never go idle; content is fine
                pass
            body = (await page.content()).encode()
        finally:
            await page.close()  # closes only our tab; browser.close() below just disconnects
            await browser.close()
    return 200, "text/html", body


STAGES = [("curl_cffi", _curl_cffi), ("camoufox", _camoufox), ("chrome_cdp", _chrome_cdp),
          ("camoufox_visible", _camoufox_visible), ("jina", _jina)]
# method= forces a single stage (or pair) instead of the full escalation chain.
METHOD_STAGES = {
    "plain": [("curl_cffi", _curl_cffi)],
    "tor": [("curl_cffi", _curl_cffi)],
    "browser": [("camoufox", _camoufox), ("camoufox_visible", _camoufox_visible)],
    "chrome": [("chrome_cdp", _chrome_cdp)],
}
METHODS = ("auto", "plain", "browser", "chrome", "tor", "archive")


async def fetch(url: str, timeout: int = 15, interactive: bool = True, on_stage=None,
                deadline: float = FETCH_TOTAL_DEADLINE, method: str = "auto") -> Page:
    """Run the stage chain; if the site is unreachable directly, run it again through Tor. If every
    live stage fails, or succeeds but the page is an obvious paywall stub, fall back to an archived
    copy (archive.today, then the closest Wayback Machine snapshot).
    method picks a single stage instead of the chain, and skips the archive fallback on failure:
    plain (curl_cffi only), browser (camoufox, visible window if it's still blocked), chrome (your
    own Chrome over CDP; needs CHROME_CDP_URL), tor (curl_cffi via Tor), archive (skip live
    fetching, read only the archived copy). auto (default) is the chain described above.
    interactive=False never opens a visible window (for batch reads nobody is watching).
    on_stage(name) is awaited before each stage, for progress reporting.
    deadline is the wall-clock budget in seconds for the whole chain (direct + Tor retry); the
    interactive visible-window stage only runs if enough of it remains."""
    await check_url(url)
    if method not in METHODS:
        raise FetchError(f"unknown method {method!r}; use one of {', '.join(METHODS)}")
    if method == "archive":
        page = await _paywall_fallback(url, timeout, on_stage)
        if page:
            return page
        raise FetchError("no archived copy found (archive.today and Wayback both failed)")
    if method != "auto":
        proxy = TOR if method == "tor" else None
        return await _escalate(url, timeout, proxy, interactive, on_stage, time.monotonic() + deadline,
                               stages=METHOD_STAGES[method])
    host = urlparse(url).hostname or ""
    end = time.monotonic() + deadline
    try:
        page = await _live_fetch(url, timeout, interactive, on_stage, end, host)
    except FetchError:
        page = await _paywall_fallback(url, timeout, on_stage)
        if page:
            return page
        raise
    if is_paywalled(page.text):
        better = await _paywall_fallback(url, timeout, on_stage)
        if better:
            return better
    return page


async def _live_fetch(url: str, timeout: int, interactive: bool, on_stage, end: float, host: str) -> Page:
    if host in VIA_TOR:
        return await _escalate(url, timeout, TOR, interactive, on_stage, end)
    try:
        return await _escalate(url, timeout, None, interactive, on_stage, end)
    except FetchError as direct:
        if not str(direct).startswith("unreachable"):
            raise
        try:
            page = await _escalate(url, timeout, TOR, interactive, on_stage, end)
        except FetchError as tor:
            raise FetchError(f"{direct}; via Tor: {tor}") from tor
    VIA_TOR.add(host)
    return page._replace(via=page.via + "+tor")


# Phrases that only show up on a paywall's teaser stub, never on the article itself. Paired with
# a short-text check so a normal page that happens to mention "subscribe" isn't misdetected.
_PAYWALL_MARKERS = (
    "subscribe to continue reading", "subscribe now to continue reading", "to continue reading this",
    "to keep reading, subscribe", "you have reached your article limit", "you've reached your article limit",
    "this article is for subscribers only", "already a subscriber? sign in",
    "create a free account to continue reading", "sign in or subscribe to continue",
)
_PAYWALL_STUB_CHARS = 1200  # a real article runs longer than this; a paywall stub is one teaser paragraph


def is_paywalled(text: str) -> bool:
    if len(text) > _PAYWALL_STUB_CHARS:
        return False
    low = text.lower()
    return any(m in low for m in _PAYWALL_MARKERS)


# archive.today's own captcha wall (a reCAPTCHA widget), distinct from CHALLENGE_MARKERS: those
# are matched against every site fetch() reads, and "g-recaptcha" is too common on ordinary pages
# (contact forms, comment sections) to be a safe marker there. Confirmed by hand: /newest/<url>
# answers curl_cffi's Chrome fingerprint directly, for both hits and a clean 404 on a miss -- but
# a URL archive.today gets hit with often enough (a widely-shared paywalled story) can still tip
# into this captcha instead, even for a plain GET.
_ARCHIVE_CAPTCHA = b'id="g-recaptcha"'


async def _archive_today(url: str, timeout: int) -> Page | None:
    """archive.today's newest snapshot of url, via the plain redirect endpoint."""
    try:
        status, content_type, body = await _curl_cffi(f"https://archive.ph/newest/{url}", timeout)
    except Exception:  # noqa: BLE001 - archive.today being unreachable isn't fetch()'s error to raise
        return None
    if status >= 400 or is_binary(content_type, body) or is_challenge(body) or _ARCHIVE_CAPTCHA in body:
        return None
    text = await asyncio.to_thread(to_text, url, content_type, body)
    if not text.strip():
        return None
    return Page("archive.today", content_type, body, text)


async def _paywall_fallback(url: str, timeout: int, on_stage=None) -> Page | None:
    """archive.today, then the closest Wayback snapshot: whichever has a readable copy of a
    paywalled or otherwise dead page."""
    if on_stage:
        await on_stage("archive.today")
    page = await _archive_today(url, timeout)
    if page:
        return page
    if on_stage:
        await on_stage("wayback")
    return await archived_page(url, timeout)


async def archived_page(url: str, timeout: int, timestamp: str = "") -> Page | None:
    """The archived copy closest to timestamp (YYYY[MM[DD]], default now), fetched via the raw
    `id_` replay form so the bytes are the original page, not archive.org's replay UI around it.
    fetch() uses it as the last resort when every live stage failed."""
    import wayback  # local import: wayback.py imports fetch, so this stays out of the module cycle

    try:
        replay = await wayback.closest(url, timestamp)
    except Exception:  # noqa: BLE001 - archive.org being unreachable isn't fetch()'s error to raise
        return None
    match = replay and re.search(r"/web/(\d{4,14})", replay)
    if not match:
        return None
    ts = match.group(1)
    raw_url = f"https://web.archive.org/web/{ts}id_/{url}"
    try:
        status, content_type, body = await _curl_cffi(raw_url, timeout, impersonate=None)  # chrome fp hangs here
    except Exception:  # noqa: BLE001 - a broken snapshot isn't better than no answer
        return None
    if status >= 400 or is_binary(content_type, body):
        return None
    text = await asyncio.to_thread(to_text, url, content_type, body)
    if not text.strip():
        return None
    date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else ts
    return Page(f"wayback (snapshot {date})", content_type, body, text)


async def _escalate(url: str, timeout: int, proxy: str | None, interactive: bool = True, on_stage=None,
                    deadline: float | None = None, stages=STAGES) -> Page:
    """Escalate through stages (default STAGES; method= passes a shorter list) until one returns
    real content. Raises FetchError with every attempt."""
    attempts = []
    for name, stage in stages:
        if name == "camoufox_visible" and not interactive:
            continue
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                attempts.append(f"{name}: skipped (fetch budget ran out)")
                break
            if name == "camoufox_visible" and remaining < 10:
                attempts.append(f"{name}: skipped (only {remaining:.0f}s left in fetch budget)")
                continue
        if on_stage:
            await on_stage(name + (" via Tor" if proxy else ""))
        try:
            status, content_type, body = await stage(url, timeout, proxy, deadline=deadline)
        except Exception as e:  # noqa: BLE001 - any stage failure escalates to the next
            if name == "curl_cffi" and is_unreachable(e):
                # a browser can't get past a cut connection either; skip straight to the Tor retry
                raise FetchError(f"unreachable ({str(e)[:120]})") from e
            attempts.append(f"{name}: {type(e).__name__}: {e}"[:200])
            continue
        if status >= 400 and status not in RETRYABLE:
            raise FetchError(f"{name}: HTTP {status}")  # a real 404 won't improve with stealth
        if status in RETRYABLE or is_challenge(body):
            attempts.append(f"{name}: blocked (HTTP {status})")
            continue
        if is_binary(content_type, body):
            raise FetchError(f"{name}: not a readable page ({content_type or 'binary data'}, {len(body):,} bytes)")
        text = await asyncio.to_thread(to_text, url, content_type, body)  # pymupdf/trafilatura are CPU-bound
        # A JS app shell: lots of HTML, almost no text. The browser stage renders it.
        if name == "curl_cffi" and len(text) < 300 and len(body) > 20000:
            attempts.append(f"{name}: JS shell ({len(body)}B html -> {len(text)} chars text)")
            continue
        if not text.strip():
            attempts.append(f"{name}: empty")
            continue
        return Page(name, content_type, body, text)
    raise FetchError("; ".join(attempts))


def _cache_file(url: str) -> Path:
    return CACHE / (hashlib.md5(url.encode()).hexdigest() + ".txt")


def cache_get(url: str, max_age: int | None = None) -> str | None:
    """The cached text if it's younger than max_age seconds (default: CACHE_TTL). The file's own
    mtime is the fetch time, so no separate timestamp needs storing. max_age=0 never returns a hit.
    A cache entry never lives past CACHE_TTL regardless of max_age (cache_put prunes on that
    schedule), so max_age only usefully narrows the window, not widens it."""
    age = CACHE_TTL if max_age is None else max_age
    if age <= 0:
        return None
    f = _cache_file(url)
    try:
        if time.time() - f.stat().st_mtime < age:
            return f.read_text()
    except FileNotFoundError:
        pass  # another call's cache_put pruned it, or it never existed
    return None


CACHE_PRUNE_INTERVAL = 600  # seconds between sweeps for expired entries; not on every write
_last_prune = 0.0


def cache_put(url: str, text: str) -> None:
    global _last_prune
    CACHE.mkdir(parents=True, exist_ok=True)
    now = time.time()
    if now - _last_prune > CACHE_PRUNE_INTERVAL:
        for old in CACHE.glob("*.txt"):  # drop expired entries so the cache doesn't grow forever
            if now - old.stat().st_mtime > CACHE_TTL:
                old.unlink(missing_ok=True)
        _last_prune = now
    _cache_file(url).write_text(text)
    index.add_page(url, text)


async def fetch_text(url: str, timeout: int = 15, max_age: int | None = None, interactive: bool = True,
                     on_stage=None, deadline: float = FETCH_TOTAL_DEADLINE, method: str = "auto") -> tuple[str, str]:
    """(via, text). max_age: None uses the default 1h cache, 0 always fetches live, N accepts a
    cached copy up to N seconds old. method picks which stage(s) fetch() uses; see fetch()."""
    cached = cache_get(url, max_age)
    if cached is not None:
        return "cache", cached
    page = await fetch(url, timeout, interactive, on_stage, deadline, method)
    cache_put(url, page.text)
    return page.via, page.text


def window(text: str, start: int, max_chars: int) -> str:
    """Slice long documents so agents can page through them instead of losing the tail."""
    chunk = text[start:start + max_chars]
    end = start + len(chunk)
    if end < len(text):
        chunk += f"\n\n[showing chars {start}-{end} of {len(text)}; call again with start={end} for more]"
    return chunk
