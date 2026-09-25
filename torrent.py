"""Download a magnet link: the one step after media.search() finds one. aria2c is the CLI
that does the actual BitTorrent work; this just drives it and reports what landed on disk.
"""
import asyncio
import shutil
import time
from pathlib import Path

import media

ARIA2C = shutil.which("aria2c")
STALL_SECONDS = 300  # give up when nothing has arrived for this long (dead torrent, no seeders)


async def download(magnet: str, folder: str, on_progress=None,
                   seed: bool = False, timeout: int | None = None) -> str:
    """Fetch one magnet with aria2c into folder. on_progress(line), if given, is awaited with
    each progress line aria2c prints while downloading. A torrent that stalls for
    STALL_SECONDS is abandoned. seed=False (the default) stops
    the moment the download completes (--seed-time=0); seed=True keeps seeding until timeout
    or the process is killed. Returns the saved file name(s) and total size, or raises with a
    clear message if aria2c isn't installed, times out, or exits with an error."""
    if not ARIA2C:
        raise RuntimeError("aria2c not found. Install it: brew install aria2")
    dest = Path(folder).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    started = time.time()
    args = [ARIA2C, magnet, "--dir", str(dest), "--summary-interval=2", "--console-log-level=warn",
            f"--bt-stop-timeout={STALL_SECONDS}"]
    if not seed:
        args.append("--seed-time=0")
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)

    last = {"line": ""}

    async def stream():
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if line.startswith("[#"):
                last["line"] = line
            if line.startswith("[#") and on_progress:  # "[#id 1.2MiB/700MiB(0%) CN:5 DL:1.1MiB ETA:10m]"
                await on_progress(line)

    try:
        await asyncio.wait_for(asyncio.gather(stream(), proc.wait()), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"download timed out after {timeout}s")
    if proc.returncode == 7:  # aria2c's "unfinished download": --bt-stop-timeout gave up on it
        raise RuntimeError(f"nothing arrived for {STALL_SECONDS}s, so it was abandoned (no reachable "
                           f"seeders?). Last status: {last['line'] or 'none'}")
    if proc.returncode != 0:
        raise RuntimeError(f"aria2c exited with status {proc.returncode}. Last status: {last['line'] or 'none'}")

    saved = sorted((f, f.stat().st_size) for f in dest.rglob("*")
                   if f.is_file() and f.suffix != ".aria2" and f.stat().st_mtime >= started)
    if not saved:
        raise RuntimeError("aria2c finished but no new file was found in the folder")
    total = sum(size for _, size in saved)
    names = ", ".join(f.name for f, _ in saved)
    return f"saved {len(saved)} file(s), {media._size(total)}, to {dest}: {names}"
