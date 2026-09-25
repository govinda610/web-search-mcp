"""Small JSON state files under state/, written atomically so a crash or a second server
instance can never leave a half-written file behind."""
import json
import os
import tempfile
from pathlib import Path

STATE = Path(__file__).parent / "state"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}  # missing or corrupt: start fresh rather than break every call


def save_json(path: Path, data: dict, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    if not private:
        os.chmod(tmp, 0o644)  # mkstemp creates 600; only secrets should stay that way
    os.replace(tmp, path)
