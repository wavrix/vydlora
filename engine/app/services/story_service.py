"""Story monitoring - detect and download new TikTok stories for creators.

Runs as a separate, lower-frequency sweep from the post scanner: stories require
the logged-in headless browser (to sign TikTok's story API) and last ~24h, so
checking every ~30 min is ample. Opt-in via the `monitor_stories` setting.
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from pathlib import Path

from .. import platforms, models, repositories as repo
from ..core.events import publish
from ..core.logging_config import get_logger
from ..models.enums import (
    ActivityType,
    DownloadStatus,
    LogSource,
    NotificationType,
    PostType,
    UserStatus,
)
from ..tiktok import downloader, stories
from . import cookie_service, download_service, events_service, settings_service, storage_service

log = get_logger("stories")

# Spacing between per-account story fetches. Each fetch drives the (serialised)
# headless browser, so this mostly bounds how hard we hit TikTok during a sweep.
STORY_SPACING_SECS = 2.0

# Per-creator story-check throttle, SHARED between the inline (post-scan) check in
# monitor_service and the periodic sweep below, so a creator's stories are fetched
# at most once per `story_scan_interval` no matter which path triggers it - the
# story browser is a single serialised Chromium and must not be hammered.
_story_throttle_lock = threading.Lock()
_story_last_check: dict[int, float] = {}


def release_story_check(user_id: int) -> None:
    """Forget that a check was started for this creator.

    Called when the browser was too busy to run it. The throttle mark is taken
    at dispatch, so without this a refused creator would look freshly checked
    and wait a whole interval for a check that never happened - the busiest
    creators would be starved exactly when the queue is under pressure."""
    with _story_throttle_lock:
        _story_last_check.pop(user_id, None)


def story_check_due(user_id: int, interval: float) -> bool:
    """True at most once per `interval` seconds per creator (and records the time)."""
    now = time.monotonic()
    with _story_throttle_lock:
        if now - _story_last_check.get(user_id, 0.0) >= interval:
            _story_last_check[user_id] = now
            return True
    return False


# ── Background story worker ───────────────────────────────────────────────────
# Story checks drive the single, serialised headless browser, so running them in
# line with a post scan makes the whole sweep crawl (the first pass would wait on
# ~one browser fetch per creator). Instead the post scanner DISPATCHES a due
# story check onto this queue and returns immediately; one background worker
# drains it, so post scanning stays fast while stories trickle in behind it.
_story_q: "queue.Queue[int]" = queue.Queue()
_story_worker_started = False
_story_worker_lock = threading.Lock()


def _story_worker_loop() -> None:
    from ..core.database import session_scope

    while True:
        user_id = _story_q.get()
        try:
            with session_scope() as db:
                if not settings_service.get(db, "monitor_stories"):
                    continue
                # The queue outlives the sweep that filled it, so a pause has to
                # be honoured here too - otherwise checks dispatched moments
                # before the pause keep driving the browser afterwards.
                from . import monitor_service

                if monitor_service.is_paused(db):
                    release_story_check(user_id)
                    continue
                user = repo.Users.get(db, user_id)
                if user:
                    scan_user_stories(db, user)
        except Exception as exc:  # pragma: no cover
            log.error("[uid=%s] background story check failed: %s", user_id, exc)
        finally:
            _story_q.task_done()


def _ensure_story_worker() -> None:
    global _story_worker_started
    with _story_worker_lock:
        if not _story_worker_started:
            threading.Thread(target=_story_worker_loop, name="story-worker", daemon=True).start()
            _story_worker_started = True


def dispatch_story_check(user_id: int, interval: float) -> None:
    """Queue a background story check for a creator (non-blocking, throttled). Safe
    to call from a post scan - it never touches the browser on the caller's thread."""
    if story_check_due(user_id, interval):
        _ensure_story_worker()
        _story_q.put(user_id)


def _story_url(username: str, story_id: str) -> str:
    return f"https://www.tiktok.com/@{username}/story/{story_id}"


def _dir_or_file_bytes(path: Path) -> int:
    if path.is_dir():
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return path.stat().st_size if path.exists() else 0


def _download_story(user: models.User, it: stories.StoryItem, target: Path, cookies: Path) -> tuple[Path | None, int, str]:
    """Fetch a story's media. Public accounts go through tikdownloader.io (clean,
    watermark-free, no session needed); private accounts - whose stories aren't
    reachable without an authenticated follow - use the direct session download.
    Public downloads fall back to the direct method if tikdownloader can't serve
    it. Returns (path, bytes, strategy)."""
    is_private = it.private or user.status == UserStatus.private
    # Photo stories never go through tikdownloader. It is a video service, and
    # the call hands it a `.mp4` destination - so an image story (a still plus a
    # separate audio track) either fails or lands as an .mp4 that is really the
    # soundtrack: unplayable, and listed as a video whose duration is the song's.
    # The direct path below writes them correctly, as a folder of images.
    if not is_private and it.kind != "photo":
        out = target / f"{it.story_id}.mp4"
        try:
            res = downloader.tikdownloader_download(_story_url(user.username, it.story_id), out, None)
            if res and res.success and res.path:
                p = Path(res.path)
                return p, (res.size or _dir_or_file_bytes(p)), "tikdownloader"
            log.info("[@%s] tikdownloader had no story media for %s, using direct", user.username, it.story_id)
        except Exception as exc:
            log.warning("[@%s] tikdownloader story %s failed (%s); using direct", user.username, it.story_id, exc)

    path, nbytes = stories.download_story(it, target, cookies)
    return path, nbytes, "story"


