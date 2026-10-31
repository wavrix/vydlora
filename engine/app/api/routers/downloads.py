"""Downloads endpoints - listing, thumbnails, in-app media viewing, file download."""

from __future__ import annotations

from pathlib import Path
from datetime import date, datetime, time, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models, platforms, repositories as repo
from ...core.config import settings as cfg
from ...core.database import session_scope
from ...core.security import safe_join
from ...core import media_convert
from ...models.enums import PostType
from ...schemas import Ack, DownloadOut, Page
from ...services import (
    mega_service,
    settings_service,
    storage_service,
    telegram_service,
    user_service,
)
from ...tiktok import oembed, urls
from ..deps import PageParams, get_db, page_params, paginate_meta

router = APIRouter(prefix="/api/downloads", tags=["downloads"])

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
VIDEO_POSTER_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _to_out(db: Session, d: models.Download) -> DownloadOut:
    return _to_out_many(db, [d])[0]


def _to_out_many(db: Session, items) -> list[DownloadOut]:
    """Serialise downloads with creator names via one batched user lookup
    instead of one query per row."""
    from sqlalchemy import select

    ids = {d.user_id for d in items if d.user_id}
    users = {}
    if ids:
        users = {
            u.id: u
            for u in db.scalars(select(models.User).where(models.User.id.in_(ids))).all()
        }
    out: list[DownloadOut] = []
    for d in items:
        o = DownloadOut.model_validate(d)
        user = users.get(d.user_id)
        if user:
            o.username = user.username
            o.nickname = user.nickname
        # Computed here rather than derived by the client: the poster naming
        # rule belongs to one place, and the app should not have to reimplement
        # it to draw a tile.
        poster = _video_poster(d)
        if poster:
            o.poster_path = str(poster)
        out.append(o)
    return out


# ── path helpers ──
def _download_roots() -> list[Path]:
    """Private staging plus legacy read-only locations while files migrate."""
    from ...core.compat import LEGACY_DOWNLOAD_FOLDER
    roots = {settings_service.managed_downloads_path(), Path.home() / "Downloads" / "Vydlora", Path.home() / "Downloads" / LEGACY_DOWNLOAD_FOLDER}
    try:
        with session_scope() as db:
            roots.add(settings_service.downloads_path(db))
            legacy = repo.Settings.get(db, settings_service.DOWNLOADS_DIR_KEY, "")
            if legacy:
                roots.add(Path(legacy).expanduser().resolve())
    except Exception:
        pass
    # Media stored in MEGA is served from the local cache it is fetched into,
    # which is outside the downloads tree and would otherwise fail validation.
    try:
        roots.add(storage_service.cache_dir().resolve())
    except Exception:
        pass
    return list(roots)


def _safe(path: Path) -> Path:
    """Validate a stored path stays within an allowed downloads root."""
    resolved = path.resolve()
    for root in _download_roots():
        try:
            rel = resolved.relative_to(root)
            return safe_join(root, *rel.parts)
        except Exception:
            continue
    raise HTTPException(403, "Invalid path")


def _get(db: Session, download_id: int) -> models.Download:
    d = db.get(models.Download, download_id)
    if not d:
        raise HTTPException(404, "Download not found")
    return d


