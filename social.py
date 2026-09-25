"""Read public social posts without logging in: X/Twitter (fxtwitter, then X's own embed
endpoint), Bluesky (public AppView), Telegram channels (t.me/s web preview) and Instagram
profiles (the web app's JSON, often rate-limited)."""
import html as htmllib
import re
from urllib.parse import quote, urlparse

from media import http


def _clean(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    return htmllib.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _tweet(t: dict) -> str:
    media = [m.get("url", "") for m in (t.get("media") or {}).get("all", [])]
    lines = [f"@{t['author']['screen_name']} ({t['author'].get('name', '')}) · {t.get('created_at', '')}",
             t.get("text", ""),
             f"{t.get('likes', 0)} likes · {t.get('retweets', 0)} reposts · {t.get('replies', 0)} replies"
             + (f" · {t['views']} views" if t.get("views") else "") + f" · {t.get('url', '')}"]
    if media:
        lines.append("media: " + " ".join(media))
    if t.get("quote"):
        lines.append("quoting:\n  " + _tweet(t["quote"]).replace("\n", "\n  "))
    return "\n".join(lines)


async def x_post(user: str, post_id: str) -> str:
    try:
        return _tweet((await http(f"https://api.fxtwitter.com/{user}/status/{post_id}")).json()["tweet"])
    except Exception:  # noqa: BLE001 - fxtwitter is a community service; X's embed endpoint is the backup
        t = (await http(f"https://cdn.syndication.twimg.com/tweet-result?id={post_id}&token=a")).json()
        return (f"@{t['user']['screen_name']} ({t['user'].get('name', '')}) · {t.get('created_at', '')}\n"
                f"{t.get('text', '')}\n{t.get('favorite_count', 0)} likes · https://x.com/{user}/status/{post_id}")


async def x_profile(user: str, limit: int) -> str:
    u = (await http(f"https://api.fxtwitter.com/{user}")).json()["user"]
    out = [f"@{u['screen_name']} ({u.get('name', '')}) · {u.get('followers', 0):,} followers · "
           f"{u.get('following', 0):,} following · {u.get('tweets', 0):,} posts · joined {u.get('joined', '')}",
           u.get("description", ""), ""]
    try:
        posts = (await http(f"https://api.fxtwitter.com/2/profile/{user}/statuses?count={limit}")).json()["results"]
        out += [f"--- {p.get('created_at', '')} {p.get('url', '')}\n{p.get('text', '')}" for p in posts[:limit]]
    except Exception as e:  # noqa: BLE001 - the profile alone is still worth returning
        out.append(f"(recent posts unavailable: {e})")
    return "\n".join(out)


async def bluesky(handle: str, post_id: str, limit: int) -> str:
    api = "https://public.api.bsky.app/xrpc"
    if post_id:
        uri = quote(f"at://{handle}/app.bsky.feed.post/{post_id}", safe="")
        thread = (await http(f"{api}/app.bsky.feed.getPostThread?uri={uri}&depth=1")).json()["thread"]
        posts = [thread["post"]] + [r["post"] for r in thread.get("replies", [])[:limit] if "post" in r]
    else:
        feed = (await http(f"{api}/app.bsky.feed.getAuthorFeed?actor={quote(handle)}&limit={limit}")).json()["feed"]
        posts = [f["post"] for f in feed]
    return "\n".join(
        f"--- @{p['author']['handle']} · {p['record'].get('createdAt', '')[:16]} · {p.get('likeCount', 0)} likes · "
        f"{p.get('replyCount', 0)} replies · https://bsky.app/profile/{p['author']['handle']}/post/{p['uri'].rsplit('/', 1)[-1]}"
        f"\n{p['record'].get('text', '')}" for p in posts)


async def telegram(channel: str, limit: int) -> str:
    page = (await http(f"https://t.me/s/{channel}")).text
    title = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    posts = re.findall(r'data-post="([^"]+)".*?(?:tgme_widget_message_text[^>]*>(.*?)</div>.*?)?'
                       r'<time[^>]*datetime="([^"]+)"', page, re.DOTALL)
    out = [f"Telegram channel {htmllib.unescape(title.group(1)) if title else channel} (t.me/{channel})"]
    for post, text, when in posts[-limit:][::-1]:
        out.append(f"--- {when[:16]} https://t.me/{post}\n{_clean(text) or '(media only)'}")
    return "\n".join(out) if len(out) > 1 else out[0] + "\n(no public posts; the channel may be private)"


async def instagram(user: str, limit: int) -> str:
    r = await http(f"https://www.instagram.com/api/v1/users/web_profile_info/?username={user}",
                   headers={"x-ig-app-id": "936619743392459"})
    u = r.json()["data"]["user"]
    out = [f"@{u['username']} ({u.get('full_name', '')}) · {u['edge_followed_by']['count']:,} followers · "
           f"{u['edge_owner_to_timeline_media']['count']:,} posts", u.get("biography", ""), ""]
    for edge in u["edge_owner_to_timeline_media"]["edges"][:limit]:
        n = edge["node"]
        caption = n["edge_media_to_caption"]["edges"]
        out.append(f"--- https://www.instagram.com/p/{n['shortcode']}/ · {n.get('edge_liked_by', {}).get('count', 0)} likes"
                   f"\n{caption[0]['node']['text'] if caption else ''}")
    return "\n".join(out)


def parse(target: str) -> tuple[str, list[str]]:
    """Map a URL or @handle to (network, path parts). Bare @handles are X accounts."""
    target = target.strip()
    if target.startswith("@"):
        return ("bluesky", [target[1:]]) if "." in target else ("x", [target[1:]])
    parsed = urlparse(target if "//" in target else "https://" + target)
    host = (parsed.hostname or "").removeprefix("www.").removeprefix("mobile.")
    parts = [p for p in parsed.path.split("/") if p]
    network = {"x.com": "x", "twitter.com": "x", "fxtwitter.com": "x", "vxtwitter.com": "x", "t.me": "telegram",
               "telegram.me": "telegram", "bsky.app": "bluesky", "instagram.com": "instagram"}.get(host)
    if not network or not parts:
        raise ValueError(f"not an X, Bluesky, Telegram or Instagram profile/post: {target!r}")
    return network, parts


def is_social(url: str) -> bool:
    try:
        parse(url)
        return True
    except ValueError:
        return False


async def read(target: str, limit: int = 10) -> str:
    network, parts = parse(target)
    if network == "x":
        if len(parts) >= 3 and parts[1] == "status":
            return await x_post(parts[0], parts[2])
        return await x_profile(parts[0], limit)
    if network == "bluesky":
        if parts[0] == "profile":
            parts = parts[1:]
        post_id = parts[2] if len(parts) >= 3 and parts[1] == "post" else ""
        return await bluesky(parts[0], post_id, limit)
    if network == "telegram":
        return await telegram(parts[1] if parts[0] == "s" and len(parts) > 1 else parts[0], limit)
    return await instagram(parts[0], limit)
