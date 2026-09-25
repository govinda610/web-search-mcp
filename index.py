"""Local full-text index (SQLite FTS5) of every page fetch.py successfully fetches, so a past
read can be found again without refetching it. Populated from fetch.cache_put; queried by search().
"""
import re
import sqlite3
import threading
import time

from store import STATE

DB = STATE / "index.db"
MAX_TEXT = 200_000  # chars kept per page; plenty for a snippet, not a full mirror of the page
MAX_PAGES = 20_000  # oldest pages beyond this are pruned on write
_lock = threading.Lock()  # one writer at a time; sqlite3 connections aren't shared across threads

_TITLE_RE = re.compile(r"(?m)^(?:title:|#)\s+(.+)")  # front matter first, else the first heading
_WORD_RE = re.compile(r"\w+")
_FRONT_MATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)  # fetch.py's title/url/date header


def _connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS pages
                   USING fts5(url UNINDEXED, title, text, fetched_at UNINDEXED)""")
    return con


def _title(url: str, text: str) -> str:
    match = _TITLE_RE.search(text)
    return match.group(1).strip().strip('"') if match else url


def _add_page(url: str, text: str) -> None:
    with _lock:
        con = _connect()
        try:
            with con:
                con.execute("DELETE FROM pages WHERE url = ?", (url,))  # replace on refetch
                con.execute("INSERT INTO pages (url, title, text, fetched_at) VALUES (?, ?, ?, ?)",
                           (url, _title(url, text), _FRONT_MATTER_RE.sub("", text, count=1)[:MAX_TEXT], time.time()))
                n = con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
                if n > MAX_PAGES:
                    stale = con.execute("SELECT url FROM pages ORDER BY fetched_at ASC LIMIT ?",
                                       (n - MAX_PAGES,)).fetchall()
                    con.executemany("DELETE FROM pages WHERE url = ?", stale)
        finally:
            con.close()


def add_page(url: str, text: str) -> None:
    """Index a fetched page. Best-effort: a locked or corrupt index must not break the fetch
    that triggered it."""
    try:
        _add_page(url, text)
    except Exception:  # noqa: BLE001, S110 - see docstring
        pass


def _fts_query(q: str) -> str:
    """Each word quoted and ANDed, so FTS5 operators (AND/OR/NOT/*/column filters/unbalanced
    quotes/...) in arbitrary user text are matched as plain words instead of raising."""
    words = _WORD_RE.findall(q)
    return " ".join(f'"{w}"' for w in words)


def search(query: str, limit: int = 10, site: str = "") -> list[dict]:
    """Previously-fetched pages whose title/text match query, best match first, each with a short
    highlighted snippet and the date it was fetched. site restricts results to a domain."""
    fts = _fts_query(query)
    if not fts or not DB.exists():
        return []
    sql = "SELECT url, title, fetched_at, snippet(pages, 2, '[', ']', '...', 12) FROM pages WHERE pages MATCH ?"
    params = [fts]
    if site:
        sql += " AND url LIKE ?"
        params.append(f"%{site}%")
    sql += " ORDER BY bm25(pages) LIMIT ?"
    params.append(limit)
    con = _connect()
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    return [{"source": "history", "title": title, "url": url, "snippet": " ".join(snippet.split()),
             "date": "read " + time.strftime("%Y-%m-%d", time.localtime(fetched_at)), "info": ""}
            for url, title, fetched_at, snippet in rows]