def _image_files(d: models.Download, db: Session | None = None) -> list[Path]:
    """Sorted image files for a photo post (file_path is the image directory).

    Local only. A photo post stored in MEGA has an empty folder here, so callers
    that need the pictures themselves go through `_fetched_images`; the ones
    that only want to know how many there are stay off the network.
    """
    if not d.file_path:
        return []
    base = Path(d.file_path)
    if base.is_dir():
        return sorted(p for p in base.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
    if base.is_file() and base.suffix.lower() in IMAGE_EXTS:
        return [base]
    return []


def _fetched_images(db: Session, d: models.Download) -> list[Path]:
    """Image files for a photo post, pulling them back from MEGA if that is
    where they live."""
    local = _image_files(d)
    return local or storage_service.cached_images(db, d)


def _media_file(db: Session, d: models.Download) -> Path:
    """The real media for this download, fetched out of MEGA when needed."""
    path = storage_service.materialize(db, d)
    if not path:
        raise HTTPException(404, "File missing on disk")
    if path.is_dir():
        images = _fetched_images(db, d)
        if not images:
            raise HTTPException(404, "File missing on disk")
        return images[0]
    return path


def _video_poster(d: models.Download) -> Path | None:
    """A pre-generated poster image sitting next to the media, if one exists.

    This is what keeps the library grid instant once media moves to MEGA: the
    poster is the one thing left behind locally, so a thousand tiles draw
    without a single network call.
    """
    if not d.file_path:
        return None
    media = Path(d.file_path)
    candidates = [storage_service.poster_for(media)]
    candidates += [media.with_suffix(ext) for ext in VIDEO_POSTER_EXTS]
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size:
            return candidate
    return None


def _serve(path: Path, *, inline: bool = True, download_name: str | None = None) -> FileResponse:
    safe = _safe(path)
    if not safe.exists() or not safe.is_file():
        raise HTTPException(404, "File missing on disk")
    return FileResponse(
        str(safe),
        filename=download_name,
        content_disposition_type="attachment" if download_name else "inline",
        background=BackgroundTask(storage_service.release_temporary_media, safe),
    )


# ── listing ──
@router.get("", response_model=Page[DownloadOut])
def list_downloads(
    params: PageParams = Depends(page_params),
    search: str | None = None,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    post_type: str | None = None,
    source: str | None = None,
    source_group: Literal["manual", "monitor", "story", "telegram"] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "The start date must be on or before the end date.")
    # Calendar filters include the whole final day, including subsecond timestamps.
    created_from = datetime.combine(date_from, time.min) if date_from else None
    created_before = datetime.combine(date_to, time.min) + timedelta(days=1) if date_to and date_to < date.max else None
    items, total = repo.Downloads.list_paginated(
        db, params.page, params.page_size, search,
        sort_by=sort_by, sort_dir=sort_dir, post_type=post_type,
        source=source, created_from=created_from, created_before=created_before, source_group=source_group,
    )
    return {"items": _to_out_many(db, items), **paginate_meta(total, params.page, params.page_size)}


@router.get("/{download_id}", response_model=DownloadOut)
def get_download(download_id: int, db: Session = Depends(get_db)):
    return _to_out(db, _get(db, download_id))


def _stored_files(d: models.Download) -> list[Path]:
    """Everything on disk belonging to this download: the media itself plus any
    sidecar poster, or every image for a photo post."""
    if not d.file_path:
        return []
    images = _image_files(d)
    if images:
        return images
    files = [Path(d.file_path)]
    poster = _video_poster(d)
    if poster:
        files.append(poster)
    return files


def _erase(db: Session, d: models.Download, delete_file: bool) -> tuple[int, list[str]]:
    """Drop one library row and, optionally, the media behind it.

    Returns (files removed, problems). Split out so deleting one and deleting a
    selection share the same rules - notably never unlinking anything outside the
    downloads roots.
    """
    removed = 0
    problems: list[str] = []

    if delete_file:
        for path in _stored_files(d):
            try:
                safe = _safe(path)
            except HTTPException:
                # Outside the allowed download roots - never unlink it.
                problems.append(f"skipped {path.name} (outside downloads folder)")
                continue
            try:
                if safe.exists():
                    safe.unlink()
                    removed += 1
            except OSError as exc:
                problems.append(f"{safe.name}: {exc}")

        # Photo posts own their folder; drop it once the images are gone.
        base = Path(d.file_path) if d.file_path else None
        if base and base.is_dir():
            try:
                base.rmdir()
            except OSError:
                pass

        # The copy in MEGA is the real one once media is stored there, so
        # "delete the file" has to reach it or the post is only half removed.
        if d.remote_path:
            try:
                if not mega_service.remove(db, d.remote_path):
                    problems.append(f"{d.video_id}: could not delete it from MEGA")
                else:
                    removed += 1
            except Exception as exc:
                problems.append(f"{d.video_id} (MEGA): {exc}")

        # Its cached copy, if it was played or sent recently.
        slot = storage_service.cache_dir() / str(d.id)
        if slot.is_dir():
            for child in slot.iterdir():
                try:
                    child.unlink()
                except OSError:
                    pass
            try:
                slot.rmdir()
            except OSError:
                pass

    db.delete(d)
    return removed, problems


@router.delete("/{download_id}", response_model=Ack)
def delete_download(download_id: int, delete_file: bool = True, db: Session = Depends(get_db)):
    """Remove a download from the library, optionally clearing its files too.

    The matching Post keeps `downloaded=True` on purpose: deleting is a
    deliberate act, and resetting the flag would have the next scan quietly
    fetch the post all over again.
    """
    d = _get(db, download_id)
    label = d.title or d.video_id
    removed, problems = _erase(db, d, delete_file)
    db.commit()

    detail = f"Deleted “{label}”"
    if delete_file:
        detail += f" and {removed} file(s)"
    if problems:
        detail += ": " + "; ".join(problems)
    return Ack(ok=True, detail=detail)


class BulkDeleteRequest(BaseModel):
    ids: list[int] = Field(..., min_length=1)
    delete_file: bool = True


@router.post("/bulk-delete", response_model=Ack)
def bulk_delete(payload: BulkDeleteRequest, db: Session = Depends(get_db)):
    """Delete a selection in one call.

    One request rather than one per item: a hundred selected downloads would
    otherwise be a hundred round-trips, each committing separately, with a
    half-applied result if any of them failed partway.

    Ids that no longer exist are skipped rather than failing the batch - by the
    time a selection is acted on, something else may already have removed one.
    """
    rows = db.scalars(
        select(models.Download).where(models.Download.id.in_(payload.ids))
    ).all()

    removed_files = 0
    problems: list[str] = []
    for d in rows:
        count, issues = _erase(db, d, payload.delete_file)
        removed_files += count
        problems.extend(issues)
    db.commit()

    detail = f"Deleted {len(rows)} item{'' if len(rows) == 1 else 's'}"
    if payload.delete_file:
        detail += f" and {removed_files} file(s)"
    missing = len(payload.ids) - len(rows)
    if missing:
        detail += f" ({missing} already gone)"
    if problems:
        detail += ": " + "; ".join(problems[:3])
    return Ack(ok=True, detail=detail)


# ── import history from a previous Lora 1 install ──
class MetadataImportRequest(BaseModel):
    directory: str = Field(..., min_length=1)
    # Import creators onto the monitoring roster too. Off by default: the point
    # is history + dedupe, and quietly adding dozens of accounts to the scanner
    # would be a surprising side effect.
    monitored: bool = False


class MetadataImportResult(BaseModel):
    imported: int
    creators: int
    skipped_existing: int
    files_located: int
    problems: list[str] = []


@router.post("/import-metadata", response_model=MetadataImportResult)
def import_metadata(payload: MetadataImportRequest, db: Session = Depends(get_db)):
    """Import Lora 1 `metadata.json` exports so previously-downloaded posts are
    recognised and never fetched again. Idempotent."""
    from ...services import import_service

    try:
        result = import_service.import_metadata_dir(db, payload.directory, monitored=payload.monitored)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return MetadataImportResult(**result)


# ── ad-hoc download from any pasted media URL ──
class UrlDownloadRequest(BaseModel):
    url: str = Field(..., min_length=8)


class UrlDownloadResult(BaseModel):
    warning: str = ""
    platform: str
    queued: bool = False
    job_id: int | None = None
    username: str = ""
    video_id: str = ""
    completed: int = 0
    detail: str = ""
    # Media metadata resolved at enqueue time (yt-dlp probe / TikTok oEmbed), so
    # the UI can show the real title + a preview immediately. Best-effort.
    title: str = ""
    thumbnail: str = ""


def _generic_user(db: Session, platform: str, username: str) -> models.User:
    """Get/create an on-demand creator for a non-TikTok platform. Never monitored
    (the scanner is TikTok-only), mirroring how Instagram creators work."""
    from ...models.enums import UserStatus
    from ...tiktok import naming

    platform = (platform or platforms.GENERIC).lower()[:16]
    username = (username or "unknown")[:128]
    key = platforms.state_key(platform, username)[:64]

    user = db.scalar(select(models.User).where(models.User.state_key == key))
    if user:
        return user
    user = models.User(
        state_key=key,
        platform=platform,
        username=username,
        nickname=username,
        status=UserStatus.active,
        monitored=False,
        folder_name=naming.account_folder_name(username, username),
    )
    db.add(user)
    db.flush()
    return user


@router.post("/url", response_model=UrlDownloadResult)
def download_url(payload: UrlDownloadRequest, db: Session = Depends(get_db)):
    """Download any supported media URL.

    Routed by platform: TikTok keeps the tikdownloader.io path, Instagram uses
    gallery-dl (it handles image posts/carousels yt-dlp refuses), and every other
    site goes through yt-dlp. TikTok/generic links are queued so the worker pool
    downloads them with live progress; Instagram runs inline because one URL can
    yield a whole carousel.
    """
    url = payload.url.strip()
    if not platforms.looks_like_url(url):
        raise HTTPException(400, "That doesn't look like a link.")

    platform = platforms.detect(url)
    if platform == platforms.TIKTOK:
        return _queue_tiktok(db, url)
    if platform == platforms.INSTAGRAM:
        return _download_instagram(db, url)
    if platform == platforms.X:
        from ...services import x_service
        result = x_service.download_url(db, url, source="manual")
        if not result.get("ok"):
            raise HTTPException(400, result.get("error") or "X download failed.")
        warning = result.get("warning") or ("Some X posts could not be saved. Try Download again." if result.get("errors") else "")
        return UrlDownloadResult(platform=platforms.X, completed=result["count"], warning=warning,
                                 username=", ".join(result.get("creators") or []),
                                 detail=f"Saved {result['count']} item(s) from X." )
    return _queue_generic(db, url)


def _queue_tiktok(db: Session, url: str) -> UrlDownloadResult:
    handle, video_id, post = urls.identify(url)
    user, post = user_service.adhoc_for_link(db, url, handle, source="manual", post=post)
    if not user:
        raise HTTPException(400, "Couldn't identify the creator from that TikTok URL.")

    # Re-submitting a post that is still downloading joins the job already
    # running instead of starting a competing one.
    job = repo.Queue.active_for_video(db, user.id, video_id)
    if job is None:
        # Queue and commit first so a worker can start on it right away. For a
        # creator we already know, `post` is empty and the lookup below is the
        # only thing standing between here and the response - holding the
        # enqueue behind it would delay the download for nothing.
        job = repo.Queue.enqueue(
            db, user_id=user.id, username=user.username, video_id=video_id,
            url=url, title=post.title, source="manual",
        )
        db.commit()
    job_id = job.id

    title, thumbnail = post.title, post.thumbnail
    if not title:
        # Known creator: nothing has been looked up yet. The worker resolves the
        # title itself if this hasn't landed by the time it writes the library
        # record, so the download is never waiting on it.
        card = oembed.lookup(url)
        title, thumbnail = card.title, card.thumbnail
        if title:
            job.title = title
            db.commit()

    return UrlDownloadResult(
        platform=platforms.TIKTOK, queued=True, job_id=job_id,
        username=user.username, video_id=video_id,
        title=title, thumbnail=thumbnail,
    )


def _download_instagram(db: Session, url: str) -> UrlDownloadResult:
    from ...services import instagram_service

    result = instagram_service.download_url(db, url, source="manual")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "Instagram download failed")
    creators = result.get("creators") or []
    return UrlDownloadResult(
        platform=platforms.INSTAGRAM,
        queued=False,
        completed=int(result.get("count") or 0),
        username=creators[0] if creators else "",
        detail=f"Downloaded {result.get('count', 0)} item(s) from Instagram.",
    )


