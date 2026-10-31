"""Where a finished download actually lives.

With MEGA storage on, the library folder keeps almost nothing: a post is
downloaded to the account folder as always, uploaded to MEGA, and then moved out
of the library into a capped cache. What stays behind is a poster image of a few
dozen KB, so the grid still draws every post instantly and offline - browsing a
thousand downloads never touches the network.

Anything that needs the real bytes back (playing a video, saving it, sending it
to Telegram) calls `materialize`, which serves it from that cache or fetches it
from MEGA. The cache is disposable: deleting it costs a re-download, never data.

The order in `offload` matters. The upload has to succeed before the local file
is touched, or a crash leaves a post recorded as downloaded with no copy
anywhere - so an upload that fails leaves everything exactly where it was.

When `mega_keep_local_copies` is false, successful uploads leave neither media
nor posters behind. Viewing uses separate temporary folders, removed after
HTTP responses or Telegram sends; system-player files last only for the session.
"""

from __future__ import annotations

import os
import hashlib
import shutil
import tempfile
import threading
from pathlib import Path

from sqlalchemy.orm import Session

from .. import models, repositories as repo
from ..core.logging_config import get_logger
from ..core import media_convert
from . import mega_service

log = get_logger("storage")

# A poster sits next to the media it depicts, named `<video id>.poster.jpg`.
#
# The `.poster` marker is load-bearing, not decoration. The disk scan behind
# catch-up counts any id-named image as a downloaded post, so a plain
# `<video id>.jpg` would look exactly like one - and a post whose media had gone
# to MEGA would then be considered present even after a database reset that lost
# the row pointing at it, quietly never coming back. With the marker its stem is
# no longer a bare id, so the scanners skip it and the database stays the single
# authority on what is stored remotely.
POSTER_SUFFIX = ".poster.jpg"

# How much fetched-back media to keep on disk. Generous enough that re-watching
# what you just downloaded is instant, small enough to stay a cache.
CACHE_BUDGET_BYTES = 2 * 1024**3

_cache_lock = threading.Lock()
KEEP_LOCAL_COPIES_KEY = "mega_keep_local_copies"


def keep_local_copies(db: Session) -> bool:
    """Whether cloud storage may retain media and previews on this machine."""
    return bool(repo.Settings.get(db, KEEP_LOCAL_COPIES_KEY, True))


def temporary_dir() -> Path:
    return cache_dir() / "temporary"


def release_temporary_media(path: Path) -> None:
    """Remove only a uniquely allocated viewing folder, never the library."""
    root = temporary_dir().resolve()
    try:
        relative = Path(path).resolve().relative_to(root)
    except ValueError:
        return
    if not relative.parts or not relative.parts[0].startswith("view-"):
        return
    slot = root / relative.parts[0]
    if slot.is_symlink() or slot.resolve().parent != root:
        return
    try:
        shutil.rmtree(slot)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.debug("temporary media still in use: %s", exc)


def clear_temporary_media() -> None:
    """Remove viewing files left by a previous session or a system player."""
    root = temporary_dir()
    if root.is_dir():
        for slot in root.iterdir():
            if slot.is_dir():
                release_temporary_media(slot)


