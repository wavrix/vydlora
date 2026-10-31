"""Import download history from a previous Lora 1 installation.

Lora 1 wrote a per-account ``metadata.json`` mapping ``video_id`` → entry::

    {"video_id": …, "url": …, "title": …, "username": …,
     "strategy": …, "size": …, "type": "video"|"image",
     "downloaded_at": "<iso8601>"}

An export directory holds one such file per creator (``<username>.json``); more
than one file may cover the same creator, and they are merged.

Importing records each entry as a COMPLETED download plus a downloaded post, so
the existing dedupe paths ("download all", catch-up, scanning) treat those posts
as already saved and never fetch them again.

Creators are imported **unmonitored** by default: the goal is history + dedupe,
and silently adding dozens of accounts to the scan roster would be a surprising
side effect. Adding such a creator later promotes the same row to monitored
(``user_service._promote_existing``) rather than duplicating it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, platforms, repositories as repo
from ..core.logging_config import get_logger
from ..models.enums import ActivityType, DownloadStatus, LogSource, NotificationType, PostType, UserStatus
from ..tiktok import naming
from . import events_service, settings_service

log = get_logger("import")


def _parse_when(value: str | None) -> datetime:
    """Preserve the original download timestamp so history keeps its real dates."""
    if value:
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _account_dir_readonly(db: Session, user: models.User) -> Path:
    """Where this creator's media would live - WITHOUT creating the folder
    (an import must not litter the library with empty directories)."""
    folder = user.folder_name or naming.account_folder_name(user.nickname or user.username, user.username)
    return settings_service.downloads_path(db) / platforms.display_name(user.platform or platforms.TIKTOK) / folder


def _get_or_create_user(db: Session, username: str, monitored: bool) -> models.User:
    key = platforms.state_key(platforms.TIKTOK, username)
    user = db.scalar(select(models.User).where(models.User.state_key == key))
    if user:
        return user
    user = models.User(
        state_key=key,
        platform=platforms.TIKTOK,
        username=username,
        nickname=username,
        status=UserStatus.monitoring if monitored else UserStatus.active,
        monitored=monitored,
        folder_name=naming.account_folder_name(username, username),
    )
    db.add(user)
    db.flush()
    return user


def _collect(directory: Path) -> tuple[dict[str, dict[str, dict]], list[str]]:
    """Merge every *.json in `directory` into {username: {video_id: entry}}."""
    by_user: dict[str, dict[str, dict]] = {}
    problems: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            problems.append(f"{path.name}: unreadable ({exc})")
            continue
        if not isinstance(data, dict):
            problems.append(f"{path.name}: not a metadata object")
            continue
        for vid, entry in data.items():
            if not isinstance(entry, dict):
                continue
            username = (entry.get("username") or path.stem).strip().lstrip("@")
            video_id = str(entry.get("video_id") or vid).strip()
            if not username or not video_id:
                continue
            by_user.setdefault(username, {})[video_id] = entry
    return by_user, problems


def import_metadata_dir(db: Session, directory: str | Path, monitored: bool = False) -> dict:
    """Import a Lora 1 metadata export. Idempotent - re-running only adds
    entries that aren't already recorded."""
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")

    by_user, problems = _collect(root)
    if not by_user:
        raise ValueError("No usable metadata files found in that folder.")

    creators = imported = skipped = located = 0

    for username, entries in sorted(by_user.items()):
        try:
            user = _get_or_create_user(db, username, monitored)
            # Load the creator's known ids once instead of querying per entry.
            already = repo.Downloads.completed_video_ids(db, user.id)
            seen = repo.Posts.seen_ids(db, user.id)
            target_dir = _account_dir_readonly(db, user)
            added_here = 0

            for video_id, entry in entries.items():
                if video_id in already:
                    skipped += 1
                    continue

                is_image = (entry.get("type") or "video").lower() == "image"
                when = _parse_when(entry.get("downloaded_at"))
                url = entry.get("url") or f"https://www.tiktok.com/@{username}/video/{video_id}"
                title = entry.get("title") or ""

                # Link the record to the media only when it's really on disk, so
                # storage stats and "open file" stay truthful after a restore.
                media = None
                if target_dir.exists():
                    from . import download_service

                    media = download_service.media_on_disk(target_dir, video_id)
                if media is not None:
                    located += 1

                if video_id not in seen:
                    repo.Posts.add(db, user.id, video_id, url, title, downloaded=True)
                    seen.add(video_id)
                else:
                    repo.Posts.mark_downloaded(db, user.id, video_id)

                dl = repo.Downloads.add(
                    db,
                    user_id=user.id,
                    video_id=video_id,
                    title=title,
                    file_path=str(media) if media else None,
                    file_size=int(entry.get("size") or 0) if media else 0,
                    post_type=PostType.image if is_image else PostType.video,
                    strategy=str(entry.get("strategy") or "import"),
                    status=DownloadStatus.completed,
                    source="import",
                )
                dl.created_at = when      # keep the original download date
                already.add(video_id)
                imported += 1
                added_here += 1

            if added_here:
                creators += 1
            db.commit()
        except Exception as exc:  # never let one creator abort the whole import
            log.error("[@%s] metadata import failed: %s", username, exc)
            db.rollback()
            problems.append(f"@{username}: {exc}")

    log.info("metadata import: %d record(s) across %d creator(s), %d already known, %d file(s) located",
             imported, creators, skipped, located)

    if imported:
        events_service.emit(
            db, ActivityType.scan_completed,
            f"Imported {imported} download record(s) from {creators} creator(s). These won't be re-downloaded",
            meta={"imported": imported, "creators": creators, "skipped": skipped},
            log_source=LogSource.system,
            notify=(NotificationType.success, "History imported", f"{imported} record(s) recognised"),
        )

    return {
        "imported": imported,
        "creators": creators,
        "skipped_existing": skipped,
        "files_located": located,
        "problems": problems[:20],
    }