def _queue_generic(db: Session, url: str) -> UrlDownloadResult:
    from ...generic import downloader as generic_downloader

    info = generic_downloader.probe(url)
    if info is None or not info.video_id:
        raise HTTPException(
            400, "Couldn't read that link. The site may be unsupported, private, or the URL is wrong."
        )

    user = _generic_user(db, info.extractor, info.uploader)
    job = repo.Queue.active_for_video(db, user.id, info.video_id)
    if job is None:
        job = repo.Queue.enqueue(
            db, user_id=user.id, username=user.username, video_id=info.video_id,
            url=url, title=info.title, source="manual",
        )
        db.commit()
    return UrlDownloadResult(
        platform=user.platform, queued=True, job_id=job.id,
        username=user.username, video_id=info.video_id,
        title=info.title, thumbnail=info.thumbnail,
    )


# ── media viewer payload ──
@router.get("/{download_id}/media")
def get_media(download_id: int, db: Session = Depends(get_db)):
    """Describe what the in-app viewer should render (no file re-download)."""
    d = _get(db, download_id)
    if d.post_type == PostType.image:
        imgs = _fetched_images(db, d)
        return {
            "id": d.id,
            "type": "image",
            "title": d.title,
            "video_url": None,
            "images": [f"/api/downloads/{d.id}/image/{i}" for i in range(len(imgs))],
        }
    return {
        "id": d.id,
        "type": "video",
        "title": d.title,
        "video_url": f"/api/downloads/{d.id}/file",
        "images": [],
    }


