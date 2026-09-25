"""Keyless APIs for knowledge an agent often needs directly: encyclopedia, developer Q&A,
code, tech discussion, ML papers and models, packages.

Each source is one small async function: query -> list of {source, title, url, snippet, date, info}.
Requests go through media.http, so they share its politeness (1 request/second/host) and
Tor fallback.
"""
import asyncio
import html as htmllib
import re
import time
from urllib.parse import quote

from media import http


def _clean(text: str, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()[:limit]


async def wikipedia(query: str, limit: int) -> list[dict]:
    r = await http(f"https://en.wikipedia.org/w/rest.php/v1/search/page?q={quote(query)}&limit={limit}")
    return [{"source": "wikipedia", "title": p["title"], "url": f"https://en.wikipedia.org/wiki/{quote(p['key'])}",
             "snippet": _clean(p.get("excerpt")), "date": "", "info": p.get("description") or ""}
            for p in r.json().get("pages", [])]


async def hackernews(query: str, limit: int) -> list[dict]:
    r = await http(f"https://hn.algolia.com/api/v1/search?query={quote(query)}&tags=story&hitsPerPage={limit}")
    return [{"source": "hackernews", "title": h.get("title") or "", "url": h.get("url") or
             f"https://news.ycombinator.com/item?id={h['objectID']}",
             "snippet": _clean(h.get("story_text")), "date": (h.get("created_at") or "")[:10],
             "info": f"{h.get('points', 0)} points · {h.get('num_comments', 0)} comments · "
                     f"https://news.ycombinator.com/item?id={h['objectID']}"}
            for h in r.json().get("hits", [])]


async def stackoverflow(query: str, limit: int) -> list[dict]:
    r = await http("https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance"
                   f"&site=stackoverflow&pagesize={limit}&q={quote(query)}")
    return [{"source": "stackoverflow", "title": _clean(q["title"]), "url": q["link"], "snippet": "",
             "date": time.strftime("%Y-%m-%d", time.gmtime(q.get("creation_date", 0))),
             "info": f"score {q.get('score', 0)} · {q.get('answer_count', 0)} answers"
                     f"{' · accepted answer' if q.get('accepted_answer_id') else ''} · {', '.join(q.get('tags', [])[:4])}"}
            for q in r.json().get("items", [])]


async def github(query: str, limit: int) -> list[dict]:
    r = await http(f"https://api.github.com/search/repositories?q={quote(query)}&per_page={limit}",
                   headers={"Accept": "application/vnd.github+json"})
    return [{"source": "github", "title": g["full_name"], "url": g["html_url"], "snippet": _clean(g.get("description")),
             "date": "", "info": f"{g.get('stargazers_count', 0):,} stars · {g.get('language') or '?'} · "
                                 f"last push {(g.get('pushed_at') or '')[:10]}"}
            for g in r.json().get("items", [])]


async def openreview(query: str, limit: int) -> list[dict]:
    r = await http(f"https://api2.openreview.net/notes/search?term={quote(query)}&limit={limit * 2}&source=forum")
    out = []
    for n in r.json().get("notes", []):
        c = n.get("content", {})
        title = (c.get("title") or {}).get("value")
        if not title:
            continue  # reviews and comments, not papers
        out.append({"source": "openreview", "title": title, "url": f"https://openreview.net/forum?id={n['forum']}",
                    "snippet": _clean((c.get("abstract") or {}).get("value")),
                    "date": time.strftime("%Y-%m-%d", time.gmtime((n.get("pdate") or n.get("cdate") or 0) / 1000)),
                    "info": (c.get("venue") or {}).get("value", "")})
    return out[:limit]


async def huggingface_papers(query: str, limit: int) -> list[dict]:
    r = await http(f"https://huggingface.co/api/papers/search?q={quote(query)}")
    return [{"source": "hf-papers", "title": _clean(p["title"]), "url": f"https://huggingface.co/papers/{p['id']}",
             "snippet": _clean(p.get("summary")), "date": (p.get("publishedAt") or "")[:10],
             "info": f"arXiv {p['id']} · {p.get('upvotes', 0)} upvotes"
                     + (f" · code {p['githubRepo']}" if p.get("githubRepo") else "")}
            for p in (x["paper"] for x in r.json()[:limit])]


async def huggingface_models(query: str, limit: int) -> list[dict]:
    r = await http(f"https://huggingface.co/api/models?search={quote(query)}&sort=downloads&limit={limit}")
    return [{"source": "hf-models", "title": m["id"], "url": f"https://huggingface.co/{m['id']}", "snippet": "",
             "date": "", "info": f"{m.get('downloads', 0):,} downloads · {m.get('likes', 0)} likes · "
                                 f"{m.get('pipeline_tag') or ''}"}
            for m in r.json()]


async def lemmy(query: str, limit: int) -> list[dict]:
    r = await http(f"https://lemmy.world/api/v3/search?q={quote(query)}&type_=Posts&sort=TopAll&limit={limit}",
                   timeout=25)  # lemmy.world's search is slow
    return [{"source": "lemmy", "title": p["post"]["name"], "url": p["post"]["ap_id"],
             "snippet": _clean(p["post"].get("body")), "date": p["post"].get("published", "")[:10],
             "info": f"c/{p['community']['name']} · score {p['counts'].get('score', 0)} · "
                     f"{p['counts'].get('comments', 0)} comments" + (f" · links {p['post']['url']}" if p["post"].get("url") else "")}
            for p in r.json().get("posts", [])]


async def packages(query: str, limit: int) -> list[dict]:
    """npm and crates.io search, plus PyPI when the query is an exact package name."""
    async def pypi():
        try:
            info = (await http(f"https://pypi.org/pypi/{quote(query.strip())}/json")).json()["info"]
        except Exception:  # noqa: BLE001 - PyPI has no search API; only exact names resolve
            return []
        return [{"source": "pypi", "title": f"{info['name']} {info['version']}", "url": info.get("project_url") or
                 f"https://pypi.org/project/{info['name']}/", "snippet": _clean(info.get("summary")), "date": "",
                 "info": f"requires python {info.get('requires_python') or '?'}"}]

    async def npm():
        r = await http(f"https://registry.npmjs.org/-/v1/search?text={quote(query)}&size={limit}")
        return [{"source": "npm", "title": f"{o['package']['name']} {o['package'].get('version', '')}",
                 "url": f"https://www.npmjs.com/package/{o['package']['name']}",
                 "snippet": _clean(o["package"].get("description")), "date": o["package"].get("date", "")[:10],
                 "info": f"{o.get('downloads', {}).get('monthly', '?')} downloads/month"}
                for o in r.json().get("objects", [])]

    async def crates():
        r = await http(f"https://crates.io/api/v1/crates?q={quote(query)}&per_page={limit}",
                       headers={"User-Agent": "web-search-mcp (personal research tool)"})
        return [{"source": "crates", "title": f"{c['name']} {c.get('max_version', '')}",
                 "url": f"https://crates.io/crates/{c['name']}", "snippet": _clean(c.get("description")),
                 "date": (c.get("updated_at") or "")[:10], "info": f"{c.get('downloads', 0):,} downloads"}
                for c in r.json().get("crates", [])]
    runs = await asyncio.gather(pypi(), npm(), crates(), return_exceptions=True)
    return [r for run in runs if isinstance(run, list) for r in run]


SOURCES = {f.__name__: f for f in (wikipedia, hackernews, stackoverflow, github, openreview, huggingface_papers,
                                   huggingface_models, lemmy, packages)}
DEFAULT = ["wikipedia", "hackernews", "stackoverflow", "github", "openreview", "huggingface_papers"]


async def search(query: str, names: list[str], limit: int) -> str:
    runs = await asyncio.gather(*(SOURCES[n](query, limit) for n in names), return_exceptions=True)
    parts, notes = [], []
    for name, res in zip(names, runs):
        if isinstance(res, Exception):
            notes.append(f"{name}: failed ({type(res).__name__}: {res})"[:160])
            continue
        notes.append(f"{name}: {len(res)}")
        if res:
            parts.append(f"== {name} ==\n" + "\n".join(
                f"{r['title']}\n  {r['url']}\n  " + " | ".join(x for x in (r["date"], r["info"]) if x)
                + (f"\n  {r['snippet']}" if r["snippet"] else "") for r in res))
    return f"sources: {', '.join(notes)}\n\n" + "\n\n".join(parts)
