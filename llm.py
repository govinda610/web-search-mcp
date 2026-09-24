"""LLM calls via coding-plan models (pi's models.json). Graceful: returns None on failure.

Provider chain: zai glm-5.3-flash -> qwen qwen3.8-flash -> minimax MiniMax-M2.7.
All anthropic-messages compatible. Quota-tracked as llm:<name>."""
import asyncio
import json
import os
from pathlib import Path

import httpx

import quota

_MODELS_FILE = Path(os.path.expanduser("~/.pi/agent/models.json"))
_CHAIN = [("zai", "glm-5.3-flash"), ("qwen", "qwen3.8-flash"), ("minimax", "MiniMax-M2.7")]


def _providers():
    try:
        data = json.loads(_MODELS_FILE.read_text())
    except Exception:
        return []
    out = []
    for name, model in _CHAIN:
        p = data.get("providers", {}).get(name, {})
        if p.get("baseUrl") and p.get("apiKey"):
            out.append((name, model, p["baseUrl"].rstrip("/"), p["apiKey"]))
    return out


def llm_available() -> bool:
    return bool(_providers())


async def ask(prompt: str, max_tokens: int = 600, system: str = "") -> str | None:
    """Try each coding-plan model in order. Returns text or None (never raises)."""
    if not prompt:
        return None
    for name, model, base, key in _providers():
        try:
            async with httpx.AsyncClient(timeout=45) as c:
                r = await c.post(
                    f"{base}/v1/messages",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                    json={"model": model, "max_tokens": max_tokens,
                          **({"system": system} if system else {}),
                          "messages": [{"role": "user", "content": prompt}]})
                r.raise_for_status()
                blocks = r.json().get("content", [])
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
            if text:
                quota.record(f"llm:{name}")
                return text
        except Exception:
            continue
    return None
