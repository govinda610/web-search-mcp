"""Research papers.

Search: SearXNG's science category fans out to arXiv, Semantic Scholar, Google Scholar,
PubMed, EuropePMC and OpenAIRE locally, no keys. Results are deduped by title.
Resolve: arXiv id/URL -> arXiv PDF; DOI -> open-access PDF via OpenAlex (keyless lookup),
then Unpaywall when UNPAYWALL_EMAIL is set (it asks for a contact address, no key).
"""
import os
import re

import httpx

OPENALEX = "https://api.openalex.org/works/https://doi.org/"
UNPAYWALL = "https://api.unpaywall.org/v2/"


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())


def _clean(value) -> str:
    return "" if value in (None, "None") else str(value)


async def search(query: str, n: int, searxng_url: str, timeout: int, year_from: int = 0,
                 year_to: int = 0) -> list[dict]:
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(f"{searxng_url.rstrip('/')}/search",
                        params={"q": query, "format": "json", "categories": "science"})
        r.raise_for_status()
    papers: dict[str, dict] = {}
    for x in r.json().get("results", []):
        m = re.match(r"\d{4}", _clean(x.get("publishedDate")))
        year = int(m.group(0)) if m else 0
        if year and ((year_from and year < year_from) or (year_to and year > year_to)):
            continue  # papers with no known year are kept rather than silently dropped
        key = _norm_title(x.get("title", ""))
        if not key:
            continue
        if key in papers:  # same paper from another engine: fill gaps, record the source
            seen = papers[key]
            seen["pdf"] = seen["pdf"] or _clean(x.get("pdf_url"))
            seen["doi"] = seen["doi"] or _clean(x.get("doi"))
            seen["year"] = seen["year"] or year
            seen["engines"] |= set(x.get("engines", []))
            continue
        papers[key] = {
            "title": x["title"], "year": year, "url": x.get("url", ""),
            "authors": x.get("authors") or [], "venue": _clean(x.get("journal")),
            "doi": _clean(x.get("doi")), "pdf": _clean(x.get("pdf_url")),
            "note": _clean(x.get("comments")), "abstract": _clean(x.get("content")),
            "engines": set(x.get("engines", [])),
        }
    # Keep SearXNG's order: its score already rewards agreement across engines and rank position.
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


def arxiv_id(ref: str) -> str | None:
    ref = ref.strip()
    if re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", ref):
        return ref
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([^?#\s]+?)(?:\.pdf)?/?$", ref, re.I) \
        or re.search(r"arxiv\.(\d{4}\.\d{4,5}(?:v\d+)?)", ref, re.I)  # 10.48550/arXiv.XXXX DOIs
    return m.group(1) if m else None


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
    return f"https://doi.org/{doi}", f"DOI {doi}: no open-access copy found, trying publisher page"
