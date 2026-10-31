"""Download service - execute a queue job end to end."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, platforms, repositories as repo
from ..core.database import session_scope
from ..core.events import publish
from ..core.logging_config import get_logger
from ..models.enums import (
    ActivityType,
    DownloadStatus,
    JobStatus,
    LogLevel,
    LogSource,
    NotificationType,
    PostType,
    UserStatus,
)
from ..tiktok import downloader, naming, oembed, urls
from . import cookie_service, events_service, settings_service, storage_service

log = get_logger("download")

# How many times a transient failure is retried before the job is given up on.
# Three is enough to ride out a WAF challenge or a slow response without a
# genuinely broken post occupying a worker all afternoon.
MAX_DOWNLOAD_ATTEMPTS = 3


# ── Rate-limit feedback ──
# The downloader governor calls this (from a worker thread) when tikdownloader.io
# returns a 429. Debounced so a burst of 429s surfaces a single notification.
_rl_lock = threading.Lock()
_rl_last_notify = 0.0


def _on_rate_limited(cooldown: float) -> None:
    global _rl_last_notify
    with _rl_lock:
        now = time.monotonic()
        if now - _rl_last_notify < 90:
            return
        _rl_last_notify = now
    try:
        with session_scope() as db:
            events_service.emit(
                db,
                ActivityType.rate_limit,
                f"tikdownloader.io rate limit hit, slowing requests (~{cooldown:.0f}s) and retrying",
                log_level=LogLevel.warning,
                log_source=LogSource.download,
                notify=(
                    NotificationType.warning,
                    "Rate limited by tikdownloader.io",
                    "Requests are being throttled. Downloads will automatically slow down and retry. No action needed.",
                ),
            )
    except Exception as exc:  # never let feedback break a download
        log.warning("rate-limit notify failed: %s", exc)


downloader.set_rate_limit_hook(_on_rate_limited)


def account_dir(db: Session, user: models.User | None, username: str, nickname: str = "") -> Path:
    folder = (user.folder_name if user and user.folder_name else None) or naming.account_folder_name(
        nickname or username, username
    )
    # Private staging; platform and creator folders are visible only in MEGA.
    base = settings_service.downloads_path(db)
    # Every platform gets its own folder (<downloads>/TikTok/<creator>/), so a
    # handle can't collide across sites and the library stays browsable.
    platform = getattr(user, "platform", None) or platforms.TIKTOK
    path = base / platforms.display_name(platform) / folder
    path.mkdir(parents=True, exist_ok=True)
    return path


# Media a post can be saved as on disk: a `{video_id}.mp4` file (video), or a
# `{video_id}/` folder of images (photo post). Extensions we treat as media when
# a file's stem is a TikTok video id.
_MEDIA_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".jpg", ".jpeg", ".png", ".webp"}


def media_on_disk(dir_path: Path, video_id: str) -> Path | None:
    """Return the on-disk media for a post if present (the `{id}.mp4` file or the
    `{id}/` image folder), else None. Used to avoid re-downloading a post whose
    file already exists - including files saved outside the app."""
    mp4 = dir_path / f"{video_id}.mp4"
    if mp4.is_file():
        return mp4
    folder = dir_path / str(video_id)
    if folder.is_dir() and any(folder.iterdir()):
        return folder
    return None


def downloaded_ids_on_disk(dir_path: Path) -> set[str]:
    """Every TikTok video id already saved in an account folder - id-named `.mp4`
    files and id-named image folders. Ignores `metadata.json`, the `Stories/`
    subfolder and anything not named by a numeric video id."""
    ids: set[str] = set()
    if not dir_path.exists():
        return ids
    for entry in dir_path.iterdir():
        name = entry.name
        if entry.is_dir():
            if name.isdigit():
                ids.add(name)  # image/photo post folder
            continue
        if entry.stem.isdigit() and entry.suffix.lower() in _MEDIA_EXTS:
            ids.add(entry.stem)
    return ids


# Serialises read-modify-write of per-account metadata.json: two workers
# downloading for the same account raced each other and could lose entries or
# leave a half-written file. Writes go to a temp file then atomically replace.
_meta_lock = threading.Lock()


def _write_metadata(dir_path: Path, entry: dict) -> None:
    meta_file = dir_path / "metadata.json"
    with _meta_lock:
        data = {}
        if meta_file.exists():
            try:
                data = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[entry["video_id"]] = entry
        tmp = meta_file.with_name("metadata.json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(meta_file)


def _job_stop_signal(db: Session, job_id: int) -> str | None:
    """Fresh read of a job's status → "paused", "cancelled", or None. Detects a
    pause OR a cancellation (e.g. the creator was removed, or the job was
    cancelled from the queue) committed by another session while this worker is
    mid-download. A missing row (deleted out from under us) also means stop. The
    worker's own progress commits only touch the `progress` column, so they don't
    clobber the status set elsewhere."""
    status = db.scalar(select(models.QueueJob.status).where(models.QueueJob.id == job_id))
    if status is None or status == JobStatus.cancelled:
        return "cancelled"
    if status == JobStatus.paused:
        return "paused"
    return None


def _complete_from_existing(db: Session, job: models.QueueJob, media_path: Path, worker_name: str) -> bool:
    """Mark a job complete from media already on disk - no network download.
    Records a completed Download (if not already present) and updates metadata."""
    _ensure_title(db, job)  # this path writes the same record and sidecar
    is_image = media_path.is_dir()
    post_type = PostType.image if is_image else PostType.video
    size = (
        sum(f.stat().st_size for f in media_path.rglob("*") if f.is_file())
        if is_image else (media_path.stat().st_size if media_path.exists() else 0)
    )
    job.status = JobStatus.completed
    job.progress = 100.0
    job.worker = worker_name
    job.finished_at = datetime.now(timezone.utc)
    db.commit()

    if job.user_id:
        if job.video_id not in repo.Downloads.completed_video_ids(db, job.user_id):
            repo.Downloads.add(
                db, user_id=job.user_id, video_id=job.video_id, title=job.title,
                file_path=str(media_path), file_size=size, post_type=post_type,
                strategy="existing", status=DownloadStatus.completed, source=job.source,
                telegram_chat_id=job.telegram_chat_id,
            )
        repo.Posts.mark_downloaded(db, job.user_id, job.video_id)
        db.commit()
        # Adopted from disk rather than downloaded, but it is still a finished
        # download and belongs in MEGA like any other.
        row = _completed_row(db, job.user_id, job.video_id)
        if row:
            storage_service.store_download(db, row, media_path, post_url=job.url)

    _write_metadata(media_path.parent, {
        "video_id": job.video_id, "url": job.url, "title": job.title, "username": job.username,
        "strategy": "existing", "size": size, "type": post_type.value,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    })
    publish("queue_job", {"id": job.id, "status": "completed", "progress": 100})
    log.info("Skipped %s (@%s), already on disk", job.video_id, job.username)
    return True


def _already_in_cloud(db: Session, user_id: int, video_id: str) -> models.Download | None:
    """A completed download for this post whose media is stored in MEGA."""
    return db.scalar(
        select(models.Download).where(
            models.Download.user_id == user_id,
            models.Download.video_id == video_id,
            models.Download.status == DownloadStatus.completed,
            models.Download.remote_path.isnot(None),
        )
    )


def _complete_from_cloud(db: Session, job: models.QueueJob, worker_name: str) -> bool:
    """Finish a job whose post is already stored in MEGA - the cloud twin of
    _complete_from_existing. No transfer either way: the file is safe where it
    is, and pulling it back only to notice we have it would waste the quota."""
    _ensure_title(db, job)
    job.status = JobStatus.completed
    job.progress = 100.0
    job.worker = worker_name
    job.finished_at = datetime.now(timezone.utc)
    if job.user_id:
        repo.Posts.mark_downloaded(db, job.user_id, job.video_id)
    db.commit()
    publish("queue_job", {"id": job.id, "status": "completed", "progress": 100})
    log.info("Skipped %s (@%s), already stored in MEGA", job.video_id, job.username)
    return True


def _download_for_platform(user, job, target_dir, cookies, on_progress):
    """Dispatch a queued job to the engine that owns its platform.

    TikTok keeps the tikdownloader.io path (with its shared rate-limit governor
    and yt-dlp fallback); every other platform is handled by yt-dlp directly, so
    a non-TikTok link never burns a doomed tikdownloader attempt.
    """
    platform = getattr(user, "platform", None) or platforms.TIKTOK
    if platform == platforms.TIKTOK:
        # A private creator's posts are the same doomed-attempt case: tikdownloader
        # has no session, so it can only ever answer with the empty body it uses
        # for "can't fetch this". That wasted a paced API call per post AND was
        # counted as a soft block, which widens the shared interval for every
        # worker - so one private creator's backlog throttled the whole pool.
        # yt-dlp with the saved cookies is the only path that can serve these.
        private = getattr(user, "status", None) == UserStatus.private
        return downloader.download_video(
            job.url, target_dir, job.video_id, cookies, on_progress, private=private
        )

    from ..generic import downloader as generic_downloader

    return generic_downloader.download_media(
        job.url, target_dir, job.video_id, on_progress, title=job.title
    )


def _ensure_title(db: Session, job: models.QueueJob) -> None:
    """Fill in a missing post title before it is written to the library record
    and the metadata sidecar.

    Jobs reach the worker with a title already resolved when they came from a
    monitor scan or the in-app submit box, but a link sent to the Telegram bot
    carried none - so those downloads landed as "Untitled" with an empty title
    in their metadata, while an identical link pasted into the app did not.
    Resolving here rather than at enqueue keeps it off the path between sending
    a link and the download starting.
    """
    if job.title:
        return
    # The submitting request may have filled it in after this worker claimed the
    # job, in which case its copy is stale and no lookup is needed.
    try:
        db.refresh(job)
    except Exception:
        pass
    if job.title or not urls.is_tiktok_url(job.url or ""):
        return

    post = oembed.lookup(job.url)
    if post.title:
        job.title = post.title
        db.commit()


def process_job(db: Session, job: models.QueueJob, worker_name: str) -> bool:
    """Download a single queued job. Returns success."""
    user = repo.Users.get(db, job.user_id) if job.user_id else None
    if user and user.platform == platforms.INSTAGRAM and job.video_id == f"instagram-profile:{user.id}":
        from . import instagram_service

        return instagram_service.process_profile_job(db, job, user, lambda: _job_stop_signal(db, job.id))
    cookies = cookie_service.active_cookies_path(db)
    target_dir = account_dir(db, user, job.username, user.nickname if user else "")

    # Already on disk? (Saved outside the app, a duplicate bulk/catch-up enqueue,
    # or downloaded before a DB reset.) Record it as complete instead of wastefully
    # re-downloading - this is what stops "download all / catch-up" from fetching
    # media the user already has.
    existing = media_on_disk(target_dir, job.video_id)
    if existing:
        return _complete_from_existing(db, job, existing, worker_name)

    # Nothing on disk is not the same as nothing downloaded once media lives in
    # MEGA: the account folder then holds only a poster. Without this, every
    # catch-up sweep would fetch the whole back catalogue again.
    if job.user_id and _already_in_cloud(db, job.user_id, job.video_id):
        return _complete_from_cloud(db, job, worker_name)

    events_service.emit(
        db, ActivityType.download_started, f"Downloading {job.video_id} from @{job.username}",
        meta={"job_id": job.id}, user_id=job.user_id, log_source=LogSource.download,
    )

    last_pub = 0.0

    def on_progress(pct: float) -> None:
        nonlocal last_pub
        now = time.monotonic()
        if now - last_pub >= 0.4 or pct >= 100:
            last_pub = now
            # Cooperative stop: if this job was paused (per-item or via Stop All)
            # or cancelled (per-item, or because its creator was removed) abort
            # right here so the download actually stops mid-flight instead of
            # running to completion.
            signal = _job_stop_signal(db, job.id)
            if signal == "cancelled":
                raise downloader.DownloadCancelled()
            if signal == "paused":
                raise downloader.DownloadPaused()

            # Reporting progress must never be able to kill the transfer. Writing
            # the percentage takes a write lock, and under three workers SQLite
            # answers "database is locked" often enough to matter - that escaped
            # the callback, unwound the CDN read loop, and was caught upstream as
            # "tikdownloader exception", throwing away a working download and
            # re-fetching the whole file through yt-dlp. The stop signals above
            # still propagate: those are decisions, not bookkeeping.
            try:
                job.progress = pct
                db.commit()
                # getattr, not .value: the status column is a String, so a job that
                # was read back from the database rather than assigned in Python
                # carries the raw value and would abort the download here.
                status = getattr(job.status, "value", job.status)
                publish("queue_job", {"id": job.id, "status": status,
                                      "progress": round(pct, 1), "worker": worker_name})
            except (downloader.DownloadPaused, downloader.DownloadCancelled):
                raise
            except Exception as exc:
                db.rollback()   # leave the session usable for the next tick
                log.debug("progress update skipped: %s", exc)

    try:
        result = _download_for_platform(user, job, target_dir, cookies, on_progress)
    except downloader.DownloadCancelled:
        # Aborted because the job was cancelled mid-download (commonly: the
        # creator was removed). Finalise as cancelled - do NOT requeue or record
        # a download. The partial file (if any) is dropped by the downloader.
        job.status = JobStatus.cancelled
        job.worker = None
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
        publish("queue_job", {"id": job.id, "status": "cancelled", "progress": job.progress})
        log.info("Cancelled %s mid-download (@%s)", job.video_id, job.username)
        return False
    except downloader.DownloadPaused:
        job.status = JobStatus.paused
        job.worker = None
        job.started_at = None
        job.finished_at = None
        db.commit()
        publish("queue_job", {"id": job.id, "status": "paused", "progress": job.progress})
        log.info("Paused %s mid-download (@%s)", job.video_id, job.username)
        return False

    if not result.success:
        # Retry what can be retried, and only that.
        #
        # A post TikTok will not serve to this region never succeeds however
        # often it is asked, so retrying it burns a worker slot that a winnable
        # download needs - which is exactly what makes a bulk run look broken.
        # A timeout or a WAF hiccup, on the other hand, usually clears within
        # seconds, and those were being abandoned on the first failure.
        raw = downloader.last_error(job.url)
        kind = downloader.classify_failure(raw or result.error or "")
        attempts = (job.attempts or 0)
        if kind == downloader.TRANSIENT and attempts < MAX_DOWNLOAD_ATTEMPTS:
            # `attempts` is NOT incremented here. claim_next already bumps it
            # every time a worker takes the job, so counting again made each
            # retry cost two and the cap fire at unpredictable points - jobs were
            # reaching eight attempts against a limit of three.
            job.status = JobStatus.pending
            job.worker = None
            job.started_at = None
            job.error = result.error
            db.commit()
            publish("queue_job", {"id": job.id, "status": "pending", "progress": 0})
            log.info("Retrying %s (@%s) after a transient failure, attempt %d of %d: %s",
                     job.video_id, job.username, attempts, MAX_DOWNLOAD_ATTEMPTS, result.error)
            events_service.emit(
                db, ActivityType.download_failed,
                f"Retrying {job.video_id} (@{job.username}): {result.error}",
                meta={"job_id": job.id, "attempt": job.attempts},
                user_id=job.user_id, log_level=LogLevel.warning, log_source=LogSource.download,
            )
            return False

        job.status = JobStatus.failed
        job.error = result.error
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
        repo.Downloads.add(
            db, user_id=job.user_id or 0, video_id=job.video_id, title=job.title,
            status=DownloadStatus.failed, source=job.source, error=result.error,
            telegram_chat_id=job.telegram_chat_id,
        ) if job.user_id else None
        db.commit()
        events_service.emit(
            db, ActivityType.download_failed, f"Download failed for {job.video_id} (@{job.username})",
            meta={"job_id": job.id}, user_id=job.user_id, log_level=LogLevel.error, log_source=LogSource.download,
            notify=(NotificationType.error, "Download failed", f"@{job.username} · {job.video_id}"),
        )
        publish("queue_job", {"id": job.id, "status": "failed", "progress": job.progress})
        if user:
            _recompute_success_rate(db, user)
        return False

    # success
    post_type = PostType.image if result.is_image else PostType.video
    job.status = JobStatus.completed
    job.progress = 100.0
    job.finished_at = datetime.now(timezone.utc)
    db.commit()

    _ensure_title(db, job)

    # Guarded the same way the already-on-disk path is: two jobs for one post
    # (the same link submitted twice, or sent to the bot and then pasted into the
    # app) both pass the on-disk check while neither has finished, and would
    # otherwise each add a library row for the same file.
    if job.user_id:
        if job.video_id not in repo.Downloads.completed_video_ids(db, job.user_id):
            repo.Downloads.add(
                db,
                user_id=job.user_id,
                video_id=job.video_id,
                title=job.title,
                file_path=str(result.path) if result.path else None,
                file_size=result.size,
                post_type=post_type,
                strategy=result.strategy,
                status=DownloadStatus.completed,
                source=job.source,
                telegram_chat_id=job.telegram_chat_id,
            )
        # Runs either way: the post is downloaded even when another job for it
        # got there first, and the record already exists.
        repo.Posts.mark_downloaded(db, job.user_id, job.video_id)
        if user:
            user.last_download = datetime.now(timezone.utc)
        db.commit()

    _write_metadata(target_dir, {
        "video_id": job.video_id,
        "url": job.url,
        "title": job.title,
        "username": job.username,
        "strategy": result.strategy,
        "size": result.size,
        "type": post_type.value,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    })

    events_service.emit(
        db, ActivityType.download_completed,
        f"Downloaded {'photo' if result.is_image else 'video'} {job.video_id} from @{job.username}",
        meta={"job_id": job.id, "size": result.size, "strategy": result.strategy},
        user_id=job.user_id, log_source=LogSource.download,
        notify=(NotificationType.success, "Download complete", f"@{job.username} · {job.video_id}"),
    )
    publish("queue_job", {"id": job.id, "status": "completed", "progress": 100})
    if user:
        _recompute_success_rate(db, user)

    # Announce genuinely-new monitored posts to Telegram (no-op unless the
    # feature is configured with authorized chat IDs). Only "monitor" jobs are
    # new content; bulk back-catalogue and ad-hoc Telegram link downloads are
    # excluded so they don't spam the chat.
    if job.source == "monitor" and user is not None:
        try:
            from . import telegram_service

            telegram_service.announce_new_post(
                db,
                username=user.username,
                nickname=user.nickname or user.username,
                caption=job.title or "",
                post_url=job.url,
                file_path=str(result.path) if result.path else None,
                post_type=post_type,
            )
        except Exception as exc:  # never let an announce failure fail the job
            log.warning("telegram new-post announce failed: %s", exc)

    # Last, so everything above still sees the real file. The Telegram announce
    # in particular reads the bytes off disk to attach them; moving the media
    # first would leave it uploading a file that is no longer there.
    _store_in_cloud(db, job, result)
    return True


def _store_in_cloud(db: Session, job: models.QueueJob, result) -> None:
    """Hand a finished download to MEGA and drop the local copy.

    A no-op unless MEGA storage is on. Failure is never fatal: the file simply
    stays on this machine, which is exactly what the app did before.
    """
    if not result.path or not job.user_id:
        return
    row = _completed_row(db, job.user_id, job.video_id)
    if row and storage_service.store_download(db, row, Path(result.path), post_url=job.url):
        publish("download", {"user": job.username, "stored": "mega"})


def _completed_row(db: Session, user_id: int, video_id: str) -> models.Download | None:
    return db.scalar(
        select(models.Download).where(
            models.Download.user_id == user_id,
            models.Download.video_id == video_id,
            models.Download.status == DownloadStatus.completed,
        ).order_by(models.Download.id.desc())
    )


def _recompute_success_rate(db: Session, user: models.User) -> None:
    from sqlalchemy import func, select

    total = db.scalar(select(func.count(models.Download.id)).where(models.Download.user_id == user.id)) or 0
    ok = db.scalar(
        select(func.count(models.Download.id)).where(
            models.Download.user_id == user.id, models.Download.status == DownloadStatus.completed
        )
    ) or 0
    user.success_rate = round((ok / total * 100) if total else 100.0, 1)
    db.commit()


def download_from_url(
    db: Session, url: str, video_id: str, user: models.User,
    source: str = "telegram", title: str = "", chat_id: int | None = None,
) -> bool:
    """Direct download path used by the Telegram bot (creates a queue job + processes it)."""
    # The same post already on its way (sent twice, or pasted into the app too)
    # is left to the job that owns it rather than downloaded a second time in
    # parallel over the same file.
    if repo.Queue.active_for_video(db, user.id, video_id) is not None:
        log.info("Skipping %s (@%s), already queued", video_id, user.username)
        return True

    job = repo.Queue.enqueue(
        db, user_id=user.id, username=user.username, video_id=video_id,
        url=url, title=title, source=source, telegram_chat_id=chat_id,
    )
    job.status = JobStatus.processing
    job.started_at = datetime.now(timezone.utc)
    job.attempts = 1
    db.commit()
    return process_job(db, job, "telegram")
