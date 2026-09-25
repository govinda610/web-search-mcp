"""Self-healing mirror lists for sites whose domains keep changing.

Each site has seed domains. The domain that last worked is tried first; a failing one
drops to the back. When every known domain fails, the list is refreshed (at most every
6 hours) from a maintained source:
  prowlarr: the Prowlarr indexer definition for the site (updated almost daily on GitHub)
  slum:     open-slum.org, the shadow-library uptime monitor (Anna's Archive, LibGen, ...)
A new domain is only kept if the site's own adapter parses real results from it, so
parked domains and malware clones that merely answer HTTP 200 are rejected.
"""
import re
import time

from curl_cffi import AsyncSession

from store import STATE as STATE_DIR
from store import load_json, save_json

STATE = STATE_DIR / "mirrors.json"
REFRESH_AFTER = 6 * 3600
PROWLARR = "https://raw.githubusercontent.com/Prowlarr/Indexers/master/definitions/v11/{}.yml"
SLUM_PAGES = ("https://open-slum.org/", "https://open-slum.pages.dev/")


class MirrorError(Exception):
    pass


_state: dict | None = None  # one shared copy, so parallel searches don't overwrite each other


def _load() -> dict:
    global _state
    if _state is None:
        _state = load_json(STATE)
    return _state


def _save(state: dict) -> None:
    save_json(STATE, state)


async def _get(url: str) -> str:
    async with AsyncSession() as s:
        r = await s.get(url, impersonate="chrome", timeout=20)
    r.raise_for_status()
    return r.text


async def _from_prowlarr(name: str) -> list[str]:
    text = await _get(PROWLARR.format(name))
    block = re.search(r"(?m)^links:\n((?:\s+(?:- |#).*\n)+)", text)  # the list may hold comment lines
    return [u.strip().rstrip("/") for u in re.findall(r"(?m)^\s+- (\S+)", block.group(1))] if block else []


async def _from_slum(keyword: str) -> list[str]:
    """Domains SLUM reports as UP or PROTECTED (behind a browser check), UP first."""
    for page in SLUM_PAGES:
        try:
            html = await _get(page)
            break
        except Exception:  # noqa: BLE001, S112 - the Cloudflare-fronted .org sometimes challenges; try the mirror
            continue
    else:
        return []
    pairs = re.findall(r'class="domain-link"[^>]*title="(https?://[^"]+)".*?status-badge compact (\w+)', html, re.DOTALL)
    matching = [(url, status) for url, status in pairs if keyword in url]
    return ([u for u, s in matching if s == "up"] + [u for u, s in matching if s == "protected"])


async def refresh(site: str, updates: dict) -> list[str]:
    if "prowlarr" in updates:
        return await _from_prowlarr(updates["prowlarr"])
    if "slum" in updates:
        return await _from_slum(updates["slum"])
    return []


async def call(site: str, seeds: list[str], updates: dict, attempt):
    """Run attempt(base_url) against the site's domains until one succeeds."""
    state = _load()
    entry = state.setdefault(site, {"domains": list(seeds), "refreshed": 0})
    domains = entry["domains"] + [d for d in seeds if d not in entry["domains"]]
    errors = []

    async def try_all(candidates):
        for base in candidates:
            try:
                result = await attempt(base)
            except Exception as e:  # noqa: BLE001 - any failure moves on to the next mirror
                errors.append(f"{base}: {type(e).__name__}: {e}"[:160])
                continue
            entry["domains"] = [base] + [d for d in domains if d != base]
            _save(state)
            return True, result
        return False, None

    ok, result = await try_all(domains)
    if ok:
        return result
    if updates and time.time() - entry["refreshed"] > REFRESH_AFTER:
        entry["refreshed"] = time.time()
        fresh = []
        try:
            fresh = [d for d in await refresh(site, updates) if d not in domains]
        except Exception as e:  # noqa: BLE001
            errors.append(f"refresh failed: {e}"[:160])
        domains = domains + fresh
        ok, result = await try_all(fresh)
        _save(state)
        if ok:
            return result
    raise MirrorError("; ".join(errors[-4:]))


def status() -> str:
    state = _load()
    return "\n".join(f"  {site}: {e['domains'][0] if e['domains'] else '-'} "
                     f"({len(e['domains'])} known)" for site, e in sorted(state.items()))
