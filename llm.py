"""LLM calls: MCP sampling (the connected client's own model) first when available, else
coding-plan models (pi's models.json). Graceful: returns None on failure.

Provider chain: zai glm-5.3-flash -> qwen qwen3.8-flash -> minimax MiniMax-M2.7.
All anthropic-messages compatible. Quota-tracked as llm:<name>."""
import json
import os
from pathlib import Path

import httpx
from mcp.server.mcpserver import Context
from mcp.types import SamplingMessage, TextContent

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


def can_sample(ctx: Context | None) -> bool:
    """Whether ctx is a live request whose client declared the sampling capability."""
    if ctx is None:
        return False
    try:
        caps = ctx.client_capabilities
    except ValueError:  # ctx not attached to a request
        return False
    return caps is not None and caps.sampling is not None


async def _ask_client(ctx: Context, prompt: str, max_tokens: int, system: str) -> str | None:
    """Ask the connected client's own model via MCP sampling. None on any failure or refusal."""
    try:
        result = await ctx.session.create_message(
            messages=[SamplingMessage(role="user", content=TextContent(type="text", text=prompt))],
            max_tokens=max_tokens,
            system_prompt=system or None)
    except Exception:  # noqa: BLE001 - client declined, doesn't support it, or errored
        return None
    if not isinstance(result.content, TextContent):
        return None
    return result.content.text.strip() or None


async def ask(prompt: str, max_tokens: int = 600, system: str = "", ctx: Context | None = None) -> str | None:
    """Try MCP sampling via ctx first, then each coding-plan model in order. Returns text or None (never raises)."""
    if not prompt:
        return None
    if can_sample(ctx):
        text = await _ask_client(ctx, prompt, max_tokens, system)
        if text:
            quota.record("llm:client-sampling")
            return text
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
