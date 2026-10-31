"""Make downloaded media playable everywhere.

Instagram serves two formats that are valid files but open on almost nothing:

* **VP9 inside an .mp4**. Legal per the VP Codec ISO Media File Format, and
  what Instagram hands out for a large share of reels. Windows Media Player
  needs a Store extension to play it, QuickTime and iOS refuse outright, and
  most TVs and older Android devices do not decode it. The file is not corrupt;
  it just says .mp4 and behaves like something else.
* **.webp images**. Fine in a browser, patchy everywhere else, and Telegram
  treats webp as its sticker format rather than a photo.

Both are converted to the pair that plays without qualification: H.264 + AAC in
MP4, and baseline JPEG. Anything already in a good format is left completely
alone - the probe is cheap, the transcode is not, and re-encoding an H.264 file
would cost CPU and quality for no gain.

ffmpeg is not bundled with the app. When it is absent nothing is converted and
the original file is kept, because a missing converter is not a reason to lose
a download.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from .logging_config import get_logger

log = get_logger("media")

# Video codecs that need no help. Everything else is re-encoded.
GOOD_VIDEO_CODECS = {"h264", "avc1"}
# Image suffixes that are re-wrapped as JPEG.
CONVERT_IMAGE_SUFFIXES = {".webp", ".heic", ".heif", ".avif"}

_TIMEOUT = 900
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Resolved once. `False` means "looked and did not find", which is different
# from `None` ("not looked yet") - without that distinction every call would
# re-scan the filesystem on a machine that has no ffmpeg.
_ffmpeg: str | None | bool = None
_ffprobe: str | None | bool = None

# A grid can request many previews at once. Coalesce requests for the same
# file and keep decoding bounded, without retaining a lock per library item.
_poster_locks = tuple(threading.Lock() for _ in range(32))
_poster_workers = threading.BoundedSemaphore(2)


def _find(name: str) -> str | None:
    """Locate an ffmpeg tool.

    PATH first, then the place yt-dlp's winget package puts it, which is where
    it already is on a machine that installed ffmpeg for yt-dlp rather than
    system-wide.
    """
    found = shutil.which(name)
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        pkgs = Path(local) / "Microsoft" / "WinGet" / "Packages"
        try:
            for exe in pkgs.glob(f"*FFmpeg*/**/bin/{name}.exe"):
                if exe.is_file():
                    return str(exe)
        except OSError:
            pass
    for base in (r"C:\ffmpeg\bin", "/usr/bin", "/usr/local/bin"):
        candidate = Path(base) / name
        for path in (candidate, candidate.with_suffix(".exe")):
            try:
                if path.is_file():
                    return str(path)
            except OSError:
                continue
    return None


def ffmpeg_path() -> str | None:
    global _ffmpeg
    if _ffmpeg is None:
        _ffmpeg = _find("ffmpeg") or False
        log.info("ffmpeg: %s", _ffmpeg or "not found (media left as downloaded)")
    return _ffmpeg or None


def ffprobe_path() -> str | None:
    global _ffprobe
    if _ffprobe is None:
        _ffprobe = _find("ffprobe") or False
    return _ffprobe or None


def available() -> bool:
    return bool(ffmpeg_path() and ffprobe_path())


def _run(args: list[str], timeout: int = _TIMEOUT) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
    except Exception as exc:
        log.debug("ffmpeg call failed: %s", exc)
        return None


def video_codec(path: Path) -> str:
    """The video stream's codec, or '' when it cannot be read."""
    probe = ffprobe_path()
    if not probe:
        return ""
    result = _run([
        probe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path),
    ], timeout=60)
    return (result.stdout.strip().splitlines() or [""])[0].strip() if result else ""


def video_poster(media: Path, destination: Path) -> Path | None:
    """Cache a small JPEG from local video, publishing only a complete image.

    This never fetches media or changes the original. Callers validate their
    storage paths before invoking it. Existing posters survive cloud offload.
    """
    temporary = None
    try:
        lock = _poster_locks[hash(str(destination.resolve())) % len(_poster_locks)]
        with lock:
            if destination.is_file() and destination.stat().st_size:
                return destination
            executable = ffmpeg_path()
            if not executable or not media.is_file():
                return None
            with _poster_workers:
                with tempfile.NamedTemporaryFile(
                    prefix=f".{destination.stem}-", suffix=".jpg",
                    dir=destination.parent, delete=False,
                ) as pending:
                    temporary = Path(pending.name)
                result = _run([
                    executable, "-v", "error", "-y", "-threads", "1",
                    "-i", str(media), "-map", "0:v:0", "-an", "-sn", "-dn",
                    "-vf", "scale=480:480:force_original_aspect_ratio=decrease,thumbnail=30",
                    "-frames:v", "1", "-q:v", "4", "-threads", "1", str(temporary),
                ], timeout=30)
                if result is None or result.returncode or not temporary.stat().st_size:
                    log.debug("could not generate poster for %s", media.name)
                    return None
                temporary.replace(destination)
                return destination
    except OSError as exc:
        log.debug("could not save poster for %s: %s", media.name, exc)
        return None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def needs_conversion(path: Path, is_video: bool) -> str:
    """Why this file needs converting, or '' when it is already fine."""
    if not path.is_file():
        return ""
    if not is_video:
        return "image" if path.suffix.lower() in CONVERT_IMAGE_SUFFIXES else ""
    codec = video_codec(path)
    if not codec:
        return ""      # unreadable: leave it rather than guess
    return "" if codec in GOOD_VIDEO_CODECS else codec


def to_playable(path: Path, is_video: bool) -> Path:
    """Convert `path` in place when it needs it. Returns the file to use.

    The original is only removed once the replacement exists and is non-empty,
    so a failed or half-written conversion leaves the download intact rather
    than destroying it.
    """
    reason = needs_conversion(path, is_video)
    if not reason:
        return path
    if not available():
        log.warning("%s needs converting (%s) but ffmpeg was not found", path.name, reason)
        return path

    target = path.with_suffix(".mp4" if is_video else ".jpg")
    temp = path.with_name(path.stem + ".converting" + target.suffix)
    if is_video:
        args = [
            ffmpeg_path(), "-v", "error", "-y", "-i", str(path),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",          # some players reject 4:2:0 alternatives
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",      # playable before it has fully loaded
            str(temp),
        ]
    else:
        args = [ffmpeg_path(), "-v", "error", "-y", "-i", str(path),
                "-q:v", "2", str(temp)]

    result = _run(args)
    if result is None or result.returncode != 0 or not temp.is_file() or temp.stat().st_size == 0:
        log.warning("could not convert %s (%s): %s", path.name, reason,
                    (result.stderr if result else "").strip()[:160])
        temp.unlink(missing_ok=True)
        return path

    try:
        if target != path:
            path.unlink(missing_ok=True)
        temp.replace(target)
    except OSError as exc:
        log.warning("could not replace %s: %s", path.name, exc)
        temp.unlink(missing_ok=True)
        return path

    log.info("converted %s (%s) -> %s", path.name, reason, target.name)
    return target
