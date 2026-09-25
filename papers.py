"""Research papers.

Search: SearXNG's science category fans out to arXiv, Semantic Scholar, Google Scholar,
PubMed, EuropePMC and OpenAIRE locally, no keys. Alongside it, Crossref, bioRxiv/medRxiv
(via Crossref, which indexes both as posted-content) and CORE (only with CORE_API_KEY) run in
parallel through media.http, so one dead source never blocks the rest. Results are deduped by
title across every source.
Resolve: arXiv id/URL -> arXiv PDF; DOI -> open-access PDF via OpenAlex (keyless lookup), then
Unpaywall when UNPAYWALL_EMAIL is set, then Anna's Archive SciDB, then LibGen's article index,
both keyless.
"""
import asyncio
import html as htmllib
import os
import re
from urllib.parse import quote, urlencode, urljoin

import httpx

import fetch
import health
import mirrors
from media import http

OPENALEX = "https://api.openalex.org/works/https://doi.org/"
UNPAYWALL = "https://api.unpaywall.org/v2/"
CROSSREF = "https://api.crossref.org/works"
CORE_SEARCH = "https://api.core.ac.uk/v3/search/works"
UA = "web-search-mcp/1.0 (+https://github.com/govinda610/web-search-mcp)"
ANNAS_MIRRORS = ["https://annas-archive.gl", "https://annas-archive.pk", "https://annas-archive.gd"]
LIBGEN_MIRRORS = ["https://libgen.li", "https://libgen.bz", "https://libgen.vg"]


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())


def _clean(value) -> str:
    return "" if value in (None, "None") else str(value)


def _strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _year_ok(year: int, year_from: int, year_to: int) -> bool:
    return not year or ((not year_from or year >= year_from) and (not year_to or year <= year_to))


# ---------------------------------------------------------------- extra search sources

