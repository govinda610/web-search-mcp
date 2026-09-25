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
from urllib.parse import urlparse

import httpx
import pymupdf
import trafilatura
from curl_cffi import AsyncSession

CACHE = Path(__file__).parent / "state" / "cache"
# Cookies earned in the visible window (a check you clicked through, a login) are reused by
# every later browser fetch, so you only solve a site's check once.
COOKIES = Path(__file__).parent / "state" / "browser-cookies.json"
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
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
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


async def _curl_cffi(url: str, timeout: int, proxy: str | None = None):
    async with AsyncSession() as s:
        r = await s.get(url, impersonate="chrome", timeout=timeout, allow_redirects=True, proxy=proxy)
    return r.status_code, r.headers.get("content-type", ""), r.content


async def _camoufox(url: str, timeout: int, proxy: str | None = None, headless: bool = True):
    from camoufox.async_api import AsyncCamoufox

    # The visible window uses the settings verified against DataDome (G2): real-location
    # fingerprint + human-like cursor movement. os is pinned so saved cookies match the fingerprint.
    options = {"os": "macos"} if headless else {"os": "macos", "humanize": True, "geoip": True}
    if proxy:  # Firefox takes socks5:// and resolves hostnames through the proxy itself
        options["proxy"] = {"server": proxy.replace("socks5h://", "socks5://")}
    async with BROWSER_SLOTS, AsyncCamoufox(headless=headless, **options) as browser:
        page = await browser.new_page(storage_state=COOKIES if COOKIES.exists() else None)
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 2000)
        # Automatic challenges clear within ~5s. A visible window also waits for you to click
        # through a manual check (DDoS-Guard captcha, "I'm not a robot" box).
        for _ in range(15 if headless else HUMAN_WAIT):
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
    # A solved challenge still reports its 403/503, so only a hard status (404, 410, ...) is kept;
    # otherwise report success and let fetch() judge the final content with is_challenge().
    status = response.status if response else 200
    return (status if status >= 400 and status not in RETRYABLE else 200), "text/html", body


async def _jina(url: str, timeout: int, proxy: str | None = None):  # Jina fetches from its own servers
    key = os.environ.get("JINA_API_KEY")
    if not key:
        raise FetchError("JINA_API_KEY not set")
    async with httpx.AsyncClient(timeout=max(timeout, 30)) as c:
        r = await c.get(f"https://r.jina.ai/{url}", headers={"Authorization": f"Bearer {key}"})
    return r.status_code, "text/markdown", r.content


async def _camoufox_visible(url: str, timeout: int, proxy: str | None = None):
    """DataDome catches headless browsers but not a real window, so a Firefox window opens
    briefly. Only reached when the headless browser was blocked. FETCH_VISIBLE_BROWSER=0 disables."""
    if os.environ.get("FETCH_VISIBLE_BROWSER", "1") == "0":
        raise FetchError("disabled (FETCH_VISIBLE_BROWSER=0)")
    return await _camoufox(url, timeout, proxy, headless=False)


async def _chrome_cdp(url: str, timeout: int, proxy: str | None = None):
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
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 2000)
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


async def fetch(url: str, timeout: int = 15, interactive: bool = True, on_stage=None) -> Page:
    """Run the stage chain; if the site is unreachable directly, run it again through Tor.
    interactive=False never opens a visible window (for batch reads nobody is watching).
    on_stage(name) is awaited before each stage, for progress reporting."""
    await check_url(url)
    host = urlparse(url).hostname or ""
    if host in VIA_TOR:
        return await _escalate(url, timeout, TOR, interactive, on_stage)
    try:
        return await _escalate(url, timeout, None, interactive, on_stage)
    except FetchError as direct:
        if not str(direct).startswith("unreachable"):
            raise
        try:
            page = await _escalate(url, timeout, TOR, interactive, on_stage)
        except FetchError as tor:
            raise FetchError(f"{direct}; via Tor: {tor}") from tor
    VIA_TOR.add(host)
    return page._replace(via=page.via + "+tor")


async def _escalate(url: str, timeout: int, proxy: str | None, interactive: bool = True, on_stage=None) -> Page:
    """Escalate through STAGES until one returns real content. Raises FetchError with every attempt."""
    attempts = []
    for name, stage in STAGES:
        if name == "camoufox_visible" and not interactive:
            continue
        if on_stage:
            await on_stage(name + (" via Tor" if proxy else ""))
        try:
            status, content_type, body = await stage(url, timeout, proxy)
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


def cache_get(url: str) -> str | None:
    f = _cache_file(url)
    if f.exists() and time.time() - f.stat().st_mtime < CACHE_TTL:
        return f.read_text()
    return None


def cache_put(url: str, text: str) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    for old in CACHE.glob("*.txt"):  # drop expired entries so the cache doesn't grow forever
        if time.time() - old.stat().st_mtime > CACHE_TTL:
            old.unlink(missing_ok=True)
    _cache_file(url).write_text(text)


async def fetch_text(url: str, timeout: int = 15, fresh: bool = False, interactive: bool = True,
                     on_stage=None) -> tuple[str, str]:
    """(via, text), served from cache when fresh unless fresh=True."""
    cached = None if fresh else cache_get(url)
    if cached is not None:
        return "cache", cached
    page = await fetch(url, timeout, interactive, on_stage)
    cache_put(url, page.text)
    return page.via, page.text


def window(text: str, start: int, max_chars: int) -> str:
    """Slice long documents so agents can page through them instead of losing the tail."""
    chunk = text[start:start + max_chars]
    end = start + len(chunk)
    if end < len(text):
        chunk += f"\n\n[showing chars {start}-{end} of {len(text)}; call again with start={end} for more]"
    return chunk
