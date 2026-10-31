"""Monitoring for Instagram creators.

The Instagram counterpart to `monitor_service`, following the same shape so the
two behave alike from the outside: a due-list driven sweep, one isolated session
per creator, the same three-strike failure rule before an account is called
unreachable, the same `scan` events driving the UI, and the same status
transitions on the user row.

Two things are deliberately different, because the sites are:

* **New posts are found by downloading, not by listing.** TikTok lists a
  profile and enqueues the ids it has not seen. gallery-dl has no cheap listing
  step, so instead it is pointed at the newest few posts with a download archive
  behind it. Anything already fetched is skipped without transferring it, so a
  sweep that finds nothing new costs one request per creator rather than a
  re-download of their back catalogue.
* **There is no queue.** A TikTok scan enqueues jobs for the download workers.
  Instagram downloads inline through gallery-dl, which is already one process
  per creator and already rate-limited by `instagram_sleep`.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .. import models, platforms, repositories as repo
from ..core.database import session_scope
from ..core.events import publish
from ..core.logging_config import get_logger
from ..instagram import urls as ig_urls
from ..models.enums import ActivityType, LogLevel, LogSource, UserStatus
from . import cookie_service, events_service, instagram_service, settings_service

log = get_logger("instagram.monitor")

# Matches monitor_service: one blip must not disable a live account, and a
# disabled account is skipped by later sweeps, so the mistake would be silent
# and permanent.
SCAN_FAILURE_THRESHOLD = 3

# How many of the newest posts a sweep looks at. The archive means an unchanged
# creator costs the listing request and nothing more, so this only bounds how
# much can arrive at once after a gap.
DEFAULT_POSTS_PER_SCAN = 5

# Ceiling on parallel creator checks, mirroring the TikTok sweep. Each one is a
# gallery-dl process, so this is a real resource bound rather than a courtesy.
SCAN_CONCURRENCY_MAX = 6

# Instagram currently redirects gallery-dl's posts and reels extractors to the
# home page for every account, signed in or not. That is a site-wide condition,
# not a per-creator one, so retrying it for each creator on every sweep spends a
# request and a rate-limit allowance per creator to learn the same thing again.
#
# After it fails, the posts phase is skipped for a cooldown and then tried once
# more, so the moment Instagram starts answering again it is picked back up
# without anyone having to notice or restart anything.
POSTS_BLOCKED_SIGNATURES = ("redirect to home", "redirect to the home")
POSTS_RETRY_AFTER_SECS = 1800.0
_posts_blocked_until = 0.0
_posts_lock = threading.Lock()
_scan_locks: dict[int, threading.Lock] = {}
_scan_locks_guard = threading.Lock()
_sweep_lock = threading.Lock()


# When the sweep last completed a tick, whether or not anything was due.
#
# A tick that finds nothing due does no work and says nothing, which is correct
# but indistinguishable from a sweep that has died. Recording it gives the UI
# something to show, so "monitoring is running" is a fact rather than a claim.
_last_run_at: datetime | None = None


def last_run_at() -> datetime | None:
    with _posts_lock:
        return _last_run_at


def _note_run() -> None:
    global _last_run_at
    with _posts_lock:
        _last_run_at = datetime.now(timezone.utc)


def _posts_blocked() -> bool:
    with _posts_lock:
        return time.monotonic() < _posts_blocked_until


def _note_posts_blocked(reason: str) -> bool:
    """Start the cooldown if this failure is the site-wide redirect. True if so."""
    if not any(sig in (reason or "").lower() for sig in POSTS_BLOCKED_SIGNATURES):
        return False
    global _posts_blocked_until
    with _posts_lock:
        first = time.monotonic() >= _posts_blocked_until
        _posts_blocked_until = time.monotonic() + POSTS_RETRY_AFTER_SECS
    if first:
        log.info("Instagram is redirecting the posts endpoint; skipping it for "
                 "%d minutes. Stories and highlights are unaffected.",
                 int(POSTS_RETRY_AFTER_SECS // 60))
    return True


def _enabled(db: Session) -> bool:
    return bool(settings_service.get(db, "instagram_monitor_enabled"))


def is_paused(db: Session) -> bool:
    """Instagram follows the same global pause as TikTok.

    Pausing monitoring means pausing monitoring; having one platform carry on
    would make the switch mean something different depending on which tab you
    were looking at.
    """
    return bool(settings_service.get(db, "monitoring_paused"))


def scan_user(db: Session, user: models.User) -> int:
    if user.platform != platforms.INSTAGRAM:
        return 0
    with _scan_locks_guard:
        lock = _scan_locks.setdefault(user.id, threading.Lock())
    if not lock.acquire(blocking=False):
        log.debug("[@%s] Instagram check already running", user.username)
        return 0
    try:
        db.refresh(user)
        return _scan_user(db, user)
    finally:
        lock.release()


def _scan_user(db: Session, user: models.User) -> int:
    """Check one Instagram creator for new posts and stories.

    Returns how many new items were saved.
    """
    handle = (user.username or "").lstrip("@")
    if not handle:
        return 0

    # Confirm this is still the same account before touching it.
    #
    # Instagram handles are released and reclaimed, so a name that resolved to
    # one person when it was added can belong to someone else later. Checking
    # the stored account id catches that instead of quietly monitoring a
    # stranger under the old name. Only a positive contradiction stops the
    # scan: no stored id, or no answer, both carry on.
    if user.platform_uid:
        profile, _ = instagram_service.probe_creator(db, handle)
        if profile and profile.get("owner_id") and profile["owner_id"] != user.platform_uid:
            user.last_error = (
                f"@{handle} now belongs to a different Instagram account "
                f"({profile['owner_id']} rather than {user.platform_uid})."
            )
            user.status = UserStatus.error
            user.last_checked = datetime.now(timezone.utc)
            db.commit()
            log.warning("[@%s] account id changed (%s -> %s); not scanning",
                        handle, user.platform_uid, profile["owner_id"])
            events_service.emit(
                db, ActivityType.user_private, user.last_error,
                user_id=user.id, log_level=LogLevel.warning, log_source=LogSource.monitor,
            )
            return 0

    prev_status = user.status
    user.status = UserStatus.checking
    db.commit()
    publish("scan", {"phase": "start", "uid": user.id, "username": handle,
                     "platform": platforms.INSTAGRAM})

    max_posts = DEFAULT_POSTS_PER_SCAN
    try:
        max_posts = int(settings_service.get(db, "instagram_max_posts_per_scan"))
    except Exception:
        pass

    parts = []
    if not _posts_blocked():
        parts.append(("posts", ig_urls.profile_url(handle), max_posts))
    parts += [
        # Highlights are checked every sweep, not treated as optional. They are
        # cheap with the archive in place, they authenticate reliably, and while
        # Instagram is redirecting gallery-dl's posts extractor they are the
        # main way anything new is found at all. Without a part that can
        # actually succeed, a creator whose only phase is the broken one looks
        # unreachable and gets disabled.
        ("highlights", ig_urls.highlights_url(handle), 0),
        # Stories every sweep, and NOT behind the `monitor_stories` switch.
        #
        # That switch belongs to the TikTok sweep, which runs a headless browser
        # per creator to find stories and is genuinely expensive. Instagram's
        # cost one request with the archive behind it. Tying the two together
        # meant a TikTok preference silently decided whether Instagram caught
        # anything at all - and stories are the thing monitoring exists for,
        # because they are gone in 24 hours.
        ("stories", ig_urls.stories_url(handle), 0),
    ]

    found = 0
    failures: list[str] = []
    reached = False          # did any part complete cleanly?
    for name, url, cap in parts:
        try:
            result = instagram_service.download_url(
                db, url, source="monitor", limit=cap, use_archive=True)
        except Exception as exc:
            log.warning("[@%s] instagram %s scan failed: %s", handle, name, exc)
            failures.append(str(exc))
            continue
        if result.get("ok"):
            found += int(result.get("count") or 0)
            reached = True
        elif result.get("empty"):
            # Nothing new, but the request itself worked: proof the account is
            # reachable and the session is good.
            reached = True
        else:
            reason = str(result.get("error") or "scan failed")
            if name == "posts" and _note_posts_blocked(reason):
                # A site-wide outage on one endpoint says nothing about this
                # creator, so it is not held against them.
                continue
            failures.append(reason)

    user.last_checked = datetime.now(timezone.utc)

    # The streak only advances when NOTHING got through.
    #
    # One part failing while another succeeds is that endpoint's problem, not
    # the creator's - and it is the normal case right now, because Instagram
    # redirects gallery-dl's posts extractor to the home page for every
    # account. Counting that would have marked every monitored creator as
    # unreachable after three sweeps while stories and highlights were
    # downloading perfectly well.
    if failures and not found and not reached:
        user.scan_failures = (user.scan_failures or 0) + 1
        user.last_error = failures[0][:500]
        if user.scan_failures >= SCAN_FAILURE_THRESHOLD:
            user.status = UserStatus.error
        elif prev_status in (UserStatus.monitoring, UserStatus.private, UserStatus.active):
            user.status = prev_status   # transient - do not alarm yet
        else:
            user.status = UserStatus.monitoring
        db.commit()
        publish("scan", {"phase": "done", "uid": user.id, "username": handle,
                         "platform": platforms.INSTAGRAM, "new": 0, "failed": True})
        # Announced once, on the crossing, not on every retry.
        if user.scan_failures == SCAN_FAILURE_THRESHOLD:
            events_service.emit(
                db, ActivityType.user_private,
                f"@{handle} could not be checked on Instagram after "
                f"{SCAN_FAILURE_THRESHOLD} attempts",
                user_id=user.id, log_level=LogLevel.warning, log_source=LogSource.monitor,
            )
        return 0

    user.scan_failures = 0
    user.last_error = None
    user.status = UserStatus.monitoring
    if found:
        user.last_download = datetime.now(timezone.utc)
    db.commit()
    publish("scan", {"phase": "done", "uid": user.id, "username": handle,
                     "platform": platforms.INSTAGRAM, "new": found})

    if found:
        events_service.emit(
            db, ActivityType.scan_completed,
            f"@{handle}: {found} new Instagram item(s) saved",
            user_id=user.id, log_source=LogSource.monitor,
        )
        log.info("[@%s] %d new item(s)", handle, found)
    else:
        # A creator with nothing new is the ordinary case and does not need a
        # line of its own; the sweep's own summary already says how many were
        # checked.
        log.debug("[@%s] nothing new", handle)
    return found


def _scan_one_isolated(user_id: int) -> int:
    """One creator in its own session, so a failure cannot poison the sweep."""
    try:
        with session_scope() as db:
            user = db.get(models.User, user_id)
            if user is None or not user.enabled or not user.monitored or user.platform != platforms.INSTAGRAM:
                return 0
            if deferral_reason(db):
                return 0
            return scan_user(db, user)
    except Exception as exc:
        log.error("instagram scan of user %s failed: %s", user_id, exc)
        return 0


# The download queue is TikTok's, but its depth is the best available signal
# that the machine is already working hard. Set BELOW the TikTok threshold
# (monitor_service.SCAN_BACKLOG_PAUSE, 800) on purpose: Instagram stands aside
# while TikTok is merely busy, well before TikTok itself stops scanning.
BUSY_BACKLOG = 400


def deferral_reason(db: Session) -> str:
    """Why this sweep is standing aside, or '' when it will run.

    Instagram defers rather than queues. Blocking behind TikTok would make one
    platform's backlog into the other's outage; skipping a tick costs at most
    one interval, and the next one picks up exactly where this would have.
    """
    if not _enabled(db):
        return "Instagram monitoring is off."
    if is_paused(db):
        return "Monitoring is paused."
    from .cloud_sync_service import sync
    if not sync.can_automate(db):
        return "Background monitoring runs on another device."
    if not cookie_service.has_instagram_cookies():
        return "No Instagram cookies are set."
    try:
        from ..workers.manager import manager_instance

        if manager_instance.catch_up_running():
            return "Standing aside while the TikTok catch-up finishes."
    except Exception:
        pass
    try:
        counts = repo.Queue.counts(db)
        backlog = int(counts.get("pending", 0)) + int(counts.get("processing", 0))
        if backlog >= BUSY_BACKLOG:
            return f"Standing aside while {backlog} download(s) are queued."
    except Exception:
        pass
    return ""


def scan_all(db: Session) -> int:
    if not _sweep_lock.acquire(blocking=False):
        return 0
    try:
        return _scan_all(db)
    finally:
        _sweep_lock.release()


def _scan_all(db: Session) -> int:
    """Sweep the Instagram creators that are due for a check."""
    reason = deferral_reason(db)
    if reason:
        # Debug, not info: the usual reason is "the feature is off", and saying
        # so every 15 minutes forever would be noise.
        log.debug("instagram sweep deferred: %s", reason)
        return 0

    interval = int(settings_service.get(db, "instagram_scan_interval"))
    concurrency = max(1, min(SCAN_CONCURRENCY_MAX,
                             int(settings_service.get(db, "instagram_scan_concurrency"))))
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=interval)
    due = repo.Users.due_for_scan(db, cutoff, platform=platforms.INSTAGRAM)
    _note_run()
    if not due:
        log.debug("instagram sweep: nothing due")
        return 0

    # Ids captured up front: the isolated scans own their sessions and must not
    # share these ORM objects across threads.
    due_ids = [u.id for u in due]
    total = 0

    if concurrency > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="ig-scan") as pool:
            for n in pool.map(_scan_one_isolated, due_ids):
                total += n
        log.info("instagram: scanned %d due creator(s) at concurrency=%d",
                 len(due_ids), concurrency)
        _note_run()
        return total

    # Sequential sweep, bounded to this tick so a long roster rolls forward
    # instead of overrunning the next one.
    deadline = time.monotonic() + max(30, interval)
    scanned = 0
    for uid in due_ids:
        if scanned and time.monotonic() >= deadline:
            break
        with session_scope() as check:
            if is_paused(check):
                log.info("instagram sweep stopped early: monitoring was paused")
                break
        if scanned:
            time.sleep(settings_service.spacing_delay(db))
        total += _scan_one_isolated(uid)
        scanned += 1
    if scanned < len(due_ids):
        log.info("instagram: scanned %d/%d due creator(s) this tick; remainder next tick",
                 scanned, len(due_ids))
    elif scanned:
        log.info("instagram: scanned %d due creator(s)", scanned)
    _note_run()
    return total
