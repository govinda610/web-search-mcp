"""Download video or audio from YouTube and ~1800 other sites with yt-dlp (ffmpeg converts).

Blocking yt-dlp runs in a worker thread; progress is polled into MCP progress notifications,
and a cancelled call stops the download at the next progress tick."""
import asyncio
from pathlib import Path

import yt_dlp

AUDIO = {"mp3", "m4a", "opus", "flac", "wav"}
VIDEO = {"mp4", "mkv", "webm"}
MAX_PLAYLIST_ITEMS = 50


class Cancelled(Exception):
    pass


def _options(fmt: str, max_height: int, folder: Path, playlist: bool, subtitles: bool, hook) -> dict:
    opts = {
        "paths": {"home": str(folder)},
        "outtmpl": "%(title).150B [%(id)s].%(ext)s",
        "windowsfilenames": True,  # no characters that break other tools
        "noplaylist": not playlist,
        "playlistend": MAX_PLAYLIST_ITEMS,
        "overwrites": False,
        "quiet": True,
        "noprogress": True,
        "progress_hooks": [hook],
    }
    if fmt in AUDIO:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": fmt, "preferredquality": "0"},
                                  {"key": "FFmpegMetadata"}]
        return opts
    height = f"[height<={max_height}]" if max_height else ""
    opts["format"] = f"bv*{height}+ba/b{height}"
    opts["merge_output_format"] = fmt
    if fmt == "mp4":  # h264/aac play everywhere; same preference as yt-dlp's own "-t mp4" preset
        opts["format_sort"] = ["vcodec:h264", "lang", "quality", "res", "fps", "hdr:12", "acodec:aac"]
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    if subtitles:
        opts.update(writesubtitles=True, writeautomaticsub=True, subtitleslangs=["en.*", "en"],
                    postprocessors=opts.get("postprocessors", []) + [{"key": "FFmpegEmbedSubtitle"}])
    return opts


def _saved_files(info: dict) -> list[Path]:
    entries = info.get("entries") or [info]
    files = []
    for entry in entries:
        if not entry:
            continue
        for item in entry.get("requested_downloads") or []:
            path = Path(item.get("filepath") or item.get("_filename") or "")
            if path.is_file():
                files.append(path)
    return files


async def download(url: str, fmt: str = "mp4", max_height: int = 1080, folder: Path = Path("."),
                   playlist: bool = False, subtitles: bool = False, on_progress=None) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    state = {"status": "starting", "line": "", "cancel": False}

    def hook(d: dict) -> None:
        if state["cancel"]:
            raise Cancelled("download cancelled")
        name = Path(d.get("filename", "")).name
        if d["status"] == "downloading":
            done, total = d.get("downloaded_bytes") or 0, d.get("total_bytes") or d.get("total_bytes_estimate")
            state["line"] = f"{name}: {done / 1e6:.0f}/{total / 1e6:.0f} MB" if total else f"{name}: {done / 1e6:.0f} MB"
        elif d["status"] == "finished":
            state["line"] = f"{name}: converting"

    def run() -> dict:
        with yt_dlp.YoutubeDL(_options(fmt, max_height, folder, playlist, subtitles, hook)) as ydl:
            return ydl.extract_info(url, download=True)

    worker = asyncio.create_task(asyncio.to_thread(run))
    try:
        while not worker.done():
            await asyncio.sleep(2)
            if on_progress and state["line"]:
                await on_progress(state["line"])
        info = await worker
    except asyncio.CancelledError:
        state["cancel"] = True  # the thread stops at its next progress tick
        raise
    except yt_dlp.utils.DownloadError as e:
        raise RuntimeError(str(e).removeprefix("ERROR: ")) from e

    files = _saved_files(info)
    if not files:
        raise RuntimeError("yt-dlp finished but no file was saved (already downloaded, or nothing matched)")
    title = info.get("title") or url
    lines = [f"Saved {len(files)} file(s) from {title!r} ({info.get('extractor_key', '')}):"]
    lines += [f"  {f} ({f.stat().st_size / 1e6:.1f} MB)" for f in files]
    if info.get("_type") == "playlist" and len(info.get("entries") or []) >= MAX_PLAYLIST_ITEMS:
        lines.append(f"(stopped at the first {MAX_PLAYLIST_ITEMS} playlist items)")
    return "\n".join(lines)

