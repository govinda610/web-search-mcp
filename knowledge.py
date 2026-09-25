"""Keyless APIs for knowledge an agent often needs directly: encyclopedia, developer Q&A,
code, tech discussion, ML papers and models, packages.

Each source is one small async function: query -> list of {source, title, url, snippet, date, info}.
Requests go through media.http, so they share its politeness (1 request/second/host) and
Tor fallback.
"""
import asyncio
import html as htmllib
import os
import re
import textwrap
import time
from urllib.parse import quote

import health
import index
from media import http


def _clean(text: str, limit: int = 300) -> str:
    text = re.sub(r"</?(?:span|a|b|i|em|strong|mark|code|sup|sub)\b[^>]*>", "", text or "")  # inside words
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", text))).strip()[:limit]


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


# property -> label, for the compact fact line rendered per wikidata entity
_WD_CLAIMS = {"P31": "instance of", "P17": "country", "P571": "inception", "P112": "founded by",
              "P169": "CEO", "P856": "website", "P1082": "population", "P569": "born",
              "P570": "died", "P106": "occupation"}


def _wd_value(claim: dict, labels: dict) -> str | None:
    snak = claim.get("mainsnak", {}).get("datavalue", {})
    kind, value = snak.get("type"), snak.get("value")
    if kind == "wikibase-entityid":
        return labels.get(value["id"], value["id"])
    if kind == "time":
        return (value.get("time") or "").lstrip("+").split("T")[0]
    if kind == "quantity":
        return value.get("amount", "").lstrip("+")
    if kind == "string":
        return value
    return None


async def wikidata(query: str, limit: int) -> list[dict]:
    hits = (await http("https://www.wikidata.org/w/api.php?action=wbsearchentities&format=json"
                       f"&language=en&limit={limit}&search={quote(query)}")).json().get("search", [])
    if not hits:
        return []
    ids = "|".join(h["id"] for h in hits)
    entities = (await http(f"https://www.wikidata.org/w/api.php?action=wbgetentities&format=json"
                           f"&ids={ids}&props=labels|descriptions|claims&languages=en")).json().get("entities", {})
    ref_ids = {v["mainsnak"]["datavalue"]["value"]["id"]
              for ent in entities.values() for pid in _WD_CLAIMS
              for v in ent.get("claims", {}).get(pid, [])
              if v.get("mainsnak", {}).get("datavalue", {}).get("type") == "wikibase-entityid"}
    labels = {}
    if ref_ids:
        ref_entities = (await http("https://www.wikidata.org/w/api.php?action=wbgetentities&format=json"
                                   f"&ids={'|'.join(ref_ids)}&props=labels&languages=en")).json().get("entities", {})
        labels = {rid: (e.get("labels", {}).get("en", {}).get("value") or rid) for rid, e in ref_entities.items()}
    out = []
    for h in hits:
        ent = entities.get(h["id"], {})
        facts = []
        for pid, label in _WD_CLAIMS.items():
            for claim in ent.get("claims", {}).get(pid, [])[:1]:
                v = _wd_value(claim, labels)
                if v:
                    facts.append(f"{label}: {v}")
        out.append({"source": "wikidata", "title": h.get("label", h["id"]),
                    "url": f"https://www.wikidata.org/wiki/{h['id']}",
                    "snippet": h.get("description", ""), "date": "", "info": " · ".join(facts[:6])})
    return out[:limit]


# ---------------------------------------------------------------- domain-specific

async def clinicaltrials(query: str, limit: int) -> list[dict]:
    r = await http(f"https://clinicaltrials.gov/api/v2/studies?query.term={quote(query)}&pageSize={limit}"
                   "&fields=NCTId,BriefTitle,OverallStatus,Phase,StartDate,LeadSponsorName,Condition")
    out = []
    for s in r.json().get("studies", []):
        p = s["protocolSection"]
        ident, status = p.get("identificationModule", {}), p.get("statusModule", {})
        sponsor = p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {})
        phases = p.get("designModule", {}).get("phases", [])
        out.append({"source": "clinicaltrials", "title": ident.get("briefTitle", ""),
                    "url": f"https://clinicaltrials.gov/study/{ident['nctId']}",
                    "snippet": ", ".join(p.get("conditionsModule", {}).get("conditions", [])[:3]),
                    "date": (status.get("startDateStruct", {}).get("date") or "")[:10],
                    "info": " · ".join(x for x in (status.get("overallStatus"), "/".join(phases),
                                                   sponsor.get("name")) if x)})
    return out