# ── thumbnail (grid preview) ──
@router.get("/{download_id}/thumbnail")
def thumbnail(download_id: int, db: Session = Depends(get_db)):
    d = _get(db, download_id)
    # The local poster answers first for every kind of post. It is the whole
    # reason scrolling the library stays instant with the media in MEGA - this
    # endpoint must never be the thing that starts a download.
    poster = _video_poster(d)
    if poster:
        return _serve(poster)
    if d.post_type == PostType.image:
        imgs = _image_files(d)
        if imgs:
            return _serve(imgs[0])
        raise HTTPException(404, "No image")
    # Local downloads may predate poster generation, or never have been
    # offloaded. Repair them lazily without fetching remote media or rewriting
    # the video; subsequent requests reuse the same small sidecar image.
    if d.file_path:
        media = _safe(Path(d.file_path))
        destination = _safe(storage_service.poster_for(media))
        poster = media_convert.video_poster(media, destination)
        if poster:
            return _serve(poster)
    raise HTTPException(404, "No poster")


# ── individual photo (gallery) ──
@router.get("/{download_id}/image/{index}")
def image(download_id: int, index: int, db: Session = Depends(get_db)):
    d = _get(db, download_id)
    imgs = _fetched_images(db, d)
    if index < 0 or index >= len(imgs):
        raise HTTPException(404, "Image not found")
    return _serve(imgs[index])