def scan_user_stories(db: Session, user: models.User) -> int:
    """Fetch, download and record any NEW active stories for one creator.

    A story is only marked seen after its media downloads successfully, so a
    transient failure is retried on the next sweep (well within the 24h window)."""
    cookies = cookie_service.active_cookies_path(db)
    timeout = int(settings_service.get(db, "request_timeout"))

    items = stories.list_stories(user.username, cookies, timeout=timeout)
    if items is None:
        # The browser was saturated. Make the creator eligible again rather than
        # recording a check that never happened.
        release_story_check(user.id)
        log.debug("[@%s] story check skipped: browser busy", user.username)
        return 0
    if not items:
        return 0

    seen = repo.Posts.seen_story_ids(db, user.id)
    new_items = [it for it in items if it.story_id not in seen]
    if not new_items:
        return 0

    target = download_service.account_dir(db, user, user.username, user.nickname) / "Stories"
    downloaded = 0
    for it in new_items:
        path, nbytes, strategy = _download_story(user, it, target, cookies)
        if not path:
            log.warning("[@%s] story %s media download failed, will retry next sweep", user.username, it.story_id)
            continue
        post_type = PostType.image if it.kind == "photo" else PostType.video
        repo.Posts.add(
            db, user.id, it.story_id, _story_url(user.username, it.story_id),
            title=it.desc or "Story", downloaded=True, is_story=True,
        )
        record = repo.Downloads.add(
            db,
            user_id=user.id,
            video_id=it.story_id,
            title=it.desc or "Story",
            file_path=str(path),
            file_size=nbytes,
            post_type=post_type,
            strategy=strategy,
            status=DownloadStatus.completed,
            source="story",
        )
        db.commit()
        # A story is a finished download like any other, so it goes to MEGA too.
        # Stories expire within a day, which makes them the media most worth
        # getting off this machine and into somewhere permanent.
        storage_service.store_download(db, record, path, post_url=_story_url(user.username, it.story_id))
        user.last_download = datetime.now(timezone.utc)
        downloaded += 1
        events_service.emit(
            db, ActivityType.download_completed,
            f"Downloaded story ({it.kind}) from @{user.username}",
            meta={"story_id": it.story_id, "size": nbytes}, user_id=user.id, log_source=LogSource.monitor,
            notify=(NotificationType.success, "New story archived", f"@{user.username}"),
        )
        publish("download", {"user": user.username, "story": True})

    if downloaded:
        db.commit()
    if new_items:
        # One line so a "detected N but downloaded fewer" gap is obvious in the
        # logs. Failed items aren't marked seen, so they retry on the next sweep.
        log.info(
            "[@%s] stories: %d active, %d new, %d downloaded, %d failed (retry next sweep)",
            user.username, len(items), len(new_items), downloaded, len(new_items) - downloaded,
        )
    return downloaded


def scan_all_stories(db: Session) -> int:
    """Sweep every monitored creator for new stories. No-op unless enabled and a
    logged-in cookie session is present (stories are followers-only)."""
    if not settings_service.get(db, "monitor_stories"):
        return 0
    if settings_service.get(db, "monitoring_paused"):
        return 0
    cookies = cookie_service.active_cookies_path(db)
    if not cookie_service.profile_is_authenticated(cookies):
        log.info("story sweep skipped: active cookies have no logged-in session")
        return 0

    users = repo.Users.all_enabled(db, platform=platforms.TIKTOK)
    if not users:
        return 0
    interval = float(settings_service.get(db, "story_scan_interval"))
    total = 0
    checked = 0
    from . import monitor_service

    for user in users:
        # Re-read the pause each creator: a story sweep drives a serialised
        # browser and can run for a long time, so checking only before the loop
        # left it working through the whole roster after a pause.
        if monitor_service.is_paused(db):
            log.info("story sweep stopped early: monitoring was paused")
            break
        # Skip creators whose stories were already checked recently - likely inline
        # with their post scan (see monitor_service). Shared throttle avoids double
        # browser fetches.
        if not story_check_due(user.id, interval):
            continue
        if checked > 0:
            # Configurable jittered spacing (scan_spacing_min/max) between
            # per-creator story fetches; each drives the serialised browser.
            time.sleep(settings_service.spacing_delay(db))
        checked += 1
        try:
            total += scan_user_stories(db, user)
        except Exception as exc:  # pragma: no cover
            log.error("[@%s] story scan error: %s", user.username, exc)
            db.rollback()
    if total:
        log.info("story sweep archived %d new story item(s) across %d creator(s)", total, checked)
    return total
