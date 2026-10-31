"""User monitoring service - add/preview/import/delete + serialisation."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, repositories as repo
from ..core.events import publish
from ..core.logging_config import get_logger
from ..models.enums import (
    ActivityType,
    DownloadStatus,
    JobStatus,
    LogSource,
    NotificationType,
    UserStatus,
)
from ..schemas import UserOut, UserPreview
from ..tiktok import listing, naming, oembed, resolver
from . import cookie_service, events_service

log = get_logger("user_service")


def adhoc_for_link(
    db: Session, url: str, handle: str, source: str, post: oembed.Post | None = None
) -> tuple[models.User | None, oembed.Post]:
    """Creator for a one-off link download, plus that post's title and cover.

    A creator we already know costs nothing but a primary-key lookup. An unknown
    one is built from the post's own oEmbed record, which carries the author's
    handle and display name - and therefore produces exactly the same folder
    name the full profile fetch would have.

    `add()` is deliberately not used here. It resolves a creator properly for
    monitoring: a headless-browser profile fetch plus a full catalogue listing,
    measured together at ~55s. Downloading one video needs none of that, and
    paying it inline was the delay between sending a link and anything
    happening. A creator promoted to monitoring later still goes through `add()`,
    which is when that data is actually needed.
    """
    post = post or oembed.Post()
    user = repo.Users.by_username(db, handle, platform="tiktok") if handle else None
    if user:
        return user, post

    if not post.ok:
        post = oembed.lookup(url)
    username = handle or post.username
    if not username:
        return None, post

    # The handle from the URL and the one oEmbed reports can differ in case.
    user = repo.Users.by_username(db, username, platform="tiktok")
    if user:
        return user, post

    return create_adhoc(db, username, post.nickname, source), post


def create_adhoc(db: Session, username: str, nickname: str, source: str) -> models.User:
    """Record a creator for a one-off download: not monitored, not scanned, and
    resolved without any network call. `folder_name` is pinned now so a later
    promotion to monitoring can enrich the row without moving already-downloaded
    files into a differently-named folder."""
    display = nickname or username
    return repo.Users.create(
        db,
        state_key=f"@{username}",
        username=username,
        nickname=display,
        status=UserStatus.monitoring,
        monitored=False,
        folder_name=naming.account_folder_name(display, username),
        last_checked=datetime.now(timezone.utc),
    )


def normalise(raw: str) -> tuple[str, str, str | None]:
    """Return (state_key, username_handle, uid|None) for a raw input."""
    raw = raw.strip()
    if resolver.is_uid(raw):
        return f"uid:{raw}", raw, raw
    m = re.search(r"tiktok\.com/@([^/?#]+)", raw)
    handle = m.group(1) if m else raw.lstrip("@").strip()
    return f"@{handle}", handle, None


def resolve_account(handle: str, cookies, max_posts: int, timeout: int):
    """Look up an account, cheaply first.

    Both adding and previewing used to go straight to `use_browser=True`, which
    launches a headless Chromium every time - the single slowest thing either
    operation does, and unnecessary for the great majority of accounts that the
    plain HTTP path resolves in a couple of seconds. The browser is still the
    fallback, because it is the only path that beats TikTok's WAF, so blocked
    accounts behave exactly as before; they just no longer set the pace for
    everyone else.
    """
    result = listing.fetch_account(
        handle, cookies, max_posts=max_posts, timeout=timeout, use_browser=False
    )
    # "Resolved" means we came away with an identity we can monitor by. Without a
    # secUid or numeric id the row would be a name and nothing else, which is the
    # case the browser exists for.
    meta = result.metadata
    if result.ok and (meta.sec_uid or meta.account_id):
        return result

    log.info("[@%s] light lookup insufficient, escalating to browser", handle)
    browsed = listing.fetch_account(
        handle, cookies, max_posts=max_posts, timeout=max(timeout, 30), use_browser=True
    )
    return browsed if browsed.ok else result


def preview(db: Session, raw_username: str) -> UserPreview:
    """Validate + fetch a profile preview without persisting anything."""
    cookies = cookie_service.active_cookies_path(db)
    _, handle, uid = normalise(raw_username)

    if uid:
        resolved = resolver.resolve_uid_to_username(uid, cookies)
        if resolved:
            handle = resolved

    result = resolve_account(handle, cookies, max_posts=6, timeout=cfg_timeout(db))
    meta = result.metadata
    if result.not_found:
        return UserPreview(username=handle, nickname=handle, exists=False)

    # Only an authoritative count is reported. This used to fall back to walking
    # the entire catalogue - a 120-second listing to fill in one number on a
    # preview - and, failing that, to the 6-post recent cap, which was simply
    # wrong. 0 means "not known yet"; the first scan fills it in.
    total_posts = meta.total_posts if (meta.followers is not None and meta.total_posts) else 0

    return UserPreview(
        username=meta.username or handle,
        nickname=meta.nickname or handle,
        avatar_url=meta.avatar_url,
        total_posts=total_posts,
        followers=meta.followers,
        tiktok_uid=meta.account_id or uid,
        exists=True,
    )


def needs_enrichment(user: models.User) -> bool:
    """True for a creator recorded from a single link: it has a name and folder
    but no TikTok identity, which the scanner needs in order to monitor it."""
    return not (user.sec_uid or user.tiktok_uid)


def _promote_existing(
    db: Session, existing: models.User, monitored: bool, source: str, meta=None
) -> models.User:
    """Reactivate / promote an already-known creator instead of inserting a
    duplicate. Covers deleted→monitoring and ad-hoc(monitored=False)→monitored.

    `meta`, when supplied, backfills the identity fields an ad-hoc row created
    from a link download never had. `folder_name` is left alone on purpose:
    files are already filed under it."""
    if meta is not None:
        existing.tiktok_uid = existing.tiktok_uid or meta.account_id
        existing.sec_uid = existing.sec_uid or meta.sec_uid or None
        # Prefer the fresh one rather than keeping whatever is stored: avatar URLs
        # are signed and expire, so an old value is the more likely of the two to
        # be dead, and "or" would have pinned the first one ever captured forever.
        existing.avatar_url = meta.avatar_url or existing.avatar_url
        if meta.nickname:
            existing.nickname = meta.nickname
        if meta.followers:
            existing.followers = meta.followers

    was_unmonitored = not existing.monitored
    if existing.status == UserStatus.deleted:
        existing.status = UserStatus.monitoring
        existing.enabled = True
    if monitored and not existing.monitored:
        existing.monitored = True
    db.commit()
    if monitored and was_unmonitored:
        events_service.emit(
            db,
            ActivityType.user_added,
            f"Added monitor account @{existing.username}",
            meta={"user_id": existing.id, "source": source},
            user_id=existing.id,
            log_source=LogSource.monitor,
            notify=(NotificationType.success, "User added", f"@{existing.username} is now monitored"),
        )
        log.info("Promoted @%s to monitoring (state_key=%s)", existing.username, existing.state_key)
    return existing


def add(
    db: Session,
    raw_username: str,
    *,
    source: str = "manual",
    baseline: bool = True,
    monitored: bool = True,
    resolve_total: bool = False,
) -> models.User:
    """Add a creator. Idempotent on identity (state_key OR resolved uid/username).

    When `baseline` is True the current posts are recorded as 'seen' so only
    posts published after this moment are downloaded (mirrors monitor.py).

    `monitored=False` records an ad-hoc creator (e.g. a one-off Telegram
    download) that is kept off the monitoring list and scanner. Calling again
    with `monitored=True` promotes it.

    `resolve_total=True` opts into the full-catalogue listing purely to get an
    exact post count. It defaults off: it can take a couple of minutes on a large
    account, it made adding a creator feel broken, and the first scan corrects
    the number anyway (a scan never lets the total shrink).
    """
    cookies = cookie_service.active_cookies_path(db)
    state_key, handle, uid = normalise(raw_username)

    existing = repo.Users.by_state_key(db, state_key)
    if existing and not (monitored and needs_enrichment(existing)):
        return _promote_existing(db, existing, monitored, source)
    # Falling through: a link-download creator is being promoted to monitoring,
    # so resolve it properly now and backfill below.

    if uid:
        resolved = resolver.resolve_uid_to_username(uid, cookies)
        if resolved:
            handle = resolved

    result = resolve_account(handle, cookies, max_posts=cfg_max_posts(db), timeout=cfg_timeout(db))
    meta = result.metadata
    if not uid and meta.account_id:
        uid = meta.account_id
        state_key = f"uid:{uid}"

    # Re-check identity AFTER resolving the account. The first lookup used the raw
    # "@handle" key, but the creator may already be stored under its "uid:<id>"
    # key or a differently-cased username - e.g. an ad-hoc creator created by a
    # Telegram link download. Promoting it here avoids a duplicate INSERT, which
    # the unique state_key constraint rejects: that error is what made "/user"
    # report failure and what poisoned the session mid bulk-import (dropping every
    # creator after the first duplicate).
    existing = repo.Users.by_state_key(db, state_key)
    if not existing:
        existing = repo.Users.by_username(db, meta.username or handle, platform="tiktok")
    if existing:
        return _promote_existing(db, existing, monitored, source, meta=meta)

    # Accurate total: the profile videoCount is WAF-gated, so derive the real
    # count from the full (working) yt-dlp listing rather than the recent-cap.
    total_posts = (
        _accurate_total_posts(handle, cookies, meta, len(result.videos))
        if resolve_total
        else max(meta.total_posts or 0, len(result.videos))
    )

    try:
        user = repo.Users.create(
            db,
            state_key=state_key,
            username=meta.username or handle,
            nickname=meta.nickname or handle,
            tiktok_uid=uid or meta.account_id,
            sec_uid=meta.sec_uid or None,
            avatar_url=meta.avatar_url,
            total_posts=total_posts,
            followers=meta.followers,
            status=UserStatus.monitoring,
            monitored=monitored,
            folder_name=naming.account_folder_name(meta.nickname or handle, meta.username or handle),
            last_checked=datetime.now(timezone.utc),
        )

        if baseline:
            # Record current posts as SEEN (so the monitor only auto-downloads posts
            # published after this moment) - but NOT as downloaded; no file exists yet.
            # "Download All Posts" relies on actual Download records, so these remain
            # fully downloadable.
            for v in result.videos:
                if not repo.Posts.exists(db, user.id, v["id"]):
                    repo.Posts.add(db, user.id, v["id"], v["url"], v.get("title", ""), downloaded=False)

        db.commit()
    except IntegrityError:
        # A concurrent add (or a duplicate within the same batch) already inserted
        # this exact creator. Clear the poisoned transaction and promote the
        # winning row instead of failing.
        db.rollback()
        existing = repo.Users.by_state_key(db, state_key) or repo.Users.by_username(db, meta.username or handle, platform="tiktok")
        if existing:
            return _promote_existing(db, existing, monitored, source)
        raise

    events_service.emit(
        db,
        ActivityType.user_added,
        (f"Added monitor account @{user.username}" if monitored else f"Downloaded @{user.username} via Telegram"),
        meta={"user_id": user.id, "source": source},
        user_id=user.id,
        log_source=LogSource.monitor,
        notify=(NotificationType.success, "User added", f"@{user.username} is now monitored") if monitored else None,
    )
    log.info("Added user @%s (state_key=%s, monitored=%s, %d baseline posts)", user.username, state_key, monitored, len(result.videos))
    return user


def bulk_import(db: Session, usernames: list[str]) -> dict:
    added, skipped, failed = [], [], []
    seen: set[str] = set()  # in-batch dedup so a repeated handle isn't fetched twice
    for raw in usernames:
        raw = raw.strip()
        if not raw:
            continue
        state_key, handle, _ = normalise(raw)
        if state_key in seen:
            skipped.append(raw)
            continue
        if repo.Users.by_state_key(db, state_key) or repo.Users.by_username(db, handle, platform="tiktok"):
            seen.add(state_key)
            skipped.append(raw)
            continue
        try:
            # resolve_total=False keeps each import light (no full-catalogue
            # listing) so a large paste finishes quickly and trips fewer rate
            # limits; the periodic scan refines the post count afterwards.
            user = add(db, raw, source="import", resolve_total=False)
            seen.add(state_key)
            if user.tiktok_uid:
                seen.add(f"uid:{user.tiktok_uid}")
            added.append(user.username)
        except Exception as exc:  # pragma: no cover
            # Roll back so a failed insert (e.g. IntegrityError) doesn't leave the
            # session in a pending-rollback state that fails every remaining
            # import - the bug that capped a 54-creator import at the first
            # duplicate.
            db.rollback()
            log.warning("import failed for %s: %s", raw, exc)
            failed.append(raw)
    return {"added": added, "skipped": skipped, "failed": failed}


def delete(db: Session, user_id: int) -> bool:
    user = repo.Users.get(db, user_id)
    if not user:
        return False
    username = user.username
    # Stop any downloads for this creator FIRST (same transaction as the delete):
    # remove not-yet-started jobs and flag the in-flight one cancelled so the
    # worker aborts it mid-download. Without this, deleting a creator left their
    # queued jobs behind (queue_jobs use ON DELETE SET NULL, so they'd just
    # detach and keep downloading). Committing both together means a worker can't
    # claim a pending job in the gap.
    removed, cancelled = repo.Queue.cancel_for_user(db, user_id)
    # Hard delete - the account is fully removed, not just disabled. The ORM
    # cascade ("all, delete-orphan") removes its posts + downloads; queue_jobs
    # and activity_events use ON DELETE SET NULL, so they detach cleanly.
    # (Media already written to disk is left in place.)
    db.delete(user)
    db.commit()
    if removed or cancelled:
        publish("queue_job", {"bulk": True, "status": "cancelled"})
        log.info("Removed @%s: cleared %d queued + cancelled %d in-flight download(s)", username, removed, cancelled)
    events_service.emit(
        db, ActivityType.user_removed, f"Removed monitor account @{username}",
        user_id=None, log_source=LogSource.monitor,
    )
    return True


def _accurate_total_posts(handle: str, cookies, meta, recent_count: int) -> int:
    """Best available total-post count.

    If the profile/browser gave authoritative data (followers known ⇒ the
    rehydration videoCount is real), trust it and skip the slow full listing.
    Otherwise count the full yt-dlp listing, which works even when the profile
    page is blocked. Never returns less than what we've already seen.
    """
    if meta.followers is not None and meta.total_posts:
        return meta.total_posts
    full = 0
    try:
        full = len(listing.fetch_all(handle, cookies, timeout=120))
    except Exception as exc:  # pragma: no cover
        log.warning("full post count failed for @%s: %s", handle, exc)
    return max(meta.total_posts or 0, full, recent_count)


# ── serialisation ──
def to_out(db: Session, user: models.User) -> UserOut:
    return to_out_many(db, [user])[0]


def to_out_many(db: Session, users: Sequence[models.User]) -> list[UserOut]:
    """Serialise users with their downloaded/pending counts using two grouped
    queries instead of two per user (the list endpoint was N+1)."""
    ids = [u.id for u in users]
    if not ids:
        return []
    downloaded = dict(
        db.execute(
            select(models.Download.user_id, func.count(models.Download.id))
            .where(
                models.Download.user_id.in_(ids),
                models.Download.status == DownloadStatus.completed,
            )
            .group_by(models.Download.user_id)
        ).all()
    )
    pending = dict(
        db.execute(
            select(models.QueueJob.user_id, func.count(models.QueueJob.id))
            .where(
                models.QueueJob.user_id.in_(ids),
                models.QueueJob.status.in_([JobStatus.pending, JobStatus.processing]),
            )
            .group_by(models.QueueJob.user_id)
        ).all()
    )
    out: list[UserOut] = []
    for u in users:
        o = UserOut.model_validate(u)
        o.downloaded_count = downloaded.get(u.id, 0)
        o.pending_count = pending.get(u.id, 0)
        out.append(o)
    return out


# ── small config helpers (live settings) ──
def cfg_timeout(db: Session) -> int:
    from . import settings_service

    return int(settings_service.get(db, "request_timeout"))


def cfg_max_posts(db: Session) -> int:
    from . import settings_service

    return int(settings_service.get(db, "max_posts_per_scan"))
