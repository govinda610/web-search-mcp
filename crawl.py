"""List a site's pages: the sitemaps it publishes (robots.txt Sitemap: lines, /sitemap.xml,
/sitemap_index.xml, nested indexes, .gz), or failing that, the same-site links on its home page.
Also a same-site best-first crawl (crawl()) for sites with no sitemap, or to go past its scope."""
import asyncio
import heapq
import html as htmllib
import itertools
import re
import time
import zlib
from urllib.parse import urljoin, urlparse

import fetch

MAX_SITEMAPS = 25  # nested sitemap files to open before stopping
ASSETS = re.compile(r"\.(css|js|png|jpe?g|gif|svg|ico|webp|woff2?|ttf|xml|json|webmanifest)(\?|$)", re.IGNORECASE)
GZIP_CAP = 50 * 1024 * 1024  # never inflate a sitemap past this, in case it's a gzip bomb
CRAWL_DEADLINE = 120  # seconds for a whole crawl() call
CRAWL_CONCURRENCY = 4


def _gunzip(data: bytes) -> bytes | None:
    d = zlib.decompressobj(zlib.MAX_WBITS | 16)  # | 16: expect a gzip header/trailer, not raw zlib
    out = d.decompress(data, GZIP_CAP)
    if d.unconsumed_tail or not d.eof:
        return None  # would inflate past the cap, or the stream is truncated; skip it
    return out


async def _get(url: str) -> bytes | None:
    try:
        _, _, body = await fetch.get_checked(url, timeout=15)
    except Exception:  # noqa: BLE001 - a missing sitemap is normal
        return None
    return _gunzip(body) if body[:2] == b"\x1f\x8b" else body


def _locs(xml: bytes) -> list[str]:
    text = xml.decode("utf-8", errors="replace")
    return [htmllib.unescape(u.strip()) for u in re.findall(r"<loc>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</loc>", text, re.DOTALL)]


async def _from_sitemaps(url: str, root: str, limit: int, keep) -> tuple[list[str], list[str]]:
    robots = await _get(root + "/robots.txt") or b""
    queue = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots.decode("utf-8", errors="replace"))
    queue = queue or [root + "/sitemap.xml", root + "/sitemap_index.xml"]
    if urlparse(url).path.strip("/"):  # docs often live under a path with their own sitemap
        queue.insert(0, url.rstrip("/") + "/sitemap.xml")
    seen, pages, seen_pages, used = set(), [], set(), []
    while queue and len(used) < MAX_SITEMAPS and len(pages) < limit:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        xml = await _get(url)
        if not xml or b"<loc>" not in xml:
            continue
        used.append(url)
        if b"<sitemapindex" in xml[:2000]:
            queue.extend(_locs(xml))  # an index of sitemaps: open each in turn
        else:
            for u in _locs(xml):  # a set + an early break: a 50k-URL sitemap can't freeze this
                if len(pages) >= limit:
                    break
                if keep(u) and u not in seen_pages:
                    seen_pages.add(u)
                    pages.append(u)
    return pages[:limit], used


async def _from_links(url: str, limit: int, keep) -> list[str]:
    _, _, body = await fetch.get_checked(url, timeout=15)
    text = body.decode("utf-8", errors="replace")
    host = urlparse(url).hostname
    links = []
    for href in re.findall(r'<a\s[^>]*?href=["\']([^"\'#]+)', text, re.IGNORECASE):
        link = urljoin(url, htmllib.unescape(href))
        if (urlparse(link).hostname == host and link.startswith("http") and not ASSETS.search(link)
                and keep(link) and link not in links):
            links.append(link)
    return links[:limit]


async def site_map(url: str, limit: int = 200, path_filter: str = "") -> str:
    if "//" not in url:
        url = "https://" + url
    await fetch.check_url(url)
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    scope = url.split("?")[0].rstrip("/") if parsed.path.strip("/") else ""  # a sub-path limits the map to it

    def keep(u: str) -> bool:
        return u.startswith(scope) and path_filter.lower() in u.lower()
    pages, used = await _from_sitemaps(url, root, limit, keep)
    if pages:
        return (f"{len(pages)} pages of {root} from {len(used)} sitemap file(s)"
                + (f" matching {path_filter!r}" if path_filter else "") + ":\n" + "\n".join(pages))
    links = await asyncio.wait_for(_from_links(url, limit, keep), 30)
    return (f"{root} publishes no sitemap; {len(links)} same-site links found on {url}"
            + (f" matching {path_filter!r}" if path_filter else "") + ":\n" + "\n".join(links))


