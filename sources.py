"""Source-specific fetchers: Reddit (public .json), YouTube transcripts. Plus URL routing."""
import asyncio
import random
import re
import time
from urllib.parse import urlparse

import httpx

from providers import ProviderError

# Reddit anti-ban: RSS ~1 req/min unauthenticated. Descriptive UA, throttle, cache, backoff.
_REDDIT_UA = "macos:web-search-mcp:0.1.0 (by /u/govinda610)"
_RSS_MIN_INTERVAL = 60.0
_RSS_CACHE_TTL = 300.0
_rss_last_hit = 0.0
_rss_cache: dict = {}

ARCTIC = "https://arctic-shift.photon-reddit.com/api"


async def _throttled_reddit_get(url: str, timeout: int) -> str:
    """RSS GET with 60s min interval, 5-min cache, Retry-After backoff (max 3 attempts)."""
    global _rss_last_hit
    now = time.time()
    if url in _rss_cache and now - _rss_cache[url][0] < _RSS_CACHE_TTL:
        return _rss_cache[url][1]
    wait = _RSS_MIN_INTERVAL - (now - _rss_last_hit)
    if wait > 0:
        await asyncio.sleep(wait)
    for attempt in range(3):
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers={"User-Agent": _REDDIT_UA}) as c:
            r = await c.get(url)
        _rss_last_hit = time.time()
        if r.status_code == 429:
            if attempt == 2:
                raise ProviderError("reddit 429: rate limit persists after 3 attempts")
            ra = float(r.headers.get("retry-after",
                       r.headers.get("x-ratelimit-reset", "60")).split(",")[0])
            await asyncio.sleep(ra * (1 + 0.5 * attempt) + random.uniform(0, 5))
            continue
        r.raise_for_status()
        _rss_cache[url] = (time.time(), r.text)
        return r.text


async def _arctic_post(post_id: str, timeout: int):
    """Post title/body + top comments from the Arctic Shift archive (free, no auth), or None."""
    async with httpx.AsyncClient(timeout=timeout) as c:
        post_r, comments_r = await asyncio.gather(
            c.get(f"{ARCTIC}/posts/ids", params={"ids": post_id}),
            c.get(f"{ARCTIC}/comments/tree", params={"link_id": post_id, "limit": 12}))
    post_r.raise_for_status()
    comments_r.raise_for_status()
    posts = post_r.json().get("data") or []
    if not posts:
        return None
    post = posts[0]
    lines = [f"POST: {post.get('title', '')} (r/{post.get('subreddit', '?')}, "
             f"u/{post.get('author', '?')}, score {post.get('score', '?')})"]
    if post.get("selftext"):
        lines.append(post["selftext"][:3000])
    lines.append("COMMENTS:")
    for cm in (comments_r.json().get("data") or [])[:10]:
        if isinstance(cm, dict) and isinstance(cm.get("data"), dict):
            cm = cm["data"]  # arctic returns reddit-listing-style {kind, data} wrappers
        body = (cm.get("body") or "").replace("\n", " ")[:350]
        if body:
            lines.append(f"  > [u/{cm.get('author', '?')}] {body}")
    return "\n".join(lines)


REDDIT_HOSTS = ("reddit.com", "redd.it")
YT_HOSTS = ("youtube.com", "youtu.be")


def _host_in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def classify(url: str) -> str:
    """Route a URL to a source handler: reddit | youtube | instagram | generic."""
    host = (urlparse(url).hostname or "").lower()
    if _host_in(host, REDDIT_HOSTS):
        return "reddit"
    if _host_in(host, YT_HOSTS):
        return "youtube"
    if _host_in(host, ("instagram.com",)):
        return "instagram"
    return "generic"


async def _reddit_rss(sub: str, sort: str, limit: int, timeout: int) -> str:
    """Subreddit feed via native RSS (unblocked, free)."""
    text = await _throttled_reddit_get(
        f"https://www.reddit.com/r/{sub}/{sort}.rss?limit={limit}", timeout)
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", text, re.S)[:limit]:
        title = re.search(r"<title>(.*?)</title>", e, re.S)
        link = re.search(r'href="([^"]+)"', e)
        author = re.search(r"/u/([^<]+)<", e)
        out.append(f"- {(title.group(1) if title else '?').strip()}"
                   f"\n  {(link.group(1) if link else '')} (u/{author.group(1) if author else '?'})")
    return "\n".join(out) if out else f"No entries for r/{sub}"


def _format_post_rss(text: str, target: str) -> str:
    import html as htmllib

    def clean(s):
        s = htmllib.unescape(re.sub(r"<[^>]+>", " ", s))
        s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
        return re.sub(r"\s+", " ", s).strip()

    entries = re.findall(r"<entry>(.*?)</entry>", text, re.S)
    out = []
    for i, e in enumerate(entries[:12]):
        title = re.search(r"<title>(.*?)</title>", e, re.S)
        content = re.search(r'<content type="html">(.*?)</content>', e, re.S)
        if i == 0 and title:
            out.append("POST: " + clean(title.group(1)))
        if content:
            body = clean(content.group(1))
            if body:
                out.append(f"  > {body[:350]}")
    return "\n".join(out[:15]) if out else f"No content for {target}"


async def reddit_fetch(target: str, sort: str = "hot", limit: int = 15,
                       timeout: int = 15) -> str:
    """target: post/permalink URL (Arctic Shift archive -> throttled RSS) or subreddit."""
    if target.startswith(("http://", "https://")):
        m = re.search(r"/comments/([a-z0-9]+)", target)
        if m:
            try:
                post = await _arctic_post(m.group(1), timeout)
                if post:
                    return "(via arctic-shift archive)\n" + post
            except Exception:
                pass  # archive lags live reddit; fall through to RSS
        url = target.rstrip("/")
        if url.endswith(".json"):
            url = url[:-5]
        return _format_post_rss(await _throttled_reddit_get(url + ".rss", timeout), target)
    sub = target.removeprefix("r/").strip("/")
    return await _reddit_rss(sub, sort, limit, timeout)


def _yt_id(url: str) -> str:
    m = (re.search(r"[?&]v=([\w-]{11})", url) or re.search(r"youtu\.be/([\w-]{11})", url)
         or re.search(r"/(?:shorts|live|embed)/([\w-]{11})", url))
    if not m:
        raise ProviderError("no YouTube video id found in URL")
    return m.group(1)


async def youtube_transcript(url: str, lang: str = "en") -> str:
    """Transcript text for a YouTube video (captions or auto-generated). Free, no API key."""
    from youtube_transcript_api import YouTubeTranscriptApi

    vid = _yt_id(url)
    api = YouTubeTranscriptApi()
    fetched = await asyncio.to_thread(api.fetch, vid, languages=[lang, "en", "hi"])
    parts = [sn.text for sn in fetched]
    text = " ".join(parts)
    return f"(video {vid}, {len(parts)} segments)\n{text[:15000]}"
