"""Page change watcher: baseline a page on first check, diff it against later checks."""
import difflib
import re
import time

import fetch
from store import STATE, load_json, save_json

PAGES = STATE / "pagewatch.json"
DIFF_CHAR_LIMIT = 4000


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _filter(text: str, query: str) -> str:
    """Keep only paragraphs containing a query word, so unrelated page churn is ignored."""
    words = [w.lower() for w in query.split() if w]
    if not words:
        return text
    return "\n\n".join(p for p in _paragraphs(text) if any(w in p.lower() for w in words))


async def check(url: str, query: str = "") -> str:
    """First call for a URL stores a baseline; later calls return a unified diff against it,
    or "unchanged since ..." when nothing changed."""
    _via, text = await fetch.fetch_text(url, timeout=15, fresh=True, interactive=False)
    if query:
        text = _filter(text, query)
    pages = load_json(PAGES)
    now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    entry = pages.get(url)
    if entry is None:
        pages[url] = {"text": text, "checked": now, "query": query}
        save_json(PAGES, pages)
        return f"Baseline stored for {url} ({now}). Check again later to see changes."
    pages[url] = {"text": text, "checked": now, "query": query}
    save_json(PAGES, pages)
    if entry["text"] == text:
        return f"unchanged since {entry['checked']} ({url})"
    diff = "\n".join(difflib.unified_diff(
        entry["text"].splitlines(), text.splitlines(),
        fromfile=f"{url} @ {entry['checked']}", tofile=f"{url} @ {now}", lineterm="", n=2))
    return diff[:DIFF_CHAR_LIMIT] if diff else f"unchanged since {entry['checked']} ({url})"


def forget(url: str) -> str:
    pages = load_json(PAGES)
    if pages.pop(url, None) is None:
        return f"No watch stored for {url}."
    save_json(PAGES, pages)
    return f"Stopped watching {url}."


def list_pages() -> str:
    pages = load_json(PAGES)
    if not pages:
        return "No pages being watched."
    return "\n".join(f"{url}: last checked {p['checked']}" + (f", filter {p['query']!r}" if p.get("query") else "")
                     for url, p in pages.items())
