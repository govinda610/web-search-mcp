"""Multi-round deep research: plan sub-queries, search, read the best sources, extract
relevant passages, summarize with remaining gaps, repeat, then synthesize a cited report.

Everything that talks to the outside world (search, page reads, the LLM, progress) is
injected as a callable, so this module stays self-contained and easy to test with fakes."""
import asyncio
import json
import re
from collections.abc import Awaitable, Callable

import rerank

SearchFn = Callable[[str, int], Awaitable[list[dict]]]
ReadFn = Callable[[str], Awaitable[str]]
AskFn = Callable[[str, int], Awaitable[str | None]]
ProgressFn = Callable[[float, float, str], Awaitable[None]]

MAX_SUBQUERIES = 5
READ_CONCURRENCY = 4
READ_TIMEOUT = 45
RESULTS_PER_QUERY = 5
_DEPTH_PRESETS = {"standard": (2, 8), "deep": (4, 16)}  # depth -> (max_rounds, max_sources)


async def _noop_progress(done: float, total: float, message: str) -> None:
    return None


def _parse_json(text: str | None) -> dict:
    """First {...} block in text, or {} if there isn't a parseable one."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def _interleave_urls(runs: list[list[dict]]) -> list[dict]:
    """Each sub-query's best result, then each one's second best, ..., deduped by URL."""
    out, seen = [], set()
    for rank in range(max((len(r) for r in runs), default=0)):
        for run in runs:
            if rank < len(run) and run[rank]["url"] not in seen:
                seen.add(run[rank]["url"])
                out.append(run[rank])
    return out


def _format_sources(sources: list[dict]) -> str:
    return "\n".join(f"[{s['n']}] {s['title']} — {s['url']}" for s in sources)


async def _plan_subqueries(question: str, ask: AskFn) -> list[str] | None:
    """3-5 search queries covering the question's angles. None means ask() has no LLM at all."""
    raw = await ask(
        "Break this research question into 3-5 focused web search queries covering its "
        'different angles. Reply with JSON only: {"queries": ["...", ...]}.\n\n'
        f"Question: {question}", 400)
    if raw is None:
        return None
    queries = [q.strip() for q in _parse_json(raw).get("queries", []) if isinstance(q, str) and q.strip()]
    return queries[:MAX_SUBQUERIES] or [question]


async def _read_sources(candidates: list[dict], read: ReadFn, budget: int) -> list[dict]:
    """Read up to `budget` candidates in parallel (bounded concurrency), skipping failures.
    Returns the ones that were read successfully, each with a "text" key added."""
    sem = asyncio.Semaphore(READ_CONCURRENCY)
    picked = candidates[:budget]

    async def one(c: dict) -> None:
        async with sem:
            try:
                c["text"] = await asyncio.wait_for(read(c["url"]), READ_TIMEOUT)
            except Exception:  # noqa: BLE001 - a failed page is skipped, not fatal
                c["text"] = None
    await asyncio.gather(*(one(c) for c in picked))
    return [c for c in picked if c["text"]]


def _degraded_report(question: str, queries: list[str], sources: list[dict], notes: dict[int, str]) -> str:
    """No LLM was available: a citation dump instead of a synthesized report."""
    lines = [f"# Research: {question}", "",
             ("_No LLM was available for synthesis; below are the sub-queries used and the most "
              "relevant passages found per source._"), "",
             "Sub-queries used: " + ", ".join(queries), ""]
    for s in sources:
        lines.append(f"## [{s['n']}] {s['title']}")
        if s["n"] in notes:
            lines.append(notes[s["n"]])
        lines.append(s["passages"])
        lines.append("")
    lines.append("## Sources")
    lines.append(_format_sources(sources))
    return "\n".join(lines)