async def openfda(query: str, limit: int) -> list[dict]:
    """FDA drug labels: indications, warnings, manufacturer. Matches by brand, generic or
    active-ingredient name."""
    q = f'(openfda.brand_name:"{query}" OR openfda.generic_name:"{query}" OR openfda.substance_name:"{query}")'
    r = await http(f"https://api.fda.gov/drug/label.json?search={quote(q)}&limit={limit}")
    out = []
    for d in r.json().get("results", []):
        info = d.get("openfda", {})
        title = (info.get("brand_name") or info.get("generic_name") or [query])[0]
        effective = d.get("effective_time") or ""  # YYYYMMDD, no dashes
        out.append({"source": "openfda", "title": title,
                    "url": f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={d.get('set_id', '')}",
                    "snippet": _clean((d.get("indications_and_usage") or [""])[0]),
                    "date": f"{effective[:4]}-{effective[4:6]}-{effective[6:8]}" if len(effective) == 8 else "",
                    "info": " · ".join(x for x in ((info.get("generic_name") or [""])[0],
                                                   (info.get("manufacturer_name") or [""])[0]) if x)})
    return out


async def courtlistener(query: str, limit: int) -> list[dict]:
    """US case law search. COURTLISTENER_TOKEN (free account) raises the rate limit; unset,
    search still works."""
    headers = {"Accept": "application/json"}  # DRF serves an HTML browsable page for Chrome's default Accept
    token = os.environ.get("COURTLISTENER_TOKEN")
    if token:
        headers["Authorization"] = f"Token {token}"
    r = await http(f"https://www.courtlistener.com/api/rest/v4/search/?q={quote(query)}&type=o",
                   headers=headers, timeout=20)
    out = []
    for c in r.json().get("results", [])[:limit]:
        opinion = (c.get("opinions") or [{}])[0]
        out.append({"source": "courtlistener", "title": c.get("caseName", ""),
                    "url": f"https://www.courtlistener.com{c.get('absolute_url', '')}",
                    "snippet": _clean(opinion.get("snippet")), "date": (c.get("dateFiled") or "")[:10],
                    "info": " · ".join(x for x in (c.get("court"), ", ".join(c.get("citation") or [])) if x)})
    return out


async def patents(query: str, limit: int) -> list[dict]:
    """US patent applications via USPTO's Open Data Portal, which replaced the old keyless
    PatentsView API. Needs a free key from data.uspto.gov (USPTO_ODP_API_KEY); skipped without one."""
    key = os.environ.get("USPTO_ODP_API_KEY")
    if not key:
        return []
    try:
        r = await http(f"https://api.uspto.gov/api/v1/patent/applications/search?q={quote(query)}&limit={limit}",
                       headers={"X-API-KEY": key})
    except RuntimeError as e:
        if "HTTP 404" in str(e):  # ODP's answer to a valid query with no matches
            return []
        raise
    out = []
    for a in r.json().get("patentFileWrapperDataBag", [])[:limit]:
        meta = a.get("applicationMetaData", {})
        num = a.get("applicationNumberText", "")
        granted = meta.get("patentNumber")  # bare digits; Google Patents wants the US prefix
        published = meta.get("earliestPublicationNumber") or (f"US{granted}" if granted else "")
        url = (f"https://patents.google.com/patent/{published}" if published
               else f"https://data.uspto.gov/patent-file-wrapper/search/details/{num}/application-data")
        out.append({"source": "patents", "title": meta.get("inventionTitle", ""), "url": url, "snippet": "",
                    "date": meta.get("filingDate", ""),
                    "info": " · ".join(x for x in (meta.get("applicationStatusDescriptionText"),
                                                   (meta.get("firstInventorName") or "")) if x)})
    return out


