"""Judge a release from its title: real quality tier vs cinema recording vs outright fake.

RTN (rank-torrent-name) parses the standard scene-naming convention (WEB-DL, BluRay,
resolution, ...) but a fake that just appends an executable/script extension to a
real-looking name still parses as a normal release, so that check is ours.
"""
import re

from RTN import parse

# Higher is better; comparable across every release. Unknown quality (no tag in the title)
# ranks between real-but-uncommon tags and known cinema recordings, never above them.
TIERS = {"BluRay REMUX": 8, "BluRay": 7, "WEB-DL": 6, "WEBRip": 5, "HDTV": 4, "PDTV": 4,
        "DVDRip": 4, "DVD": 4, "unknown": 3, "SCR": 2, "TeleCine": 2, "TeleSync": 1, "CAM": 0}
CAM_QUALITIES = {"CAM", "TeleSync", "TeleCine", "SCR"}
BAD_EXTENSIONS = (".exe", ".scr", ".bat", ".cmd", ".msi", ".lnk", ".vbs", ".zip", ".rar", ".7z")
# Feature-length floors (MB) used only when runtime is unknown; a real encode is never this small.
FLAT_FLOORS = {"2160p": 4000, "1080p": 700, "720p": 300}
# MB/min floors used once runtime is known, so short episodes aren't judged by movie sizes.
RATE_FLOORS = {"2160p": 15, "1080p": 5, "720p": 3}


def classify(title: str, size_bytes: int | None = None, runtime_min: int | None = None) -> dict:
    """tier (int, higher better), label (e.g. "WEB-DL 1080p"), resolution, is_cam (filmed-in-theatre
    or other pre-release recording), warnings (reasons to distrust this specific file)."""
    if not title:
        return {"tier": TIERS["unknown"], "label": "unknown", "resolution": "", "is_cam": False, "warnings": []}
    p = parse(title)
    quality = p.quality or "unknown"
    resolution = p.resolution if p.resolution and p.resolution != "unknown" else ""
    label = f"{quality} {resolution}".strip() if quality != "unknown" else (resolution or "unknown")
    is_cam = quality in CAM_QUALITIES or bool(re.search(r"\bpre\b", title, re.IGNORECASE))

    warnings = []
    low = title.lower().rstrip()
    ext = next((e for e in BAD_EXTENSIONS if low.endswith(e)), None)
    if ext:
        warnings.append(f"filename ends in {ext} — not a video/book file")
    if "password" in low:
        warnings.append("title mentions a password — likely a scam page, not the file")
    if "codec" in low:
        warnings.append("title mentions a required codec — a common fake-installer lure")

    if size_bytes and resolution:
        mb = size_bytes / (1024 * 1024)
        if runtime_min:
            floor = RATE_FLOORS.get(resolution)
            if floor and mb / runtime_min < floor:
                warnings.append(f"{mb:.0f} MB for {runtime_min} min is too small for real {resolution}")
        else:
            floor = FLAT_FLOORS.get(resolution)
            if floor and mb < floor:
                warnings.append(f"{mb:.0f} MB is too small for a {resolution} feature")

    return {"tier": TIERS.get(quality, TIERS["unknown"]), "label": label, "resolution": resolution,
            "is_cam": is_cam, "warnings": warnings}
