"""One-time disk → database reconciliation.

Records media that already exists in each creator's download folder - including
files saved outside the app, or before a database reset - as completed downloads,
marks the matching posts downloaded, and writes them into the folder's
``metadata.json``. Without this, the catch-up / "download all" logic (which reads
"already downloaded" from the DB) sees nothing and re-downloads everything.

Only files named by their TikTok video id are matched: ``{video_id}.mp4`` (video)
or a ``{video_id}/`` image folder. Media saved under some other name can't be
mapped back to a post and is left alone.

Idempotent - re-running only adds media not already recorded. The startup caller
runs it a single time (guarded by a settings flag); a manual endpoint can force it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from .. import models, repositories as repo
from ..core.logging_config import get_logger
from ..models.enums import ActivityType, DownloadStatus, LogSource, NotificationType, PostType
from . import download_service, events_service

log = get_logger("reconcile")

RECONCILE_FLAG = "disk_reconcile_done"


def _media_size(path: Path) -> int:
    if path.is_dir():
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return path.stat().st_size if path.exists() else 0


def reconcile_user(db: Session, user: models.User) -> int:
    """Record every on-disk post for one creator that the DB doesn't yet know
    about. Returns how many were newly recorded."""
    target_dir = download_service.account_dir(db, user, user.username, user.nickname)
    disk_ids = download_service.downloaded_ids_on_disk(target_dir)
    if not disk_ids:
        return 0
    already = repo.Downloads.completed_video_ids(db, user.id)
    seen = repo.Posts.seen_ids(db, user.id)

    recorded = 0
    for vid in sorted(disk_ids - already):
        media = download_service.media_on_disk(target_dir, vid)
        if not media:
            continue
        is_image = media.is_dir()
        post_type = PostType.image if is_image else PostType.video
        url = f"https://www.tiktok.com/@{user.username}/video/{vid}"
        size = _media_size(media)

        if vid in seen:
            repo.Posts.mark_downloaded(db, user.id, vid)
        else:
            repo.Posts.add(db, user.id, vid, url, title="", downloaded=True)
        repo.Downloads.add(
            db,
            user_id=user.id,
            video_id=vid,
            title="",
            file_path=str(media),
            file_size=size,
            post_type=post_type,
            strategy="import",
            status=DownloadStatus.completed,
            source="import",
        )
        download_service._write_metadata(target_dir, {
            "video_id": vid,
            "url": url,
            "title": "",
            "username": user.username,
            "strategy": "import",
            "size": size,
            "type": post_type.value,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
        })
        recorded += 1

    if recorded:
        db.commit()
        log.info("[@%s] reconciled %d existing file(s) from disk", user.username, recorded)
    return recorded


def reconcile_all(db: Session) -> dict:
    """Scan every creator's folder and import existing media into the DB +
    metadata.json. Returns a summary; never raises per-creator."""
    users = repo.Users.all_enabled(db)
    total = 0
    creators = 0
    for user in users:
        try:
            n = reconcile_user(db, user)
            if n:
                creators += 1
                total += n
        except Exception as exc:  # pragma: no cover
            log.error("[@%s] reconcile error: %s", user.username, exc)
            db.rollback()

    if total:
        events_service.emit(
            db,
            ActivityType.scan_completed,
            f"Imported {total} existing file(s) from disk across {creators} creator(s): "
            "these won't be re-downloaded",
            meta={"recorded": total, "creators": creators},
            log_source=LogSource.system,
            notify=(NotificationType.success, "Existing downloads imported", f"{total} file(s) recognised"),
        )
    log.info("disk reconciliation complete: %d file(s) across %d creator(s)", total, creators)
    return {"recorded": total, "creators": creators}


def reconcile_once(db: Session) -> dict:
    """Run reconcile_all a single time, guarded by a persisted flag. Subsequent
    calls are no-ops so the disk isn't rescanned on every launch."""
    if repo.Settings.get(db, RECONCILE_FLAG, None):
        return {"skipped": "already-done"}
    result = reconcile_all(db)
    repo.Settings.set(db, RECONCILE_FLAG, True)
    db.commit()
    return result