async def code(query: str, limit: int) -> list[dict]:
    """Source code search across public GitHub repos, via grep.app."""
    r = await http(f"https://grep.app/api/search?q={quote(query)}")
    out = []
    for h in r.json().get("hits", {}).get("hits", [])[:limit]:
        c = h.get("content", {})
        line = re.search(r'data-line="(\d+)"', c.get("snippet", ""))
        out.append({"source": "code", "title": f"{h['repo']}: {h['path']}",
                    "url": f"https://github.com/{h['repo']}/blob/{h.get('branch', 'HEAD')}/{h['path']}"
                          + (f"#L{line.group(1)}" if line else ""),
                    "snippet": _code_lines(c.get("snippet", "")), "date": "", "info": h.get("branch", "")})
    return out


def _code_lines(snippet: str, limit: int = 6) -> str:
    """grep.app's highlighted HTML table as "54: code" lines."""
    rows = re.findall(r'<tr data-line="(\d+)">.*?<pre>(.*?)</pre>', snippet, re.DOTALL)[:limit]
    code = textwrap.dedent("\n".join(htmllib.unescape(re.sub(r"<[^>]+>", "", c)).rstrip() for _, c in rows))
    return "\n  ".join(f"{n}: {line}" for (n, _), line in zip(rows, code.split("\n")))  # indented under the result


async def wiktionary(query: str, limit: int) -> list[dict]:
    try:
        data = (await http(f"https://en.wiktionary.org/api/rest_v1/page/definition/{quote(query.strip())}")).json()
    except RuntimeError as e:
        if "HTTP 404" in str(e):  # no entry for this word
            return []
        raise
    out = []
    for lang, senses in data.items():
        for sense in senses:
            defs = [_clean(d.get("definition")) for d in sense.get("definitions", [])[:3]]
            if not defs:
                continue
            out.append({"source": "wiktionary", "title": f"{query.strip()} ({lang})",
                        "url": f"https://en.wiktionary.org/wiki/{quote(query.strip())}",
                        "snippet": " | ".join(defs), "date": "", "info": sense.get("partOfSpeech", "")})
    return out[:limit]


async def history(query: str, limit: int) -> list[dict]:
    """Pages any agent has already read through this server (the local full-text index)."""
    return await asyncio.to_thread(index.search, query, limit)


SOURCES = {f.__name__: f for f in (wikipedia, hackernews, stackoverflow, github, openreview, huggingface_papers,
                                   huggingface_models, lemmy, packages, wikidata, clinicaltrials, openfda,
                                   courtlistener, patents, code, wiktionary, history)}
DEFAULT = ["wikipedia", "hackernews", "stackoverflow", "github", "openreview", "huggingface_papers"]


async def _guarded(name: str, query: str, limit: int):
    """Run one source, skipping it while health has it marked down and recording the outcome."""
    wait = health.skipped(name)
    if wait:
        return "skipped", wait
    try:
        res = await SOURCES[name](query, limit)
    except Exception as e:  # noqa: BLE001 - one bad source shouldn't cancel the others
        health.record(name, False)
        return "error", e
    health.record(name, True)
    return "ok", res


async def search(query: str, names: list[str], limit: int) -> str:
    runs = await asyncio.gather(*(_guarded(n, query, limit) for n in names))
    parts, notes = [], []
    for name, (status, payload) in zip(names, runs):
        if status == "skipped":
            notes.append(f"{name}: skipped, retrying in {int(payload // 60) + 1} min")
            continue
        if status == "error":
            notes.append(f"{name}: failed ({type(payload).__name__}: {payload})"[:160])
            continue
        notes.append(f"{name}: {len(payload)}")
        if payload:
            parts.append(f"== {name} ==\n" + "\n".join(
                f"{r['title']}\n  {r['url']}\n  " + " | ".join(x for x in (r["date"], r["info"]) if x)
                + (f"\n  {r['snippet']}" if r["snippet"] else "") for r in payload))
    return f"sources: {', '.join(notes)}\n\n" + "\n\n".join(parts)
