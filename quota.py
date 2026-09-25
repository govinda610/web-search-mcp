"""Per-provider monthly usage tracking. State: state/usage-YYYY-MM.json (human-readable)."""
from datetime import date
from pathlib import Path

from store import STATE as STATE_DIR
from store import load_json, save_json


def _usage_file() -> Path:
    return STATE_DIR / f"usage-{date.today().strftime('%Y-%m')}.json"


def _load() -> dict:
    return load_json(_usage_file())


def record(provider: str) -> None:
    """Increment this month's counter for a provider."""
    usage = _load()
    usage[provider] = usage.get(provider, 0) + 1
    save_json(_usage_file(), usage)


def used_this_month(provider: str) -> int:
    return _load().get(provider, 0)


def remaining(provider: str, monthly_limit) -> int | None:
    """None = unlimited."""
    if monthly_limit is None:
        return None
    return max(0, monthly_limit - used_this_month(provider))


def llm_usage() -> dict:
    """LLM call counts (keys like llm:zai) from this month's ledger."""
    return {k: v for k, v in _load().items() if k.startswith("llm:")}


def status_table(providers: list[dict]) -> str:
    """Human/LLM-readable usage summary across all providers."""
    lines = ["provider     used_this_month   limit       remaining", "-" * 58]
    for p in providers:
        used = used_this_month(p["name"])
        limit = p["monthly_limit"]
        rem = "unlimited" if limit is None else max(0, limit - used)
        lines.append(f"{p['name']:<12} {used:>8} {str(limit):>12} {str(rem):>12}")
    return "\n".join(lines)