def _crossref_year(item: dict) -> int:
    for key in ("published-print", "published-online", "published", "posted", "issued"):
        parts = (item.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            return int(parts[0][0])
    return 0


def _crossref_entry(item: dict) -> dict:
    authors = [f"{a.get('given', '')} {a.get('family', '')}".strip()
              for a in item.get("author", []) if a.get("family") or a.get("given")]
    pdf = next((l["URL"] for l in item.get("link", []) if l.get("content-type") == "application/pdf"), "")
    venue = (item.get("container-title") or [""])[0] or (item.get("institution") or [{}])[0].get("name", "")
    return {"title": (item.get("title") or [""])[0], "year": _crossref_year(item), "url": item.get("URL", ""),
            "authors": authors, "venue": venue, "doi": item.get("DOI", ""), "pdf": pdf, "note": "",
            "abstract": _strip_tags(item.get("abstract", "")), "engines": {"crossref"}}


async def crossref(query: str, n: int, timeout: int, year_from: int = 0, year_to: int = 0) -> list[dict]:
    params = {"query": query, "rows": n}
    email = os.environ.get("UNPAYWALL_EMAIL")
    if email:
        params["mailto"] = email  # Crossref's "polite pool": faster, more reliable responses
    r = await http(f"{CROSSREF}?{urlencode(params)}", timeout=timeout, headers={"User-Agent": UA})
    return [_crossref_entry(it) for it in r.json().get("message", {}).get("items", [])]


async def biorxiv_medrxiv(query: str, n: int, timeout: int, year_from: int = 0, year_to: int = 0) -> list[dict]:
    """bioRxiv and medRxiv have no keyless search API of their own; Crossref indexes every one
    of their preprints as posted-content, tagged with the depositing institution's name."""
    params = {"query": query, "rows": n * 4, "filter": "type:posted-content"}
    r = await http(f"{CROSSREF}?{urlencode(params)}", timeout=timeout, headers={"User-Agent": UA})
    items = r.json().get("message", {}).get("items", [])
    return [_crossref_entry(it) for it in items
            if ((it.get("institution") or [{}])[0].get("name") or "").lower() in ("biorxiv", "medrxiv")][:n]


async def core(query: str, n: int, timeout: int, year_from: int = 0, year_to: int = 0) -> list[dict]:
    key = os.environ.get("CORE_API_KEY")
    if not key:
        return []  # opt-in: CORE requires a free key, unlike the other sources
    r = await http(f"{CORE_SEARCH}?q={quote(query)}&limit={n}", timeout=timeout,
                   headers={"Authorization": f"Bearer {key}", "User-Agent": UA})
    out = []
    for it in r.json().get("results", []):
        authors = [a.get("name", "") for a in it.get("authors") or [] if a.get("name")]
        pdf = it.get("downloadUrl") or next(iter(it.get("sourceFulltextUrls") or []), "")
        doi = it.get("doi") or ""
        out.append({"title": it.get("title") or "", "year": int(it.get("yearPublished") or 0),
                    "url": f"https://doi.org/{doi}" if doi else pdf, "authors": authors,
                    "venue": it.get("publisher") or "", "doi": doi, "pdf": pdf, "note": "",
                    "abstract": _strip_tags(it.get("abstract") or ""), "engines": {"core"}})
    return out


EXTRA_SOURCES = [crossref, biorxiv_medrxiv, core]


async def _guarded(fn, query: str, n: int, timeout: int, year_from: int, year_to: int) -> list[dict]:
    """Run one extra source, skipping it while health has it marked down (health.py) and
    recording the outcome so a source failing twice in a row is skipped for a while."""
    if health.skipped(fn.__name__):
        return []
    try:
        entries = await fn(query, n, timeout, year_from, year_to)
    except Exception:  # noqa: BLE001 - one bad source shouldn't cancel the others
        health.record(fn.__name__, False)
        return []
    health.record(fn.__name__, True)
    return entries


async def search(query: str, n: int, searxng_url: str, timeout: int, year_from: int = 0,
                 year_to: int = 0) -> list[dict]:
    papers: dict[str, dict] = {}

    def merge(entries: list[dict]) -> None:
        for e in entries:
            key = _norm_title(e["title"])
            if not key or not _year_ok(e["year"], year_from, year_to):
                continue
            if key in papers:  # same paper from another engine: fill gaps, record the source
                seen = papers[key]
                seen["pdf"] = seen["pdf"] or e["pdf"]
                seen["doi"] = seen["doi"] or e["doi"]
                seen["year"] = seen["year"] or e["year"]
                seen["venue"] = seen["venue"] or e["venue"]
                seen["abstract"] = seen["abstract"] or e["abstract"]
                seen["engines"] |= e["engines"]
                continue
            papers[key] = e

    async def searxng() -> list[dict]:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"{searxng_url.rstrip('/')}/search",
                            params={"q": query, "format": "json", "categories": "science"})
            r.raise_for_status()
        entries = []
        for x in r.json().get("results", []):
            m = re.match(r"\d{4}", _clean(x.get("publishedDate")))
            entries.append({
                "title": x.get("title", ""), "year": int(m.group(0)) if m else 0, "url": x.get("url", ""),
                "authors": x.get("authors") or [], "venue": _clean(x.get("journal")),
                "doi": _clean(x.get("doi")), "pdf": _clean(x.get("pdf_url")),
                "note": _clean(x.get("comments")), "abstract": _clean(x.get("content")),
                "engines": set(x.get("engines", [])),
            })
        return entries

    main, *extra = await asyncio.gather(
        searxng(), *(_guarded(fn, query, n, timeout, year_from, year_to) for fn in EXTRA_SOURCES),
        return_exceptions=True)
    if isinstance(main, Exception) and not any(extra):
        raise main  # nothing found anywhere: surface SearXNG's error rather than "no papers"
    # SearXNG's results go first: its score already rewards agreement across engines and rank
    # position. The extra sources add papers SearXNG missed, or fill in a pdf/doi/year.
    for entries in [main, *extra]:
        if not isinstance(entries, Exception):
            merge(entries)

    return list(papers.values())[:n]


def format_results(papers: list[dict]) -> str:
    lines = []
    for i, p in enumerate(papers, 1):
        authors = ", ".join(p["authors"][:4]) + (" et al." if len(p["authors"]) > 4 else "")
        head = f"{i}. {p['title']} ({p['year'] or 'n.d.'})"
        meta = " | ".join(s for s in (authors, p["venue"], p["note"]) if s)
        links = " | ".join(s for s in (p["url"], p["pdf"] and f"pdf: {p['pdf']}",
                                        p["doi"] and f"doi: {p['doi']}") if s)
        lines.append(f"{head}\n   {meta}\n   {links}\n   [{', '.join(sorted(p['engines']))}] "
                     f"{p['abstract'][:250]}")
    return "\n\n".join(lines)


_OLD_ARXIV_ID = r"[a-z-]+(?:\.[A-Za-z]{2,})?/\d{7}(?:v\d+)?"  # pre-2007 style, e.g. hep-th/9901001


