"""Watch list: get notified (macOS notification) when a release meeting a quality bar shows up.

State is a dict of watches keyed by lowercased query, in store.STATE/"watches.json". check_all()
re-runs media.search() for every watch and notifies once per new hit (tracked via last_notified),
so re-running it on a schedule (run_forever) doesn't repeat the same notification.
"""
import asyncio
import subprocess
import sys

import media
import quality
from store import STATE, load_json, save_json

WATCHES = STATE / "watches.json"


def add(query: str, category: str = "movies", min_quality: str = "WEB-DL") -> str:
    watches = load_json(WATCHES)
    watches[query.lower()] = {"query": query, "category": category, "min_quality": min_quality}
    save_json(WATCHES, watches)
    return f"watching: {query} ({category}, min quality {min_quality})"


def remove(query: str) -> str:
    watches = load_json(WATCHES)
    if watches.pop(query.lower(), None) is None:
        return f"not watching: {query}"
    save_json(WATCHES, watches)
    return f"stopped watching: {query}"


def list_watches() -> str:
    watches = load_json(WATCHES)
    if not watches:
        return "no watches"
    return "\n".join(f"- {w['query']} ({w['category']}, min quality {w['min_quality']})"
                     for w in watches.values())


async def check_all() -> list[str]:
    """Re-search every watch; notify (once per hit) on the best clean (non-cam, unflagged) result
    at or above its min_quality tier. Returns one status line per watch."""
    watches = load_json(WATCHES)
    results = []
    for w in watches.values():
        try:
            _, found, _ = await media.search(w["query"], w.get("category", "movies"), limit=10)
        except Exception as e:  # noqa: BLE001 - one watch failing shouldn't stop the others
            results.append(f"{w['query']}: check failed ({type(e).__name__}: {e})"[:160])
            continue
        min_tier = quality.TIERS.get(w.get("min_quality", "WEB-DL"), 0)
        hit = next((r for r in found if r.get("tier", -1) >= min_tier
                   and not r.get("is_cam") and not r.get("warnings")), None)
        if not hit:
            results.append(f"{w['query']}: nothing new yet")
        elif w.get("last_notified") == hit["title"]:
            results.append(f"{w['query']}: already notified ({hit['title']})")
        else:
            notify(w["query"], f"{hit['title']} ({hit.get('quality_label', '')})")
            w["last_notified"] = hit["title"]
            results.append(f"{w['query']}: notified ({hit['title']})")
    # re-read: a check takes minutes, and watches may have been added or removed meanwhile
    latest = load_json(WATCHES)
    for key, w in watches.items():
        if key in latest and "last_notified" in w:
            latest[key]["last_notified"] = w["last_notified"]
    save_json(WATCHES, latest)
    return results


def notify(title: str, message: str) -> None:
    """macOS notification via osascript. Passed as a single argv element (no shell=True), so
    only AppleScript's own string-literal escaping is needed, not shell quoting."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{esc(message)}" with title "{esc(title)}"'
    subprocess.run(["osascript", "-e", script], capture_output=True, check=False)


async def run_forever(interval_hours: float = 6) -> None:
    while True:
        for line in await check_all():
            print(line)
        await asyncio.sleep(interval_hours * 3600)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "add" and len(sys.argv) > 2:
        print(add(sys.argv[2], *sys.argv[3:5]))
    elif cmd == "remove" and len(sys.argv) > 2:
        print(remove(sys.argv[2]))
    elif cmd == "list":
        print(list_watches())
    elif cmd == "check":
        for line in asyncio.run(check_all()):
            print(line)
    elif cmd == "run":
        asyncio.run(run_forever(float(sys.argv[2]) if len(sys.argv) > 2 else 6))
    else:
        print("usage: watch.py add <query> [category] [min_quality] | remove <query> | list | check | run [interval_hours]")
