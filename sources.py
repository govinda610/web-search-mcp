"""Source-specific fetchers: Reddit (public .json), YouTube transcripts. Plus URL routing."""
import asyncio
import os
import re

import httpx

from providers import ProviderError, UA

REDDIT_HOSTS = ("reddit.com", "old.reddit.com", "redd.it", "np.reddit.com")
YT_HOSTS = ("youtube.com", "youtu.be", "m.youtube.com", "www.youtube.com")


def classify(url: str) -> str:
    """Route a URL to a source handler: reddit | youtube | instagram | generic."""
    host = re.sub(r"^https?://(www\.)?", "", url.split("/")[0] + "." + url.split("/")[2]
                  if "://" in url else url).split("/")[0].lower()
    if any(h in host for h in REDDIT_HOSTS):
        return "reddit"
    if any(h in host for h in YT_HOSTS):
        return "youtube"
    if "instagram.com" in host:
        return "instagram"
    return "generic"


async def _reddit_rss(sub: str, sort: str, limit: int, timeout: int) -> str:
    """Subreddit feed via native RSS (unblocked, free)."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": UA}) as c:
        r = await c.get(f"https://www.reddit.com/r/{sub}/{sort}.rss?limit={limit}")
        r.raise_for_status()
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", r.text, re.S)[:limit]:
        title = re.search(r"<title>(.*?)</title>", e, re.S)
        link = re.search(r'href="([^"]+)"', e)
        author = re.search(r"/u/([^<]+)<", e)
        out.append(f"- {(title.group(1) if title else '?').strip()}"
                   f"\n  {(link.group(1) if link else '')} (u/{author.group(1) if author else '?'})")
    return "\n".join(out) if out else f"No entries for r/{sub}"


async def reddit_fetch(target: str, sort: str = "hot", limit: int = 15,
                       timeout: int = 15) -> str:
    """target: post/permalink URL (via reader) or subreddit (r/xyz or xyz) via native RSS."""
    if target.startswith(("http://", "https://")):
        url = target.rstrip("/")
        if url.endswith(".json"):
            url = url[:-5]
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers={"User-Agent": UA}) as c:
            r = await c.get(url + ".rss")
            r.raise_for_status()
        import html as htmllib

        def clean(s):
            s = htmllib.unescape(re.sub(r"<[^>]+>", " ", s))
            s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
            return re.sub(r"\s+", " ", s).strip()

        entries = re.findall(r"<entry>(.*?)</entry>", r.text, re.S)
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
        return chr(10).join(out[:15]) if out else f"No content for {target}"
    sub = target.removeprefix("r/").strip("/")
    return await _reddit_rss(sub, sort, limit, timeout)


def _yt_id(url: str) -> str:
    m = (re.search(r"[?&]v=([\w-]{11})", url) or re.search(r"youtu\.be/([\w-]{11})", url)
         or re.search(r"shorts/([\w-]{11})", url))
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
