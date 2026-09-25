"""List a site's pages: the sitemaps it publishes (robots.txt Sitemap: lines, /sitemap.xml,
/sitemap_index.xml, nested indexes, .gz), or failing that, the same-site links on its home page."""
import asyncio
import gzip
import html as htmllib
import re
from urllib.parse import urljoin, urlparse

import fetch
from media import http

MAX_SITEMAPS = 25  # nested sitemap files to open before stopping
ASSETS = re.compile(r"\.(css|js|png|jpe?g|gif|svg|ico|webp|woff2?|ttf|xml|json|webmanifest)(\?|$)", re.IGNORECASE)


async def _get(url: str) -> bytes | None:
    try:
        body = (await http(url, timeout=15)).content
    except Exception:  # noqa: BLE001 - a missing sitemap is normal
        return None
    return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body


def _locs(xml: bytes) -> list[str]:
    text = xml.decode("utf-8", errors="replace")
    return [htmllib.unescape(u.strip()) for u in re.findall(r"<loc>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</loc>", text, re.DOTALL)]


async def _from_sitemaps(url: str, root: str, limit: int, keep) -> tuple[list[str], list[str]]:
    robots = await _get(root + "/robots.txt") or b""
    queue = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots.decode("utf-8", errors="replace"))
    queue = queue or [root + "/sitemap.xml", root + "/sitemap_index.xml"]
    if urlparse(url).path.strip("/"):  # docs often live under a path with their own sitemap
        queue.insert(0, url.rstrip("/") + "/sitemap.xml")
    seen, pages, used = set(), [], []
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
            pages.extend(u for u in _locs(xml) if keep(u) and u not in pages)
    return pages[:limit], used


async def _from_links(url: str, limit: int, keep) -> list[str]:
    page = await http(url, timeout=15)
    host = urlparse(url).hostname
    links = []
    for href in re.findall(r'<a\s[^>]*?href=["\']([^"\'#]+)', page.text, re.IGNORECASE):
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
