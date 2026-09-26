"""A search page for people, served by the HTTP server at http://127.0.0.1:8765/.

Unlike the tools, which merge their sources into one answer for an agent, the page shows what
every source returned on its own, as each one finishes, next to the merged list. It calls the same
source functions the tools use, so a fix to a source shows up in both.

Only reachable as localhost, and every /ui/api call must carry the X-Requested-With header: a
browser can't add that header cross-site without a CORS preflight, which these routes never
answer, so a web page you visit can't make this server search or download things."""
import asyncio
import itertools
import json
import time
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse

import health
import knowledge
import media
import movies
import papers
import torrent

PAGE = Path(__file__).with_name("ui.html")
HEADER = ("x-requested-with", "web-search-ui")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
SEARXNG_CATEGORIES = ["general", "news", "images", "videos", "science", "it", "files", "music", "social media"]
SOURCE_TIMEOUT = 150  # seconds; the page shows each source as it lands, so slow ones only delay their own panel
_jobs: dict[int, dict] = {}
_job_ids = itertools.count(1)
_job_tasks: set[asyncio.Task] = set()  # keeps running downloads alive


def _allowed(request: Request, api: bool) -> bool:
    return request.url.hostname in LOCAL_HOSTS and (not api or request.headers.get(HEADER[0]) == HEADER[1])


def _json(data) -> str:
    return json.dumps(data, default=lambda o: sorted(o) if isinstance(o, set) else str(o))


async def _run(name: str, make, record: bool = True) -> dict:
    """One source, timed, as a panel event. Sources failing repeatedly are skipped for a while
    (health.py), the same as in the tools."""
    wait = health.skipped(name)
    if wait:
        return {"type": "source", "name": name, "status": "skipped",
                "error": f"failing repeatedly; retried in {int(wait // 60) + 1} min", "results": []}
    start = time.monotonic()
    try:
        results = await asyncio.wait_for(make(), SOURCE_TIMEOUT)
    except Exception as e:  # noqa: BLE001 - shown in the source's panel
        if record:
            health.record(name, False)
        error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        return {"type": "source", "name": name, "status": "error", "error": error[:300], "results": [],
                "ms": int((time.monotonic() - start) * 1000)}
    if record:
        health.record(name, True)
    return {"type": "source", "name": name, "status": "ok" if results else "empty", "results": results,
            "ms": int((time.monotonic() - start) * 1000)}


async def _media_merged(query: str, category: str, lists: list) -> dict:
    """The media tool's own merge: quality grades, fake/cinema warnings, fuzzy dedupe. A film or
    show with nothing downloadable offers the tool's retry queries as buttons."""
    runtime = await movies.runtime_min(query, category) if category in ("movies", "tv") else None
    catalog, found, _ = media._merge(lists, runtime)
    found = media.dedupe_similar(found)
    retries = await media._retry_queries(query, category) if category in ("movies", "tv") and not found else []
    return {"type": "merged", "catalog": catalog, "results": found,
            "retries": [{"why": why, "query": q} for why, q in retries]}


