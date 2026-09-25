"""Skip sources that keep failing. After FAILS_TO_SKIP failures in a row a source is skipped
for SKIP_SECONDS, so a dead community API costs one timeout, not one per call."""
import time

FAILS_TO_SKIP = 2
SKIP_SECONDS = 600
_fails: dict[str, int] = {}
_skip_until: dict[str, float] = {}


def skipped(name: str) -> float:
    """Seconds left before `name` is tried again; 0 if it can be tried now."""
    return max(0.0, _skip_until.get(name, 0) - time.time())


def record(name: str, ok: bool) -> None:
    if ok:
        _fails.pop(name, None)
        _skip_until.pop(name, None)
        return
    _fails[name] = _fails.get(name, 0) + 1
    if _fails[name] >= FAILS_TO_SKIP:
        _skip_until[name] = time.time() + SKIP_SECONDS


def report() -> list[str]:
    """One line per source currently being skipped."""
    return [f"{name}: failing, retried in {int(skipped(name) // 60) + 1} min"
            for name in sorted(_skip_until) if skipped(name)]