# ── primary media file (inline playback) ──
@router.get("/{download_id}/file")
def download_file(download_id: int, db: Session = Depends(get_db)):
    d = _get(db, download_id)
    if not d.file_path and not d.remote_path:
        raise HTTPException(404, "File not found")
    return _serve(_media_file(db, d))


# ── bring a stored file back to this machine ──
class LocalPath(BaseModel):
    path: str


@router.post("/{download_id}/localize", response_model=LocalPath)
def localize(download_id: int, db: Session = Depends(get_db)):
    """Guarantee this download has a real local file and say where it is.

    The desktop app opens media with the system player and reveals it in
    Explorer, both of which need a path rather than a stream. For a post stored
    in MEGA that means fetching it back first, which is what this does - already
    a no-op when the file is on disk or still cached.
    """
    d = _get(db, download_id)
    path = storage_service.materialize(db, d)
    if not path:
        raise HTTPException(404, "File missing on disk")
    return LocalPath(path=str(path))


# ── explicit download (forces save dialog) ──
@router.get("/{download_id}/download")
def force_download(download_id: int, db: Session = Depends(get_db)):
    d = _get(db, download_id)
    if not d.file_path and not d.remote_path:
        raise HTTPException(404, "File not found")
    path = _media_file(db, d)
    return _serve(path, download_name=path.name)


# ── push a download to the authorized Telegram chats ──
@router.post("/{download_id}/telegram", response_model=Ack)
def upload_to_telegram(download_id: int, db: Session = Depends(get_db)):
    _get(db, download_id)  # 404 if it doesn't exist
    try:
        sent = telegram_service.upload_download(db, download_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # pragma: no cover
        raise HTTPException(500, f"Telegram upload failed: {exc}")
    if sent == 0:
        raise HTTPException(502, "Could not deliver to any chat.")
    return Ack(ok=True, detail=f"Sent to {sent} chat(s).")