def register(mcp, env: dict, fetch_page, media_download, book_download) -> None:
    searxng_url = env.get("SEARXNG_URL", "").rstrip("/")

    async def searxng(query: str, category: str, engines: list[str], params: dict) -> list[dict]:
        """One SearXNG request, split into a panel per engine (a result several engines found
        appears in each of their panels), plus a panel for each engine that failed and why."""
        q = {"q": query, "format": "json", **params}
        q.update({"engines": ",".join(engines)} if engines else {"categories": category})
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                data = (await c.get(f"{searxng_url}/search", params=q)).raise_for_status().json()
        except Exception as e:  # noqa: BLE001
            return [{"type": "source", "name": "searxng", "status": "error", "results": [],
                     "error": f"SearXNG at {searxng_url or '(SEARXNG_URL not set)'}: {e}"[:300]}]
        panels: dict[str, list] = {e: [] for e in engines}
        for rank, x in enumerate(data.get("results", [])):
            r = {"title": x.get("title", ""), "url": x.get("url", ""), "snippet": (x.get("content") or "")[:400],
                 "date": (x.get("publishedDate") or "")[:10], "rank": rank, "engines": x.get("engines", []),
                 "thumbnail": x.get("thumbnail_src") or x.get("thumbnail") or "", "img_src": x.get("img_src", ""),
                 "magnet": x.get("magnetlink", ""), "seeders": int(x.get("seed") or 0) or None,
                 "size": x.get("filesize", ""), "authors": x.get("authors") or [], "doi": x.get("doi", ""),
                 "pdf": x.get("pdf_url", ""), "venue": x.get("journal", "")}
            for e in r["engines"] or [x.get("engine", "searxng")]:
                panels.setdefault(e, []).append(r)
        down = {e: why for e, why in data.get("unresponsive_engines", [])}
        # SearXNG doesn't time engines separately, so these panels carry no ms; the page shows the total
        events = [{"type": "source", "name": e, "status": "ok" if rs else "empty", "results": rs}
                  for e, rs in panels.items() if e not in down or rs]
        events += [{"type": "source", "name": e, "status": "error", "error": why, "results": []}
                   for e, why in down.items() if not panels.get(e)]
        return events

    async def stream(request: Request):
        p = request.query_params
        query, tab, limit = p.get("q", "").strip(), p.get("tab", "web"), min(int(p.get("limit") or 10), 50)
        category, chosen = p.get("category", ""), [s for s in p.get("sources", "").split(",") if s]
        params = {k: p[k] for k in ("time_range", "language", "safesearch", "pageno") if p.get(k)}

        jobs: list = []  # coroutines each returning one panel event, or a list of them
        extra = {f.__name__: f for f in papers.EXTRA_SOURCES} if tab == "papers" else {}
        if tab in ("web", "papers"):
            engines = [s for s in chosen if s not in extra]
            if engines or not chosen:  # nothing chosen = the category's default engines
                jobs.append(searxng(query, "science" if tab == "papers" else category or "general", engines, params))
        if tab == "papers":
            names = [s for s in chosen if s in extra] if chosen else list(extra)
            jobs += [_run(n, lambda f=extra[n]: f(query, limit, 20)) for n in names]
        if tab == "knowledge":
            names = [s for s in chosen if s in knowledge.SOURCES] or knowledge.DEFAULT
            jobs += [_run(n, lambda n=n: knowledge.SOURCES[n](query, limit)) for n in names]
        media_fns = []
        if tab == "media":
            category = category if category in media.SOURCES else "all"
            media_fns = [f for f in media.SOURCES[category] if not chosen or f.__name__ in chosen]
            # media._cached records health and caches each source's results for the tools too
            jobs += [_run(f.__name__, lambda f=f: media._cached(f, query, limit, category), record=False)
                     for f in media_fns]

        async def events():
            yield _json({"type": "start", "query": query}) + "\n"
            lists = []
            for done in asyncio.as_completed(jobs):
                out = await done
                for ev in out if isinstance(out, list) else [out]:
                    if media_fns and ev["status"] == "ok":
                        lists.append((ev["name"] in media.CATALOGS, ev["results"]))
                    yield _json(ev) + "\n"
            if media_fns:
                yield _json(await _media_merged(query, category, lists)) + "\n"
            yield _json({"type": "done"}) + "\n"

        if not query:
            return JSONResponse({"error": "empty query"}, status_code=400)
        return StreamingResponse(events(), media_type="application/x-ndjson")

    @mcp.custom_route("/", methods=["GET"], include_in_schema=False)
    async def page(request: Request) -> Response:
        if not _allowed(request, api=False):
            return Response("Open this page as http://127.0.0.1 or http://localhost.", status_code=403)
        return FileResponse(PAGE)

    @mcp.custom_route("/ui/api/{action}", methods=["GET", "POST"], include_in_schema=False)
    async def api(request: Request) -> Response:
        if not _allowed(request, api=True):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        action = request.path_params["action"]
        if action == "search":
            return await stream(request)
        if action == "sources":
            return JSONResponse(await _sources())
        if action == "jobs":
            return JSONResponse(sorted(_jobs.values(), key=lambda j: -j["id"]))
        if request.method != "POST":
            return JSONResponse({"error": f"unknown action {action}"}, status_code=404)
        body = await request.json()
        if action == "read":
            try:
                text = await fetch_page(url=body["url"], max_chars=int(body.get("max_chars") or 60000),
                                        query=body.get("query", ""))
            except Exception as e:  # noqa: BLE001
                return JSONResponse({"error": str(e)}, status_code=502)
            return JSONResponse({"text": text})
        if action == "download":
            return JSONResponse(_start_download(body, media_download, book_download))
        return JSONResponse({"error": f"unknown action {action}"}, status_code=404)

    async def _sources() -> dict:
        engines: dict[str, list] = {}
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                cfg = (await c.get(f"{searxng_url}/config")).json()
            for e in cfg.get("engines", []):
                if e.get("enabled"):
                    for cat in e.get("categories", []):
                        if cat in SEARXNG_CATEGORIES:
                            engines.setdefault(cat, []).append(e["name"])
        except Exception:  # noqa: BLE001, S110 - the page still works for the non-SearXNG tabs
            pass
        names = {f.__name__ for fns in media.SOURCES.values() for f in fns} | set(knowledge.SOURCES) \
            | {f.__name__ for f in papers.EXTRA_SOURCES}
        return {"searxng": engines, "papers": [f.__name__ for f in papers.EXTRA_SOURCES],
                "knowledge": {"all": list(knowledge.SOURCES), "default": knowledge.DEFAULT},
                "media": {cat: [f.__name__ for f in fns] for cat, fns in media.SOURCES.items()},
                "catalogs": sorted(media.CATALOGS), "trackers": torrent.TRACKERS,
                "skipped": {n: int(health.skipped(n)) for n in names if health.skipped(n)}}


def _start_download(body: dict, media_download, book_download) -> dict:
    """Downloads run in the background; the page polls /ui/api/jobs for progress."""
    job = {"id": next(_job_ids), "label": body.get("label") or body.get("url") or body.get("md5"),
           "folder": "~/Downloads/books" if body.get("md5") else "~/Downloads/media",  # the tools' defaults
           "status": "running", "progress": "", "result": ""}
    _jobs[job["id"]] = job

    class Progress:  # stands in for the MCP Context: the tools report progress through it
        async def report_progress(self, done, total, message):
            job["progress"] = message

    async def work():
        try:
            if body.get("md5"):
                job["result"] = await book_download(md5=body["md5"])
            else:
                job["result"] = await media_download(url=body["url"], format=body.get("format") or "mp4",
                                                     ctx=Progress())
            job["status"] = "done"
        except Exception as e:  # noqa: BLE001
            job["status"], job["result"] = "failed", str(e)

    task = asyncio.ensure_future(work())
    _job_tasks.add(task)
    task.add_done_callback(_job_tasks.discard)
    return job