def arxiv_id(ref: str) -> str | None:
    ref = ref.strip()
    if re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", ref) or re.fullmatch(_OLD_ARXIV_ID, ref, re.I):
        return ref
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([^?#\s]+?)(?:\.pdf)?/?$", ref, re.I) \
        or re.search(r"arxiv\.(\d{4}\.\d{4,5}(?:v\d+)?)", ref, re.I)  # 10.48550/arXiv.XXXX DOIs
    return m.group(1) if m else None


# ---------------------------------------------------------------- DOI fallbacks: no open-access
# copy on OpenAlex/Unpaywall, so try the two big shadow libraries, keyless, same as media.py's.

async def _scidb(doi: str, timeout: int) -> str:
    """Anna's Archive's SciDB reader. Goes through the browser stage (interactive=False, never a
    visible window) the same way media.annas_archive does, since the site sits behind DDoS-Guard."""
    async def attempt(base):
        page = await fetch.fetch(f"{base}/scidb/{doi}", timeout, interactive=False)
        if fetch.is_pdf(page.content_type, page.body):
            return f"{base}/scidb/{doi}"
        link = re.search(r'<iframe[^>]+src="([^"]+)"', page.text) or \
              re.search(r'href="([^"]+\.pdf[^"?#]*)"', page.text, re.IGNORECASE)
        if not link:
            raise RuntimeError("no PDF link on the SciDB page")
        candidate = urljoin(base, htmllib.unescape(link.group(1)))
        final_url, content_type, body = await fetch.get_checked(candidate, timeout)
        if not fetch.is_pdf(content_type, body):
            raise RuntimeError("SciDB link did not resolve to a PDF")
        return final_url
    return await mirrors.call("annas-archive", ANNAS_MIRRORS, {"slum": "annas-archive"}, attempt)


async def _libgen_scimag(doi: str, timeout: int) -> str:
    """LibGen's unified index (successor to the old standalone scimag mirror): search its
    article collection by DOI, follow the edition page to the md5, then to the actual file."""
    async def attempt(base):
        r = await http(f"{base}/index.php?req={quote(doi)}&res=25&topics[]=a", timeout=timeout)
        body = r.text[r.text.find("<tbody"):]
        row = next((row for row in body.split("<tr")[1:] if doi.lower() in row.lower()), None)
        if not row:
            raise RuntimeError("DOI not in LibGen's article index")
        edition = re.search(r"edition\.php\?id=(\d+)", row)
        if not edition:
            raise RuntimeError("no edition link for this DOI")
        page = await http(f"{base}/edition.php?id={edition.group(1)}", timeout=timeout)
        md5 = re.search(r"/ads\.php\?md5=([0-9a-f]{32})", page.text)
        if not md5:
            raise RuntimeError("no download link on the edition page")
        ads = await http(f"{base}/ads.php?md5={md5.group(1)}", timeout=timeout)
        link = re.search(r'href="(get\.php\?md5=[0-9a-f]{32}&(?:amp;)?key=\w+)"', ads.text)
        if not link:
            raise RuntimeError("no get.php link on the ads page")
        final_url, content_type, body = await fetch.get_checked(
            f"{base}/{htmllib.unescape(link.group(1))}", timeout)
        if not fetch.is_pdf(content_type, body):
            raise RuntimeError("LibGen link did not resolve to a PDF")
        return final_url
    return await mirrors.call("libgen", LIBGEN_MIRRORS, {"slum": "libgen"}, attempt)


async def resolve(ref: str, timeout: int) -> tuple[str, str]:
    """(url to fetch, note). ref: arXiv id/URL, DOI, doi.org URL, or any paper URL."""
    aid = arxiv_id(ref)
    if aid:
        return f"https://arxiv.org/pdf/{aid}", f"arXiv {aid}"
    m = re.search(r"10\.\d{4,9}/[^\s?#]+", ref)
    if not m:
        return ref, "direct URL"
    doi = m.group(0).rstrip(".")
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(OPENALEX + doi,
                        params={"select": "display_name,best_oa_location,open_access"})
    if r.status_code == 200:
        work = r.json()
        best = work.get("best_oa_location") or {}
        oa_url = best.get("pdf_url") or (work.get("open_access") or {}).get("oa_url")
        if oa_url:
            return oa_url, f"DOI {doi}: open-access copy via OpenAlex"
    email = os.environ.get("UNPAYWALL_EMAIL")
    if email:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(UNPAYWALL + doi, params={"email": email})
        best = (r.json().get("best_oa_location") or {}) if r.status_code == 200 else {}
        if best.get("url_for_pdf") or best.get("url"):
            return best.get("url_for_pdf") or best["url"], f"DOI {doi}: open-access copy via Unpaywall"
    for name, fallback in (("scidb", _scidb), ("libgen_scimag", _libgen_scimag)):
        if health.skipped(name):
            continue
        try:
            url = await fallback(doi, timeout)
        except Exception:  # noqa: BLE001 - try the next fallback, or give up cleanly below
            health.record(name, False)
            continue
        health.record(name, True)
        return url, f"DOI {doi}: copy via {"Anna's Archive SciDB" if name == "scidb" else "LibGen"}"
    return f"https://doi.org/{doi}", f"DOI {doi}: no open-access copy found, trying publisher page"