_LINK_RE = re.compile(r'<a\s[^>]*?href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)


_BASE_RE = re.compile(r'<base\s[^>]*href=["\']([^"\']+)|<link\s[^>]*rel=["\']canonical["\'][^>]*href=["\']([^"\']+)',
                      re.IGNORECASE)


def _page_links(base_url: str, body: bytes) -> list[tuple[str, str]]:
    """(url, anchor text) pairs from a page's raw HTML. Relative links resolve against the page's
    <base> or canonical URL when it has one: a redirect (/uv -> /uv/) changes what "a/b" means."""
    html = body.decode("utf-8", errors="replace")
    declared = _BASE_RE.search(html)
    if declared:
        base_url = urljoin(base_url, htmllib.unescape(declared.group(1) or declared.group(2)))
    out = []
    for href, inner in _LINK_RE.findall(html):
        link = urljoin(base_url, htmllib.unescape(href))
        if link.startswith("http"):
            out.append((link, fetch.strip_html(inner)))
    return out


def _excerpt(text: str, terms: list[str], max_chars: int) -> str:
    """The paragraphs most relevant to terms, back in reading order, up to max_chars. No terms
    (or none of them found): just the opening of the page."""
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not terms or not paras:
        return text[:max_chars]
    ranked = sorted(range(len(paras)), key=lambda i: -sum(paras[i].lower().count(t) for t in terms))
    if not sum(paras[ranked[0]].lower().count(t) for t in terms):
        return text[:max_chars]  # nothing matched; fall back to the opening
    picked, total, out = sorted(ranked[:6]), 0, []
    for i in picked:
        if total >= max_chars:
            break
        out.append(paras[i])
        total += len(paras[i])
    return "\n\n".join(out)[:max_chars]


async def crawl(url: str, query: str = "", limit: int = 25, max_depth: int = 2, path_filter: str = "",
                max_chars_each: int = 1500, on_progress=None) -> str:
    """Same-site best-first crawl from url: fetches pages, follows same-site links (skipping
    assets and anything outside a sub-path scope / path_filter), and prioritises links whose URL
    or anchor text match query. For sites with no sitemap, or content beyond one's scope."""
    if "//" not in url:
        url = "https://" + url
    await fetch.check_url(url)
    parsed = urlparse(url)
    host = parsed.hostname
    scope = url.split("?")[0].rstrip("/") if parsed.path.strip("/") else ""

    def keep(u: str) -> bool:
        if urlparse(u).hostname != host or ASSETS.search(u):
            return False
        if scope and u.split("?")[0].rstrip("/") != scope and not u.startswith(scope + "/"):
            return False  # /uv keeps /uv/... but not /uvx
        return path_filter.lower() in u.lower()

    terms = [w.lower() for w in re.findall(r"\w+", query) if len(w) > 2]

    def score(text: str) -> int:
        low = text.lower()
        return sum(low.count(t) for t in terms)

    seen = {url.rstrip("/")}  # /uv and /uv/ are the same page
    counter = itertools.count()
    frontier: list[tuple[int, int, str, int]] = [(0, next(counter), url, 0)]  # (-score, seq, url, depth)
    pages: list[tuple[str, str, str]] = []
    sem = asyncio.Semaphore(CRAWL_CONCURRENCY)
    deadline = time.monotonic() + CRAWL_DEADLINE

    async def visit(link: str, depth: int) -> None:
        async with sem:
            try:
                page = await fetch.fetch(link, interactive=False, deadline=fetch.FETCH_DEADLINE)
            except fetch.FetchError:
                return
        title_match = re.search(r"(?m)^#\s+(.+)", page.text)
        pages.append((link, title_match.group(1).strip() if title_match else link, page.text))
        if depth >= max_depth:
            return
        for href, anchor in _page_links(link, page.body):
            if href.rstrip("/") not in seen and keep(href):
                seen.add(href.rstrip("/"))
                heapq.heappush(frontier, (-score(href + " " + anchor), next(counter), href, depth + 1))

    while frontier and len(pages) < limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        batch = []
        while frontier and len(batch) < CRAWL_CONCURRENCY and len(pages) + len(batch) < limit:
            _, _, link, depth = heapq.heappop(frontier)
            batch.append((link, depth))
        if not batch:
            break
        try:
            await asyncio.wait_for(asyncio.gather(*(visit(link, depth) for link, depth in batch)), remaining)
        except TimeoutError:
            break
        if on_progress:
            await on_progress(len(pages), limit, batch[-1][0])

    root = f"{parsed.scheme}://{parsed.netloc}"
    lines = [f"Crawled {len(pages)} pages of {root} (depth {max_depth}):", ""]
    for link, title, text in pages:
        lines.append(f"== {title} ==\n{link}")
        lines.append(_excerpt(text, terms, max_chars_each))
        lines.append("")
    return "\n".join(lines).strip()
