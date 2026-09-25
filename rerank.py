"""Passage reranking: fastembed cross-encoder, falling back to a small BM25 when the model
can't be loaded (not installed, offline, disk full, ...). Used to pick the paragraphs most
relevant to a query out of a fetched page or a batch of research passages."""
import asyncio
import math
import re
import threading
from collections import Counter

from store import STATE

_MODEL_NAME = "Xenova/ms-marco-MiniLM-L-6-v2"
_CANDIDATE_CAP = 200  # a cross-encoder call above this many passages is slow; BM25 shortlists first
_model = None
_model_failed = False
_load_lock = threading.Lock()  # pages are reranked in parallel threads; load the model once


def _load_model():
    """Lazy singleton; None (and remembered) if fastembed isn't installed or loading fails."""
    global _model, _model_failed
    with _load_lock:
        if _model is not None or _model_failed:
            return _model
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            _model = TextCrossEncoder(model_name=_MODEL_NAME, cache_dir=str(STATE / "models"))
        except Exception:  # noqa: BLE001 - any load failure falls back to BM25
            _model_failed = True
        return _model


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def _bm25(query: str, passages: list[str], top_k: int) -> list[tuple[float, str]]:
    """Minimal BM25 (k1=1.5, b=0.75): no index, just scores this one passage list against query."""
    q_terms = _tokenize(query)
    docs = [_tokenize(p) for p in passages]
    lengths = [len(d) for d in docs]
    avgdl = (sum(lengths) / len(lengths)) or 1
    df = Counter()
    for d in docs:
        df.update(set(d))
    n = len(docs)
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q_terms}
    k1, b = 1.5, 0.75
    scored = []
    for p, d, length in zip(passages, docs, lengths):
        tf = Counter(d)
        score = sum(idf[t] * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * length / avgdl)) for t in q_terms)
        scored.append((score, p))
    return sorted(scored, key=lambda x: x[0], reverse=True)[:top_k]


def rerank(query: str, passages: list[str], top_k: int = 8) -> list[tuple[float, str]]:
    """The top_k passages most relevant to query, each with its score (higher = more
    relevant), best first. Cross-encoder when it loads; BM25 otherwise."""
    if not passages:
        return []
    model = _load_model()
    if model is None:
        return _bm25(query, passages, top_k)
    candidates = passages
    if len(candidates) > _CANDIDATE_CAP:  # BM25 shortlist keeps the cross-encoder call fast
        candidates = [p for _, p in _bm25(query, candidates, _CANDIDATE_CAP)]
    scores = list(model.rerank(query, candidates))
    return sorted(zip(scores, candidates), key=lambda x: x[0], reverse=True)[:top_k]


async def arerank(query: str, passages: list[str], top_k: int = 8) -> list[tuple[float, str]]:
    """rerank() off the event loop: the cross-encoder call is CPU-bound."""
    return await asyncio.to_thread(rerank, query, passages, top_k)


def split_passages(text: str, size: int = 600) -> list[str]:
    """Paragraph-aware chunks of about `size` chars: paragraphs are packed together up to
    the limit; a paragraph longer than size is split on its own."""
    text = re.sub(r"(?s)\A---\n.*?\n---\n", "", text)  # trafilatura's metadata header
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paragraphs:
        if len(p) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(p[i:i + size] for i in range(0, len(p), size))
            continue
        if buf and len(buf) + len(p) + 2 > size:
            chunks.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return chunks


def best_passages(text: str, query: str, limit: int = 1500) -> str:
    """The passages most relevant to query, in document order, joined within limit chars.
    Reranked drop-in replacement for server._best_passages' keyword-overlap heuristic."""
    chunks = split_passages(text, size=min(600, limit))  # a chunk bigger than limit would get cut off
    if not chunks:
        return ""
    order = {p: i for i, p in enumerate(chunks)}
    picked, total = [], 0
    for _, p in rerank(query, chunks, top_k=len(chunks)):
        if total + len(p) > limit and picked:
            break
        picked.append(p)
        total += len(p)
    picked.sort(key=lambda p: order[p])
    return "\n\n".join(picked)[:limit]
