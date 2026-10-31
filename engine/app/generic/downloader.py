"""yt-dlp wrapper for every platform other than TikTok and Instagram.

TikTok keeps its dedicated tikdownloader.io path (with the shared rate-limit
governor); Instagram keeps gallery-dl. Everything else lands here, where yt-dlp
does the extraction.

Two entry points:

    probe(url)          → lightweight metadata (id / title / uploader) with no
                          download, used to resolve the creator before queueing.
    download_media(...) → the actual download, reporting live progress and
                          honouring the worker's cooperative pause/cancel.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from glob import escape as glob_escape
from pathlib import Path
from typing import Callable, Optional

from ..core.logging_config import get_logger

# Reuse the shared result/abort contract so the worker handles every engine the
# same way (these are engine-agnostic despite living under tiktok/).
from ..tiktok.downloader import DownloadCancelled, DownloadPaused, DownloadResult

log = get_logger("generic.downloader")

ProgressCb = Optional[Callable[[float], None]]

_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".opus"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# yt-dlp progress lines look like: "[download]   6.4% of ~12.34MiB at 1.00MiB/s"
_PCT_RE = re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%")

_PROBE_TIMEOUT = 60
_DOWNLOAD_TIMEOUT = 1800


@dataclass
class MediaInfo:
    video_id: str
    title: str
    uploader: str
    extractor: str
    thumbnail: str = ""
    is_image: bool = False


def _base_cmd() -> list[str]:
    return [sys.executable, "-m", "yt_dlp"]


def _env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _sanitize(value: str, fallback: str = "") -> str:
    value = (value or "").strip()
    return value or fallback


def probe(url: str) -> Optional[MediaInfo]:
    """Fetch metadata for `url` without downloading. None if unsupported."""
    cmd = _base_cmd() + ["-J", "--no-playlist", "--no-warnings", "--quiet", url]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=_env(), timeout=_PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log.warning("probe timed out: %s", url)
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        log.info("probe failed for %s: %s", url, (proc.stderr or "")[-200:])
        return None

    try:
        data = json.loads(proc.stdout)
    except Exception as exc:
        log.warning("probe returned unparseable JSON for %s: %s", url, exc)
        return None

    # A playlist/profile URL slipped through --no-playlist: take the first entry.
    if data.get("_type") == "playlist":
        entries = data.get("entries") or []
        if not entries:
            return None
        data = entries[0]

    uploader = _sanitize(
        data.get("uploader") or data.get("channel") or data.get("uploader_id") or data.get("extractor_key"),
        "unknown",
    )
    return MediaInfo(
        video_id=_sanitize(str(data.get("id") or ""), ""),
        title=_sanitize(data.get("title"), ""),
        uploader=uploader,
        extractor=_sanitize(data.get("extractor_key") or data.get("extractor"), "generic").lower(),
        thumbnail=_sanitize(data.get("thumbnail")),
        is_image=(data.get("ext") or "").lower() in {"jpg", "jpeg", "png", "webp"},
    )


def _find_output(out_dir: Path, stem: str) -> Optional[Path]:
    """The file yt-dlp actually produced for `stem` (extension unknown)."""
    candidates = [
        p for p in out_dir.glob(f"{glob_escape(str(stem))}.*")
        if p.is_file() and p.suffix.lower() in (_VIDEO_EXTS | _IMAGE_EXTS) and p.stat().st_size > 10_000
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


_INVALID_FS = set('<>:"/\\|?*')


def _safe_stem(title: str) -> str:
    """A filesystem-safe filename stem from a media title."""
    cleaned = "".join(c for c in (title or "") if c not in _INVALID_FS and ord(c) >= 32)
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned[:120].strip(" .")


def download_media(
    url: str, out_dir: Path, video_id: str, progress_cb: ProgressCb = None, title: str = ""
) -> DownloadResult:
    """Download `url` into `out_dir` via yt-dlp, named after the media's TITLE
    (falling back to the id when the title is empty/unsafe).

    Streams yt-dlp's progress so the queue shows a live bar. `progress_cb` may
    raise DownloadPaused / DownloadCancelled to abort - the child process is
    killed and any partial file removed. If the file already exists, yt-dlp
    skips the transfer and this returns success from the existing file.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(title) or video_id
    template = str(out_dir / f"{stem}.%(ext)s")

    cmd = _base_cmd() + [
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", template,
        "--no-playlist", "--no-warnings",
        "--newline",            # one progress line per update (parseable)
        "--progress",
        url,
    ]

    proc = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=_env(),
        )
        tail: list[str] = []
        for line in proc.stdout or []:
            line = line.rstrip()
            if line:
                tail.append(line)
                del tail[:-8]  # keep only the last few lines for error reporting
            m = _PCT_RE.search(line)
            if m and progress_cb:
                try:
                    progress_cb(float(m.group(1)))
                except (DownloadPaused, DownloadCancelled):
                    _kill(proc)
                    _cleanup(out_dir, stem)
                    raise
        proc.wait(timeout=_DOWNLOAD_TIMEOUT)
    except (DownloadPaused, DownloadCancelled):
        raise
    except subprocess.TimeoutExpired:
        _kill(proc)
        _cleanup(out_dir, stem)
        return DownloadResult(False, strategy="yt-dlp", error="Download timed out")
    except Exception as exc:
        _kill(proc)
        log.error("yt-dlp error for %s: %s", url, exc)
        return DownloadResult(False, strategy="yt-dlp", error=str(exc))

    if proc.returncode != 0:
        _cleanup(out_dir, stem)
        return DownloadResult(False, strategy="yt-dlp", error="yt-dlp could not download that link")

    final = _find_output(out_dir, stem)
    if not final:
        return DownloadResult(False, strategy="yt-dlp", error="yt-dlp produced no media file")

    if progress_cb:
        progress_cb(100.0)
    return DownloadResult(
        True, final, "yt-dlp",
        is_image=final.suffix.lower() in _IMAGE_EXTS,
        size=final.stat().st_size,
    )


def _kill(proc) -> None:
    if proc is None:
        return
    try:
        proc.kill()
    except Exception:
        pass


def _cleanup(out_dir: Path, stem: str) -> None:
    """Drop partial/temp artefacts so a retry starts clean."""
    for p in out_dir.glob(f"{glob_escape(stem)}.*"):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
