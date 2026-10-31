"""Monitoring service - scan an account, detect new posts, enqueue downloads."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .. import platforms, models, repositories as repo
from ..core.database import session_scope
from ..core.events import publish
from ..core.logging_config import get_logger
from ..models.enums import (
    ActivityType,
    LogLevel,
    LogSource,
    NotificationType,
    UserStatus,
)
from ..tiktok import listing, naming, resolver
from . import cookie_service, events_service, settings_service

log = get_logger("monitor")

# Consecutive failed scans before an account is shown as "error". TikTok
# rate-limits make a live account look unreachable now and then, so we ride out
# a few blips (keeping the last healthy status) before flagging it.
SCAN_FAILURE_THRESHOLD = 3

# While a light scan keeps failing, re-check via the headless browser at the
# first error crossing and then every Nth consecutive failure. TikTok's WAF
# gives yt-dlp + plain HTTP false "not found"s on live accounts, so this escalation
# recovers them - but the browser is expensive (one warm Chromium, serialized),
# so a genuinely-gone account must not trigger it on every single tick.
BROWSER_RECHECK_EVERY = 6


def _should_browser_recover(next_failures: int) -> bool:
    """True when a failing light scan should escalate to the browser this tick -
    as it first crosses the error threshold, then periodically while it stays
    down (so persistently-dead accounts don't reload the browser every tick)."""
    if next_failures == SCAN_FAILURE_THRESHOLD:
        return True
    return next_failures > SCAN_FAILURE_THRESHOLD and next_failures % BROWSER_RECHECK_EVERY == 0


# yt-dlp's wording when it cannot pull the secUid out of the profile page. It is
# the WAF challenge speaking, not a missing account - and the secUid is exactly
# what the headless browser can supply.
_SECUID_FAILURE = "secondary user id"


def _secuid_recovery_due(user: models.User, reason: str | None) -> bool:
    """True when a failure is the missing-secUid signature on a creator we have
    no secUid for.

    Worth escalating immediately rather than waiting for the usual three strikes:
    this specific error is the one the browser reliably fixes, and the secUid it
    returns is stored, so every later scan takes the fast WAF-resistant path.
    The cost is therefore one browser launch per creator, not one per tick.

    Having a stored secUid does NOT disqualify a creator here. That was the
    assumption before - "we already have the thing that fixes it" - and it was
    wrong: the error is the WAF refusing to serve the profile at all, which
    happens whether or not a secUid is on file. Every creator stranded in the
    error state had one, so the immediate escalation never fired for any of
    them and they accumulated failures for days behind the slow periodic
    retry. The per-creator cooldown below is what keeps this cheap, not the
    secUid check.
    """
    if not reason or _SECUID_FAILURE not in reason.lower():
        return False
    return _browser_secuid_due(user.id)


# Even so, a creator the browser also cannot reach must not relaunch it every
# tick, so the immediate escalation gets its own cooldown.
BROWSER_SECUID_COOLDOWN_SECS = 1800.0
_browser_secuid_last: dict[int, float] = {}


def _browser_secuid_due(user_id: int) -> bool:
    now = time.monotonic()
    with _browser_post_lock:
        if now - _browser_secuid_last.get(user_id, 0.0) >= BROWSER_SECUID_COOLDOWN_SECS:
            _browser_secuid_last[user_id] = now
            return True
    return False


# Private (followed) accounts and some WAF-blocked ones can't be listed by yt-dlp,
# so the light scan resolves them with ZERO videos and new posts go undetected.
# For those we re-list via the headless browser's signed post-list capture - but
# the browser is a single serialized Chromium, so each account is throttled to at
# most one browser post-capture per this many seconds (new-post latency for such
# accounts, in exchange for not swamping the browser every scan tick).
BROWSER_POST_COOLDOWN_SECS = 180.0
_browser_post_lock = threading.Lock()
_browser_post_last: dict[int, float] = {}


def _browser_post_due(user_id: int) -> bool:
    now = time.monotonic()
    with _browser_post_lock:
        if now - _browser_post_last.get(user_id, 0.0) >= BROWSER_POST_COOLDOWN_SECS:
            _browser_post_last[user_id] = now
            return True
    return False


# Avatars change rarely and a missing one is not worth a browser launch every
# scan tick, so the retry for it is on its own far longer cooldown than posts.
BROWSER_AVATAR_COOLDOWN_SECS = 6 * 3600.0
_browser_avatar_last: dict[int, float] = {}


def _browser_avatar_due(user_id: int) -> bool:
    now = time.monotonic()
    with _browser_post_lock:
        if now - _browser_avatar_last.get(user_id, 0.0) >= BROWSER_AVATAR_COOLDOWN_SECS:
            _browser_avatar_last[user_id] = now
            return True
    return False


def scan_user(db: Session, user: models.User, use_browser: bool = False) -> int:
    """Scan one user for new posts; enqueue any found. Returns new-post count.

    use_browser=True also refreshes avatar/followers via headless Chromium -
    used for manual scans; periodic scans skip it to stay light.
    """
    cookies = cookie_service.active_cookies_path(db)
    timeout = int(settings_service.get(db, "request_timeout"))
    max_posts = int(settings_service.get(db, "max_posts_per_scan"))

    handle = user.username
    if user.tiktok_uid:
        resolved = resolver.resolve_uid_to_username(user.tiktok_uid, cookies, timeout=timeout)
        if resolved and resolved != handle:
            log.info("[%s] handle changed -> @%s", user.state_key, resolved)
            handle = resolved
            user.username = resolved

    # Remember the last known status so a transient failure can keep the account
    # looking healthy instead of flashing "error" on a single blip.
    prev_status = user.status
    user.status = UserStatus.checking
    db.commit()
    publish("scan", {"phase": "start", "uid": user.id, "username": user.username})

    result = listing.fetch_account(
        handle, cookies, max_posts=max_posts, timeout=timeout,
        use_browser=use_browser, sec_uid=user.sec_uid or "",
    )
    user.last_checked = datetime.now(timezone.utc)

    if not result.ok and (
        use_browser
        or _should_browser_recover((user.scan_failures or 0) + 1)
        or _secuid_recovery_due(user, result.error)
    ):
        # The light periodic scan uses only yt-dlp + plain HTTP, both of which
        # TikTok's WAF now serves challenge pages to for some *live* accounts -
        # so a perfectly reachable creator gets a false "not found". Before
        # crossing into an error state, escalate to the headless browser (the
        # WAF-proof path):
        #   1. Re-fetch the current handle via the browser - recovers accounts
        #      that are alive but were blocked on the light path. (A manual scan
        #      already used the browser above, so it skips straight to step 2.)
        #   2. If it still won't resolve, the creator may have RENAMED: resolve
        #      the stored uid → current handle (browser) and fetch under it.
        # A genuinely gone account (deleted / banned) resolves at neither step
        # and falls through to the error handling unchanged. For light scans the
        # escalation is rate-limited by `_should_browser_recover` so a
        # persistently-dead account doesn't load the browser on every tick.
        if not use_browser:
            # Pass the stored secUid here too. The light call above supplies it,
            # so dropping it on the retry threw away the one identifier that
            # reaches a creator whose handle the WAF will not resolve.
            result = listing.fetch_account(
                handle, cookies, max_posts=max_posts, timeout=timeout,
                use_browser=True, sec_uid=user.sec_uid or "",
            )
        if not result.ok and user.tiktok_uid:
            renamed = resolver.resolve_uid_to_username(user.tiktok_uid, cookies, timeout=timeout, use_browser=True)
            if renamed and renamed.lower() != handle.lower():
                log.info("[%s] handle renamed @%s -> @%s (recovered via browser)", user.state_key, handle, renamed)
                handle = renamed
                user.username = renamed
                result = listing.fetch_account(handle, cookies, max_posts=max_posts, timeout=timeout, use_browser=True)

    if not result.ok:
        # A failed scan is NEVER a deletion - the account is kept on the monitor
        # list and retried. TikTok's rate limits / WAF challenges routinely make a
        # perfectly live account momentarily look "not found" (both the profile
        # fetch and yt-dlp come back empty under load), so we require several
        # CONSECUTIVE failures before surfacing an "error" status. A one-off blip
        # keeps the previous healthy status; the counter resets on the next
        # success. (The old code soft-deleted on the first `not_found`, silently
        # dropping live creators during a bulk scan sweep.)
        # TikTok itself said the account is gone, which no amount of retrying
        # undoes. Stop scanning it: these creators were re-scanned on every
        # sweep forever, each costing a browser launch, which is a large part of
        # why a sweep took so long.
        #
        # It is disabled rather than marked `deleted`. In this app `deleted`
        # means "removed by the user" and is filtered out of the monitor list
        # entirely - a creator would silently vanish, which looks like data loss
        # rather than an answer. Disabling stops the scans, keeps the row on the
        # page showing exactly why, and leaves Resume available if the handle
        # ever comes back.
        if (result.error or "") == listing.ACCOUNT_GONE:
            # One reading is not proof.
            #
            # TikTok answers a throttled or challenged request with the same
            # "couldn't find this account" page it serves for a deleted one, so
            # a single verdict cannot tell a dead creator from a busy minute.
            # Acting on the first one disabled live accounts - and because a
            # disabled creator is excluded from future scans, nothing ever
            # re-checked them: the mistake was permanent and silent.
            #
            # So a gone verdict now only counts once the creator has already
            # failed SCAN_FAILURE_THRESHOLD consecutive scans. Those are spaced
            # by the scan interval, so confirmation spans minutes rather than
            # one unlucky request, and any success in between resets the count.
            failures = (user.scan_failures or 0) + 1
            user.scan_failures = failures
            if failures < SCAN_FAILURE_THRESHOLD:
                user.last_error = result.error
                if prev_status in (UserStatus.monitoring, UserStatus.private, UserStatus.active):
                    user.status = prev_status
                db.commit()
                log.info("[@%s] TikTok says the account is gone (%d/%d); "
                         "waiting for confirmation before acting",
                         user.username, failures, SCAN_FAILURE_THRESHOLD)
                publish("scan", {"phase": "done", "uid": user.id, "username": user.username,
                                 "new": 0, "failed": True})
                return 0

            user.enabled = False
            user.status = UserStatus.error
            user.last_error = listing.ACCOUNT_GONE
            db.commit()
            publish("scan", {"phase": "done", "uid": user.id, "username": user.username,
                             "new": 0, "failed": True})
            events_service.emit(
                db, ActivityType.user_private,
                f"@{user.username} no longer exists on TikTok; monitoring stopped",
                user_id=user.id, log_level=LogLevel.warning, log_source=LogSource.monitor,
            )
            return 0

        user.scan_failures = (user.scan_failures or 0) + 1
        # Record the actual reason (e.g. yt-dlp's "Unable to extract secondary
        # user ID" for a deleted/banned handle, or a timeout) rather than an
        # opaque generic string, so the UI is self-diagnosing.
        default = "Account not found / unreachable" if result.not_found else "Scan failed"
        user.last_error = result.error or default
        if user.scan_failures >= SCAN_FAILURE_THRESHOLD:
            user.status = UserStatus.error
        elif prev_status in (UserStatus.monitoring, UserStatus.private, UserStatus.active):
            user.status = prev_status  # transient - don't alarm the user yet
        else:
            user.status = UserStatus.monitoring
        db.commit()
        publish("scan", {"phase": "done", "uid": user.id, "username": user.username, "new": 0, "failed": True})
        # Only announce once, when the account first crosses into a real error
        # state - not on every transient retry (that spammed the activity feed).
        if user.scan_failures == SCAN_FAILURE_THRESHOLD:
            events_service.emit(
                db, ActivityType.user_private,
                f"@{user.username} could not be scanned after {SCAN_FAILURE_THRESHOLD} attempts",
                user_id=user.id, log_level=LogLevel.warning, log_source=LogSource.monitor,
            )
        return 0

    # Refresh metadata
    meta = result.metadata

    # Persist the secUid the moment we learn it - it unlocks the WAF-resistant /
    # private post listing (tiktokuser:<secUid>) on every FUTURE light scan, with
    # no headless browser needed.
    if meta.sec_uid and meta.sec_uid != (user.sec_uid or ""):
        user.sec_uid = meta.sec_uid

    # Detection bootstrap: private-followed and some WAF-blocked accounts can't be
    # listed by the normal @handle path, so the light scan comes back with 0
    # videos AND we don't yet know the secUid. One throttled headless-browser
    # fetch captures the secUid (WAF-proof metadata) - the re-list then flows
    # through tiktokuser:<secUid>. Once stored, subsequent scans skip the browser.
    if (not result.videos) and (not use_browser) and (not user.sec_uid):
        expects_posts = bool(meta.private) or (user.total_posts or 0) > 0 or (meta.total_posts or 0) > 0
        if expects_posts and _browser_post_due(user.id):
            browsed = listing.fetch_account(
                handle, cookies, max_posts=max_posts, timeout=timeout, use_browser=True,
            )
            if browsed.ok:
                result = browsed
                meta = result.metadata
                if meta.sec_uid:
                    user.sec_uid = meta.sec_uid

    if meta.nickname:
        user.nickname = meta.nickname
    if meta.avatar_url:
        user.avatar_url = meta.avatar_url

    # The escalation above only triggers for a creator whose *listing* fails. One
    # whose posts list fine but whose profile is WAF-gated keeps returning
    # metadata with no avatar, so nothing above ever fills it in and the row
    # shows no picture indefinitely. fetch_profile through the browser is the
    # only source that reliably gets one, and it is much cheaper than a browsed
    # listing, so retry it on its own long cooldown while the picture is missing.
    if not user.avatar_url and not use_browser and _browser_avatar_due(user.id):
        try:
            profile = listing.fetch_profile(handle, cookies, timeout=timeout, use_browser=True)
        except Exception as exc:
            log.debug("[@%s] avatar refresh failed: %s", handle, exc)
            profile = None
        if profile:
            if profile.avatar_url:
                user.avatar_url = profile.avatar_url
            if profile.sec_uid and not user.sec_uid:
                user.sec_uid = profile.sec_uid
    # meta.total_posts is the profile videoCount when reachable, else just the
    # recent-cap from yt-dlp - never let a scan shrink the known total.
    if meta.total_posts:
        user.total_posts = max(user.total_posts or 0, meta.total_posts)
    if meta.followers is not None:
        user.followers = meta.followers

    # Only ever fill a blank one. This used to be reassigned on every scan, which
    # quietly defeated the pinning that create_adhoc and _promote_existing both
    # go out of their way to preserve: a creator first saved from a Telegram link
    # has no profile metadata, so its folder is "username - (@username)", and the
    # first scan after it was added to monitoring learned the real nickname and
    # renamed it to "Nickname - (@username)". Nothing moved the files, so the
    # creator ended up with two folders and a library pointing at the empty one.
    if not user.folder_name:
        user.folder_name = naming.account_folder_name(user.nickname, user.username)

    seen = repo.Posts.seen_ids(db, user.id)
    new_videos = [v for v in result.videos if v["id"] not in seen]

    for v in new_videos:
        repo.Posts.add(db, user.id, v["id"], v["url"], v.get("title", ""), downloaded=False)
        repo.Queue.enqueue(
            db,
            user_id=user.id,
            username=user.username,
            video_id=v["id"],
            url=v["url"],
            title=v.get("title", ""),
            source="monitor",
        )

    # Preserve a known-private status when a light secUid scan can't re-confirm it
    # (the tiktokuser listing doesn't report the private flag), avoiding a
    # private↔monitoring flicker every tick. meta.nickname is empty on that path.
    if meta.private:
        user.status = UserStatus.private
    elif prev_status == UserStatus.private and not meta.nickname:
        user.status = UserStatus.private
    else:
        user.status = UserStatus.monitoring
    user.last_error = None
    user.scan_failures = 0  # healthy again - clear the transient-failure streak
    db.commit()
    publish("scan", {"phase": "done", "uid": user.id, "username": user.username, "new": len(new_videos)})

    if new_videos:
        events_service.emit(
            db,
            ActivityType.scan_completed,
            f"@{user.username}: {len(new_videos)} new post(s) queued",
            meta={"count": len(new_videos)},
            user_id=user.id,
            log_source=LogSource.monitor,
            notify=(NotificationType.info, "New posts detected", f"@{user.username}: {len(new_videos)} new"),
        )
        log.info("[%s] %d new post(s) queued", user.username, len(new_videos))

    # Trigger a story check for this creator alongside the post scan (opt-in), but
    # DISPATCH it to a background worker so the serialised story browser never
    # slows the post sweep. Throttled per creator to `story_scan_interval` (shared
    # with the periodic sweep); lower it toward the scan interval for fresher
    # stories. Stories arrive shortly after the post scan rather than blocking it.
    if settings_service.get(db, "monitor_stories") and cookie_service.profile_is_authenticated(cookies):
        from . import story_service

        story_service.dispatch_story_check(user.id, float(settings_service.get(db, "story_scan_interval")))

    return len(new_videos)


DEFAULT_AVG_VIDEO_BYTES = 3_500_000  # ~3.5 MB fallback when no history exists


def _all_posts_timeout(db: Session) -> int:
    base = int(settings_service.get(db, "request_timeout"))
    return max(90, base * 6)  # listing the whole catalogue needs longer


def _already_downloaded_ids(db: Session, user: models.User) -> set[str]:
    """Posts we already have: completed DB downloads UNION media present on disk
    (so files saved outside the app, or before a DB reset, aren't re-downloaded)."""
    from . import download_service

    on_disk = download_service.downloaded_ids_on_disk(
        download_service.account_dir(db, user, user.username, user.nickname)
    )
    return repo.Downloads.completed_video_ids(db, user.id) | on_disk


def _fetch_full_catalogue(db: Session, user: models.User) -> list[dict]:
    """Every post for a creator, for the user-initiated "download all" flow.

    Passes the stored secUid and permits the headless-browser profile fetch, so a
    WAF-blocked or private-followed account still enumerates (the plain @handle
    listing returns nothing for those, which made bulk download report 0 posts).
    A newly discovered secUid is persisted for future scans.
    """
    if user.platform != platforms.TIKTOK:
        raise ValueError("TikTok catalogue lookup cannot be used for another platform.")
    cookies = cookie_service.active_cookies_path(db)
    videos, sec_uid = listing.fetch_all_detailed(
        user.username, cookies, timeout=_all_posts_timeout(db),
        sec_uid=user.sec_uid or "", use_browser=True,
    )
    if sec_uid and sec_uid != (user.sec_uid or ""):
        user.sec_uid = sec_uid
        db.commit()
    return videos


def estimate_all_posts(db: Session, user: models.User) -> dict:
    """Fetch the full post list and estimate how much is left to download."""
    videos = _fetch_full_catalogue(db, user)
    total = len(videos)
    # "Already downloaded" means an actual saved file - a completed Download
    # record OR a media file on disk - NOT merely a baselined/seen post.
    downloaded = _already_downloaded_ids(db, user)
    to_download = [v for v in videos if v["id"] not in downloaded]
    avg = repo.Downloads.avg_size(db) or DEFAULT_AVG_VIDEO_BYTES
    est = len(to_download) * avg
    return {
        "total_posts": total,
        "already_downloaded": total - len(to_download),
        "to_download": len(to_download),
        "estimated_size": est,
        "avg_size": avg,
    }


def download_all_posts(db: Session, user: models.User) -> dict:
    """Enqueue every not-yet-downloaded post for the account."""
    videos = _fetch_full_catalogue(db, user)

    downloaded = _already_downloaded_ids(db, user)
    active = repo.Queue.active_video_ids(db, user.id)
    seen = repo.Posts.seen_ids(db, user.id)

    queued = 0
    for v in videos:
        vid = v["id"]
        if vid not in seen:
            repo.Posts.add(db, user.id, vid, v["url"], v.get("title", ""), downloaded=False)
        if vid in downloaded or vid in active:
            continue
        repo.Queue.enqueue(
            db, user_id=user.id, username=user.username, video_id=vid,
            url=v["url"], title=v.get("title", ""), source="bulk",
        )
        active.add(vid)
        queued += 1

    db.commit()
    if queued:
        events_service.emit(
            db, ActivityType.scan_completed,
            f"@{user.username}: queued {queued} post(s) for full download",
            meta={"count": queued, "total": len(videos)},
            user_id=user.id, log_source=LogSource.monitor,
            notify=(NotificationType.info, "Download All started", f"@{user.username}: {queued} post(s) queued"),
        )
    return {"queued": queued, "total": len(videos)}


# While this many download jobs are queued, the periodic scan is deferred. A full
# sweep fetches every monitored profile (headless browser + yt-dlp listing)
# straight from TikTok, so running it during a bulk download piles a second wave
# of requests onto the same IP and is a fast route to a temporary block.
#
# The ceiling has to clear a *normal* busy period, not just a quiet one. At 50 it
# did not: importing a roster or a single catch-up puts hundreds of jobs in the
# queue, so the guard latched on and monitoring stopped running for hours at a
# time - which looks exactly like monitoring being broken. This is meant to stop
# a scan piling on during a genuine flood, so it sits well above what routine
# work produces.
# Only the post scan is gated this way; the story sweep checks nothing but
# `monitoring_paused`, which is right - stories expire within a day, so a skipped
# sweep loses them for good.
SCAN_BACKLOG_PAUSE = 800

# Seconds to wait between consecutive per-account scans when running the gentle
# sequential sweep (scan_concurrency=1). TikTok rate-limits bursts hard (a freshly
# -imported batch all comes due at once) and a rate-limited fetch looks like a
# scan failure, so a small gap spaces the requests. Ignored when concurrency > 1
# (the concurrency limit is the throttle there).
SCAN_SPACING_SECS = 2.0

# Hard ceiling on parallel scans, matching the SettingsUpdate schema bound.
SCAN_CONCURRENCY_MAX = 20


def _scan_one_isolated(user_id: int) -> int:
    """Scan a single creator in its own DB session so it is safe to run inside
    the parallel scan pool (SQLAlchemy sessions are not shared across threads).
    Returns the new-post count and never raises."""
    try:
        with session_scope() as s:
            # Checked per creator: a pool sweep is dispatched all at once, so
            # without this every queued creator is still scanned after a pause.
            if is_paused(s):
                return 0
            u = repo.Users.get(s, user_id)
            if not u:
                return 0
            return scan_user(s, u)
    except Exception as exc:  # pragma: no cover
        log.error("[uid=%s] scan error: %s", user_id, exc)
        return 0


_last_defer_notice = 0.0
_DEFER_NOTICE_SECS = 900.0  # at most one "scanning on hold" notice every 15 min


def is_paused(db: Session) -> bool:
    """Whether monitoring is on hold, right now.

    Read INSIDE every sweep loop, not just before one starts. A sweep over a
    large roster runs for minutes, so a pause that is only checked up front
    stops the next sweep while the current one carries on scanning to the end -
    which looks exactly like the pause doing nothing.
    """
    try:
        return bool(settings_service.get(db, "monitoring_paused"))
    except Exception:
        # A failed read must not strand a sweep; carrying on is the safe answer.
        return False


def scan_deferral_reason(db: Session) -> str:
    """Why the periodic scan is currently skipping every tick, or '' if it will
    run. Single source of truth for both the scan loop and the status endpoint -
    the deferral used to be logged only to stdout, so the UI kept showing a
    "next scan" countdown while nothing had been scanned for hours."""
    if settings_service.get(db, "monitoring_paused"):
        return "Monitoring is paused."
    counts = repo.Queue.counts(db)
    backlog = int(counts.get("pending", 0)) + int(counts.get("processing", 0))
    if backlog >= SCAN_BACKLOG_PAUSE:
        return (
            f"Scanning is on hold while {backlog} download(s) are queued. It resumes "
            f"automatically once the backlog drops below {SCAN_BACKLOG_PAUSE}."
        )
    return ""


def _notice_deferred(db: Session, reason: str) -> None:
    """Record the hold in the activity/log feed, throttled so a long bulk
    download doesn't spam it every tick."""
    global _last_defer_notice
    now = time.monotonic()
    if now - _last_defer_notice < _DEFER_NOTICE_SECS:
        return
    _last_defer_notice = now
    try:
        events_service.emit(
            db, ActivityType.scan_completed, f"Scanning deferred: {reason}",
            log_level=LogLevel.warning, log_source=LogSource.monitor,
        )
    except Exception as exc:  # never let feedback break the scheduler
        log.debug("defer notice failed: %s", exc)


def scan_all(db: Session) -> int:
    """Scan the accounts that are due for a new-post check.

    `scan_concurrency` controls how many creators are checked at once:
      * 1  → a gentle, spaced sequential sweep bounded to this tick's time budget
             (leftovers roll to the next tick).
      * >1 → a thread pool that checks up to `scan_concurrency` creators
             simultaneously, so even a large roster finishes well within one
             interval. Faster, but heavier request bursts (TikTok limits by IP).
    Every per-account scan runs in its own session, so one failure can never
    poison another.
    """
    reason = scan_deferral_reason(db)
    if reason:
        log.info("scan deferred: %s", reason)
        # Surface it: this used to be a stdout-only log, so a long backlog could
        # starve monitoring for hours while the UI still showed a countdown.
        if not settings_service.get(db, "monitoring_paused"):
            _notice_deferred(db, reason)
        return 0

    interval = int(settings_service.get(db, "scan_interval"))
    concurrency = max(1, min(SCAN_CONCURRENCY_MAX, int(settings_service.get(db, "scan_concurrency"))))
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=interval)
    # TikTok only. Instagram creators are swept by instagram_monitor_service,
    # which talks to an entirely different site.
    due = repo.Users.due_for_scan(db, cutoff, platform=platforms.TIKTOK)
    if not due:
        return 0
    # Capture ids up-front - the isolated scans run in their own sessions and
    # must not share these ORM objects across threads.
    due_ids = [u.id for u in due]

    # ── Parallel sweep ──
    if concurrency > 1:
        total_new = 0
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="scan") as pool:
            for n in pool.map(_scan_one_isolated, due_ids):
                total_new += n
        log.info("scanned %d due account(s) at concurrency=%d", len(due_ids), concurrency)
        return total_new

    # ── Sequential sweep (gentle default) ──
    deadline = time.monotonic() + max(15, interval)  # this tick's budget
    total_new = 0
    scanned = 0
    for uid in due_ids:
        if is_paused(db):
            log.info("scan sweep stopped early: monitoring was paused")
            break
        if scanned > 0 and time.monotonic() >= deadline:
            break  # out of budget - the rest are picked up next tick
        if scanned > 0:
            # Gentle, jittered spacing (configurable via scan_spacing_min/max) to
            # avoid tripping rate limits. Falls back to SCAN_SPACING_SECS defaults.
            time.sleep(settings_service.spacing_delay(db))
        total_new += _scan_one_isolated(uid)
        scanned += 1
    if scanned < len(due_ids):
        log.info("scanned %d/%d due account(s) this tick; remainder next tick", scanned, len(due_ids))
    return total_new
