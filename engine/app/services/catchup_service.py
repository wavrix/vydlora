"""Offline catch-up - backfill everything missed while Lora 1 wasn't running.

Triggered on app launch and whenever the internet reconnects after a drop (see
``workers.manager``). For every monitored creator it lists the full back-catalogue
and enqueues any post that isn't already downloaded or queued, then runs a story
sweep for whatever stories are still active. Posts persist, so nothing published
during the offline window is lost; stories are ephemeral (~24h), so only those
still live on TikTok can be recovered.

Kept deliberately gentle: per-creator fetches are spaced by the configurable
`scan_spacing_min/max` jitter, and the whole run is skipped when monitoring is
paused or a large download backlog is already in flight.
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

from .. import platforms, models, repositories as repo
from ..core.logging_config import get_logger
from ..models.enums import ActivityType, LogSource, NotificationType
from ..tiktok import listing
from . import cookie_service, download_service, events_service, monitor_service, settings_service

log = get_logger("catchup")


def _enqueue_missing(db: Session, user: models.User, videos: list[dict]) -> int:
    """Enqueue every listed post that isn't already downloaded or queued. Mirrors
    monitor_service.download_all_posts but stays quiet (one summary event is
    emitted by the caller instead of one per creator).

    "Already downloaded" means a completed DB record OR a media file already on
    disk - so posts saved outside the app aren't re-downloaded even when the DB
    doesn't know about them yet."""
    target_dir = download_service.account_dir(db, user, user.username, user.nickname)
    # "Accounted for" is three things, not two: a completed library record, a file
    # already on disk, or a post explicitly marked downloaded. That last one is
    # what lets a one-off baseline retire a back catalogue - without it, marking
    # posts only ever affected the scanner, and catch-up queued the whole
    # catalogue again on its next run.
    downloaded = (
        repo.Downloads.completed_video_ids(db, user.id)
        | download_service.downloaded_ids_on_disk(target_dir)
        | repo.Posts.downloaded_ids(db, user.id)
    )
    active = repo.Queue.active_video_ids(db, user.id)
    seen = repo.Posts.seen_ids(db, user.id)

    queued = 0
    for v in videos:
        vid = v["id"]
        if vid not in seen:
            repo.Posts.add(db, user.id, vid, v["url"], v.get("title", ""), downloaded=(vid in downloaded))
        if vid in downloaded or vid in active:
            continue
        repo.Queue.enqueue(
            db, user_id=user.id, username=user.username, video_id=vid,
            url=v["url"], title=v.get("title", ""), source="catchup",
        )
        active.add(vid)
        queued += 1
    return queued


def catch_up_all(db: Session, reason: str = "startup") -> dict:
    """Backfill undownloaded posts (full catalogue) + active stories for every
    monitored creator. Returns a summary dict; never raises."""
    if settings_service.get(db, "monitoring_paused"):
        log.info("catch-up (%s) skipped: monitoring paused", reason)
        return {"skipped": "paused"}

    counts = repo.Queue.counts(db)
    backlog = int(counts.get("pending", 0)) + int(counts.get("processing", 0))
    if backlog >= monitor_service.SCAN_BACKLOG_PAUSE:
        log.info("catch-up (%s) deferred: %d download job(s) already queued", reason, backlog)
        return {"skipped": "backlog", "backlog": backlog}

    cookies = cookie_service.active_cookies_path(db)
    users = repo.Users.all_enabled(db, platform=platforms.TIKTOK)
    if not users:
        return {"queued": 0, "creators": 0, "stories": 0}

    timeout = monitor_service._all_posts_timeout(db)
    total_queued = 0
    creators_with_new = 0
    log.info("catch-up (%s) started: scanning %d creator(s) for missed posts", reason, len(users))

    stopped_early = False
    for i, user in enumerate(users):
        # A full catch-up runs for minutes across a large roster. Without this
        # the flag was read once before the loop, so pausing part-way through
        # left it scanning every remaining creator.
        if monitor_service.is_paused(db):
            log.info("catch-up (%s) stopped early: monitoring was paused", reason)
            stopped_early = True
            break
        if i > 0:
            time.sleep(settings_service.spacing_delay(db))
        try:
            # Pass the stored secUid so WAF-blocked / private-followed accounts
            # still enumerate. No browser here - catch-up runs unattended and the
            # headless fetch is too heavy for a full sweep.
            videos = listing.fetch_all(
                user.username, cookies, timeout=timeout, sec_uid=user.sec_uid or ""
            )
            queued = _enqueue_missing(db, user, videos)
            if queued:
                db.commit()
                creators_with_new += 1
                total_queued += queued
                log.info("[@%s] catch-up queued %d missed post(s)", user.username, queued)
        except Exception as exc:  # pragma: no cover
            log.error("[@%s] catch-up error: %s", user.username, exc)
            db.rollback()

    # Stories are ephemeral - only those still active (within ~24h) can be
    # recovered. The story sweep no-ops unless stories are enabled + logged in.
    story_new = 0
    if not stopped_early and settings_service.get(db, "monitor_stories"):
        try:
            from . import story_service

            story_new = story_service.scan_all_stories(db)
        except Exception as exc:  # pragma: no cover
            log.error("catch-up (%s) story sweep error: %s", reason, exc)

    if total_queued or story_new:
        story_note = f", {story_new} new story item(s)" if story_new else ""
        events_service.emit(
            db,
            ActivityType.scan_completed,
            f"Catch-up ({reason}): queued {total_queued} missed post(s) "
            f"from {creators_with_new} creator(s){story_note}",
            meta={"queued": total_queued, "creators": creators_with_new, "stories": story_new, "reason": reason},
            log_source=LogSource.monitor,
            notify=(
                NotificationType.info,
                "Catch-up complete",
                f"{total_queued} missed post(s)" + story_note,
            ),
        )
    log.info(
        "catch-up (%s) done: %d post(s) from %d creator(s), %d story item(s)",
        reason, total_queued, creators_with_new, story_new,
    )
    return {"queued": total_queued, "creators": creators_with_new, "stories": story_new}