async def deep_research(
    question: str,
    search: SearchFn,
    read: ReadFn,
    ask: AskFn,
    progress: ProgressFn | None = None,
    max_rounds: int | None = None,
    max_sources: int | None = None,
    depth: str = "standard",
) -> str:
    """Research `question` across several rounds and return a markdown report with [n]
    citations and a Sources list. depth picks (max_rounds, max_sources) presets
    ("standard": 2/8, ~4 min; "deep": 4/16, ~8 min); pass either explicitly to override.
    Degrades to a citation dump (no synthesis) when ask() reports no LLM is available at all."""
    progress = progress or _noop_progress
    preset_rounds, preset_sources = _DEPTH_PRESETS.get(depth, _DEPTH_PRESETS["standard"])
    max_rounds = max_rounds or preset_rounds
    max_sources = max_sources or preset_sources

    subqueries = await _plan_subqueries(question, ask)
    no_llm = subqueries is None
    if no_llm:
        subqueries = [question]

    sources: list[dict] = []  # each: n, title, url, query, text, passages
    notes: dict[int, str] = {}
    seen_urls: set[str] = set()
    used_queries = list(subqueries)
    round_no = 0

    while subqueries and len(sources) < max_sources and round_no < max_rounds:
        round_no += 1
        await progress(round_no - 1, max_rounds, f"round {round_no}: searching {len(subqueries)} queries")
        runs = await asyncio.gather(*(search(q, RESULTS_PER_QUERY) for q in subqueries), return_exceptions=True)
        tagged = [[{**r, "query": q} for r in run] for q, run in zip(subqueries, runs) if not isinstance(run, Exception)]
        candidates = [r for r in _interleave_urls(tagged) if r["url"] not in seen_urls]

        budget = max_sources - len(sources)
        await progress(round_no - 0.5, max_rounds, f"round {round_no}: reading up to {min(len(candidates), budget)} sources")
        read_batch = await _read_sources(candidates, read, budget)
        for c in read_batch:
            seen_urls.add(c["url"])
            c["n"] = len(sources) + 1
            c["passages"] = rerank.best_passages(c["text"], question)
            sources.append(c)

        if not read_batch or no_llm:
            break

        batch_text = "\n\n".join(f"[{c['n']}] {c['title']} ({c['url']})\n{c['passages'][:1200]}" for c in read_batch)
        raw = await ask(
            f"Research question: {question}\n\nFor each numbered source below, write ONE short "
            "note (1-2 sentences) capturing what it adds. Then list up to 5 remaining gaps: "
            "specific things still unanswered, phrased as search queries (empty list if none). "
            'Reply with JSON only: {"notes": {"<n>": "..."}, "gaps": ["...", ...]}.\n\n' + batch_text, 1200)
        parsed = _parse_json(raw)
        for k, v in parsed.get("notes", {}).items():
            try:
                notes[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
        gaps = [g.strip() for g in parsed.get("gaps", []) if isinstance(g, str) and g.strip()]
        gaps = [g for g in gaps if g not in used_queries][:5]
        used_queries.extend(gaps)
        subqueries = gaps

    if not sources:
        await progress(max_rounds, max_rounds, "no sources found")
        return f"No sources could be read for: {question}\nSub-queries tried: {', '.join(used_queries)}"
    if no_llm:
        await progress(max_rounds, max_rounds, "no LLM available; compiling sources")
        return _degraded_report(question, used_queries, sources, notes)

    await progress(max_rounds, max_rounds, "writing report")
    numbered_notes = "\n".join(f"[{s['n']}] {notes.get(s['n'], s['passages'][:300])}" for s in sources)
    report = await ask(
        f"Write a thorough research report answering: {question}\n\nUse ONLY the numbered notes "
        "below; cite claims inline as [n] matching the source number. End with a '## Sources' "
        "section listing each source as '[n] Title — URL'.\n\n" + numbered_notes, 3000)
    if not report:
        return _degraded_report(question, used_queries, sources, notes)
    if "## Sources" not in report:
        report += "\n\n## Sources\n" + _format_sources(sources)
    return report
