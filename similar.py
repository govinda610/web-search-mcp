"""Find pages like a given one: pull its key terms, search for them, and rerank the results
against its own lead text. No LLM -- term frequency plus the search/rerank building blocks
already in this repo.
"""
import re
from urllib.parse import urlparse

import fetch
import rerank

_STOPWORDS = frozenset([
    "a", "an", "the", "and", "or", "but", "if", "of", "to", "in", "on", "for", "with", "as", "at",
    "by", "from", "into", "onto", "over", "under", "is", "are", "was", "were", "be", "been", "being",
    "this", "that", "these", "those", "it", "its", "your", "you", "our", "we", "they", "them",
    "he", "she", "his", "her", "not", "no", "yes", "so", "than", "then", "also", "can", "will",
    "would", "could", "should", "may", "might",
])
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z'-]{2,}")
_TITLE_RE = re.compile(r"(?m)^(?:title:|#)\s+(.+)")  # front matter first, else the first heading
_LEAD_CHARS = 600  # roughly the opening paragraph or two


def _title(text: str) -> str:
    match = _TITLE_RE.search(text)
    return match.group(1).strip().strip('"') if match else ""


def _key_terms(text: str, top: int = 10) -> list[str]:
    """The most frequent non-stopword words in text, most frequent first."""
    counts: dict[str, int] = {}
    for w in _WORD_RE.findall(text.lower()):
        if w not in _STOPWORDS:
            counts[w] = counts.get(w, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:top]]


async def find_similar(url: str, search, num_results: int = 10) -> tuple[str, list[dict]]:
    """(title of url, pages similar to it): fetches url, searches for its title and top terms,
    drops results from its own domain, and reranks the rest against its lead text.
    search(query, n) -> list of result dicts with title, url and snippet."""
    _, text = await fetch.fetch_text(url, interactive=False)
    title = _title(text) or url
    terms = _key_terms(text)
    host = urlparse(url).hostname or ""
    queries = [title]
    if terms:
        queries.append(" ".join(terms[:6]))
    if len(terms) > 6:
        queries.append(" ".join(terms[6:]))

    seen, candidates = {url}, []
    for q in queries:
        for item in await search(q, num_results * 2):
            u = item.get("url", "")
            if not u or u in seen or urlparse(u).hostname == host:
                continue
            seen.add(u)
            candidates.append(item)
    if not candidates:
        return title, []

    passages = [f"{c.get('title', '')}\n{c.get('snippet', '')}" for c in candidates]
    by_passage = dict(zip(passages, candidates))
    ranked = await rerank.arerank(text[:_LEAD_CHARS], passages, top_k=num_results)
    return title, [by_passage[passage] for _score, passage in ranked]