def cache_dir() -> Path:
    """Disposable local copies of files that live in MEGA.

    Under LOCALAPPDATA rather than APPDATA: it is regenerable, so it should not
    follow the user onto another machine.
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    path = Path(base) / "Vydlora" / "cache" / "mega"
    # Remote paths can be identical in different MEGA accounts. Never return
    # a cached file downloaded by another profile for that same remote path.
    from ..core.config import settings as cfg
    profile = Path(os.environ.get("LORA1_DATA_DIR") or cfg.cookies_path.parent).resolve()
    path /= hashlib.sha256(str(profile).casefold().encode("utf-8")).hexdigest()
    path.mkdir(parents=True, exist_ok=True)
    return path


def poster_for(media: Path) -> Path:
    """The poster image belonging to a downloaded post.

    Built from the stem, so a video (`7412.mp4`) and a photo post (the folder
    `7412`) resolve to the same name - and, importantly, keep resolving to it
    after the media itself is gone, which is when the lookup actually runs.
    """
    return media.with_name(media.stem + POSTER_SUFFIX)


# ── posters ──
def _save_cover(url: str, dest: Path) -> bool:
    """Download a post's cover image. Best effort: a missing poster costs a
    prettier grid tile, never a download."""
    if not url:
        return False
    try:
        from curl_cffi import requests as cffi_requests

        resp = cffi_requests.get(url, impersonate="chrome136", timeout=15)
        if resp.status_code != 200 or not resp.content:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return True
    except Exception as exc:
        log.debug("cover fetch failed for %s: %s", dest.name, exc)
        return False


def ensure_poster(db: Session, media: Path, remote: str, post_url: str = "") -> Path | None:
    """Leave a picture of this post on disk before its media goes away.

    TikTok's own cover is tried first: it is the frame the creator chose and it
    arrives at full size. A local video frame or MEGA's generated preview can
    supply the image when a public cover is unavailable.
    """
    dest = poster_for(media)
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    if post_url:
        try:
            from ..tiktok import oembed

            cover = oembed.lookup(post_url).thumbnail
            if _save_cover(cover, dest):
                return dest
        except Exception as exc:
            log.debug("oembed cover lookup failed for %s: %s", media.name, exc)

    # Local videos also supply a preview when a social cover is unavailable.
    # Generate it before offloading so browsing never has to fetch the video.
    if media.is_file() and media_convert.video_poster(media, dest):
        return dest

    if remote and mega_service.thumbnail(db, remote, dest):
        return dest

    # A photo post already contains its own picture; copy the first image out
    # so the tile survives the folder being removed.
    if media.is_dir():
        images = sorted(p for p in media.iterdir()
                        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
        if images:
            try:
                dest.write_bytes(images[0].read_bytes())
                return dest
            except OSError as exc:
                log.debug("could not copy photo poster for %s: %s", media.name, exc)
    return None


# ── moving a finished download into MEGA ──
def _remove(path: Path) -> None:
    if path.is_dir():
        for child in path.iterdir():
            try:
                child.unlink()
            except OSError:
                pass
        try:
            path.rmdir()
        except OSError as exc:
            log.debug("could not remove %s: %s", path.name, exc)
        return
    try:
        path.unlink()
    except OSError as exc:
        log.debug("could not remove %s: %s", path.name, exc)


def _retire(media: Path, slot: Path | None) -> None:
    """Take the media out of the library folder now that MEGA holds it.

    It moves into the cache rather than being deleted outright. The file that
    just finished downloading is by far the most likely one to be wanted next -
    the bot sends it to the chat that asked, and the person who pressed download
    tends to press play - and re-fetching it from MEGA seconds after uploading
    it would spend the bandwidth twice for no gain. The cache is capped and
    trimmed oldest-first, so this is a grace period, not a second copy of the
    library: what is not touched again ages out on its own.
    """
    if slot is None:
        _remove(media)
        return
    try:
        slot.mkdir(parents=True, exist_ok=True)
        destination = slot / media.name
        if destination.exists():
            _remove(destination)
        media.replace(destination)
    except OSError as exc:
        # A cross-device move or a locked file: deleting is still correct, the
        # copy in MEGA is the one that matters.
        log.debug("could not move %s into the cache: %s", media.name, exc)
        _remove(media)


def offload(db: Session, media: Path, target_dir: Path, post_url: str = "",
            cache_slot: Path | None = None) -> str | None:
    """Upload a finished download to MEGA and clear it out of the library folder.

    Returns the remote path, or None if the file stays on this machine (MEGA is
    off, unavailable, or the upload failed - in which case nothing is removed).
    """
    if not mega_service.is_enabled(db) or not media.exists():
        return None
    remote_dir = mega_service.remote_dir_for(db, target_dir)
    try:
        remote = mega_service.upload(db, media, remote_dir)
    except mega_service.MegaError as exc:
        # Keeping the file is always the safe failure: the library still works,
        # it just has not moved yet.
        log.warning("MEGA upload failed for %s, keeping it locally: %s", media.name, exc)
        return None

    # Before the media goes anywhere: the poster is what the library grid draws
    # from, and for a photo post it is copied out of the folder itself.
    if keep_local_copies(db):
        ensure_poster(db, media, remote, post_url)
        _retire(media, cache_slot)
        trim_cache()
    else:
        _retire(media, None)
        poster = poster_for(media)
        if poster.exists():
            _remove(poster)
    log.info("stored %s in MEGA at %s", media.name, remote)
    return remote


def store_download(db: Session, download, media: Path, post_url: str = "") -> str | None:
    """Move a recorded download's media into MEGA and stamp the row with it.

    The single entry point every path that saves media calls, so a new one
    cannot quietly skip cloud storage. It used to live inline in the queue
    worker, which meant only queued video downloads were ever uploaded: stories,
    Instagram posts, imports, reconciled files and posts already on disk all
    stayed on the machine even with MEGA storage on.

    Returns the remote path, or None when the file stays local (MEGA off,
    unavailable, or the upload failed - in which case nothing is removed).
    """
    if download is None or not media:
        return None
    try:
        remote = offload(db, Path(media), Path(media).parent, post_url=post_url,
                         cache_slot=cache_slot(download.id) if keep_local_copies(db) else None)
        if remote:
            download.remote_path = remote
            db.commit()
        return remote
    except Exception as exc:
        # Cloud storage is never allowed to fail the download that produced the
        # file: the media is on disk and recorded either way.
        log.warning("could not store %s in MEGA: %s", Path(media).name, exc)
        return None


# ── getting the bytes back ──
def cache_slot(download_id: int) -> Path:
    """A per-download cache folder, so two posts that share a filename cannot
    overwrite each other."""
    return cache_dir() / str(download_id)


def materialize(db: Session, download: models.Download) -> Path | None:
    """A real local path for this download's media, fetching it if it lives in
    MEGA. Returns None when the media cannot be produced."""
    local = Path(download.file_path) if download.file_path else None
    if local and local.exists():
        return local

    remote = getattr(download, "remote_path", None)
    if not remote:
        return None

    if not keep_local_copies(db):
        # Each response owns its temporary copy: overlapping range requests and
        # photo viewers must not delete a file another response is still using.
        root = temporary_dir()
        root.mkdir(parents=True, exist_ok=True)
        slot = Path(tempfile.mkdtemp(prefix="view-", dir=root))
        try:
            got = mega_service.fetch(db, remote, slot)
        except Exception:
            release_temporary_media(slot)
            raise
        if not got:
            release_temporary_media(slot)
        return got

    slot = cache_slot(download.id)
    name = remote.rstrip("/").rsplit("/", 1)[-1]
    cached = slot / name
    if cached.exists():
        # Touch it so the trim below treats a re-watch as a recent use.
        try:
            os.utime(cached, None)
        except OSError:
            pass
        return cached

    got = mega_service.fetch(db, remote, slot)
    if got:
        trim_cache()
    return got


def cached_images(db: Session, download: models.Download) -> list[Path]:
    """Image files for a photo post, fetching the folder back if needed."""
    got = materialize(db, download)
    if not got:
        return []
    if got.is_dir():
        return sorted(p for p in got.iterdir()
                      if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    return [got]


def trim_cache(budget: int = CACHE_BUDGET_BYTES) -> int:
    """Drop the least recently used cached files until the cache fits. Returns
    the number of bytes freed."""
    root = cache_dir()
    with _cache_lock:
        entries: list[tuple[float, int, Path]] = []
        total = 0
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size
        if total <= budget:
            return 0

        freed = 0
        for _, size, path in sorted(entries):
            try:
                path.unlink()
            except OSError:
                continue
            freed += size
            if total - freed <= budget:
                break

        # Clear out the per-download folders left empty behind them.
        for folder in root.iterdir():
            if folder.is_dir():
                try:
                    folder.rmdir()
                except OSError:
                    pass
    log.info("trimmed %.1f MB from the MEGA cache", freed / 1024**2)
    return freed


def clear_cache() -> int:
    """Empty the cache entirely. Returns bytes freed."""
    return trim_cache(budget=0)
