"""Watch two community-maintained lists for new sources, so they can be added over time.

- Prowlarr/Indexers: definitions for ~550 torrent/usenet sites, with their current domains.
- FMHY (freemediaheckyeah): starred (⭐) picks for books, audio, video, games, torrents...

Each run compares against a snapshot in state/discovery.json and reports what's new, including
starred sites whose domains changed (the usual sign of a move). The first run saves the snapshot.
"""
import asyncio
import re
import time

from media import http
from store import STATE, load_json, save_json

SNAPSHOT = STATE / "discovery.json"
PROWLARR = "https://api.github.com/repos/Prowlarr/Indexers/contents/definitions/v11"
PROWLARR_RAW = "https://raw.githubusercontent.com/Prowlarr/Indexers/master/definitions/v11/"
FMHY_RAW = "https://raw.githubusercontent.com/fmhy/edit/main/docs/"
FMHY_PAGES = ["reading", "audio", "video", "gaming", "torrenting", "downloading", "educational", "ai",
              "developer-tools"]
STARRED = re.compile(r"^\* ⭐ \*\*\[([^\]]+)\]\(([^)]+)\)\*\*((?:, \[\d+\]\([^)]+\))*)(?: - (.*))?$")
MAX_DETAILS = 30  # new Prowlarr definitions to open for their type and links


async def _prowlarr_names() -> list[str]:
    listing = (await http(PROWLARR, headers={"Accept": "application/vnd.github+json"})).json()
    return sorted(f["name"].removesuffix(".yml") for f in listing if f["name"].endswith(".yml"))


async def _prowlarr_detail(name: str) -> str:
    try:
        yml = (await http(PROWLARR_RAW + name + ".yml")).text
    except Exception as e:  # noqa: BLE001 - one unreadable definition shouldn't hide the rest
        return f"{name}: (definition unreadable: {e})"
    field = {k: (re.search(rf"(?m)^{k}:\s*\"?(.*?)\"?\s*$", yml) or [None, "?"])[1]
             for k in ("name", "type", "description")}
    links = re.findall(r"(?m)^  - (https?://\S+)", yml.split("legacylinks:")[0])
    return f"{field['name']} [{field['type']}] - {field['description']}\n  {' '.join(links[:3])}"


async def _fmhy_starred() -> dict[str, dict]:
    # all pages or none: a page that failed to load would otherwise read as every pick on it being dropped
    pages = await asyncio.gather(*(http(FMHY_RAW + p + ".md", timeout=30) for p in FMHY_PAGES))
    starred = {}
    for page, r in zip(FMHY_PAGES, pages):
        section = page
        for line in r.text.splitlines():
            if line.startswith("#"):
                section = f"{page} / {line.lstrip('#► ▷').strip()}"
            m = STARRED.match(line.strip())
            if m:
                urls = [m.group(2)] + re.findall(r"\]\(([^)]+)\)", m.group(3))
                desc = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", m.group(4) or "")
                starred[m.group(1)] = {"urls": urls, "section": section, "desc": desc[:120]}
    return starred


def _domains(urls: list[str]) -> set[str]:
    return {re.sub(r"^https?://(www\.)?", "", u).split("/")[0] for u in urls}


async def discover() -> str:
    names, starred = await asyncio.gather(_prowlarr_names(), _fmhy_starred())
    old = load_json(SNAPSHOT)
    save_json(SNAPSHOT, {"prowlarr": names, "fmhy": starred, "checked": time.strftime("%Y-%m-%d")})
    if not old:
        return (f"Baseline saved: {len(names)} Prowlarr indexer definitions and {len(starred)} FMHY starred sites. "
                "Run discover_sources again later to see what has been added or moved since today.")

    report = [f"Changes since {old.get('checked', 'the last check')}:"]
    added = [n for n in names if n not in set(old.get("prowlarr", []))]
    removed = [n for n in old.get("prowlarr", []) if n not in set(names)]
    if added:
        details = await asyncio.gather(*(_prowlarr_detail(n) for n in added[:MAX_DETAILS]))
        report.append(f"\n== New Prowlarr indexers ({len(added)}) ==\n" + "\n".join(details)
                      + (f"\n...and {len(added) - MAX_DETAILS} more" if len(added) > MAX_DETAILS else ""))
    if removed:
        report.append(f"\n== Prowlarr indexers removed (site died or was dropped) ==\n{', '.join(removed)}")

    old_fmhy = old.get("fmhy", {})
    new_picks = [f"{n} ({s['section']}) - {s['desc']}\n  {' '.join(s['urls'][:3])}"
                 for n, s in starred.items() if n not in old_fmhy]
    moved = [f"{n}: {' '.join(sorted(_domains(old_fmhy[n]['urls'])))} -> {' '.join(sorted(_domains(s['urls'])))}"
             for n, s in starred.items() if n in old_fmhy and _domains(s["urls"]) != _domains(old_fmhy[n]["urls"])]
    dropped = [n for n in old_fmhy if n not in starred]
    if new_picks:
        report.append(f"\n== New FMHY starred sites ({len(new_picks)}) ==\n" + "\n".join(new_picks))
    if moved:
        report.append("\n== FMHY starred sites with changed domains ==\n" + "\n".join(moved))
    if dropped:
        report.append(f"\n== No longer starred on FMHY ==\n{', '.join(dropped)}")
    if len(report) == 1:
        report.append("Nothing new.")
    return "\n".join(report)
