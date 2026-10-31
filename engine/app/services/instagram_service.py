"""Instagram download service - on-demand media fetch via gallery-dl.

Given an Instagram post/reel/profile URL, downloads the media using the user's
Instagram cookies and records it in the DB under an Instagram creator
(platform="instagram", monitored=False, so it never touches the TikTok scanner).
Media is stored under  <downloads>/Instagram/<creator>/  so it can't collide with
a TikTok creator of the same handle.
"""

from __future__ import annotations

import shutil
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, platforms, repositories as repo
from ..core import media_convert
from ..core.logging_config import get_logger
from ..models.enums import ActivityType, DownloadStatus, JobStatus, LogSource, NotificationType, PostType, UserStatus
from ..core.events import publish
from ..tiktok import naming
from ..instagram import downloader as ig_downloader, urls as ig_urls
from . import cookie_service, events_service, settings_service, storage_service

log = get_logger("instagram")

# Safety cap when someone points a whole PROFILE at us - don't fetch a 5000-post
# back-catalogue by accident. A specific post/reel URL is never capped.
DEFAULT_PROFILE_LIMIT = 30
IG_SLEEP_SECS = 2.0


def _account_dir(db: Session, user: models.User) -> Path:
    # Same platform-folder naming as every other engine (single source of truth).
    d = (settings_service.downloads_path(db)
         / platforms.display_name(platforms.INSTAGRAM)
         / (user.folder_name or user.username))
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_or_create_creator(db: Session, username: str) -> models.User:
    """Public name for the creator lookup, used by the monitoring endpoint."""
    return _get_or_create_user(db, username)


def set_monitoring(db: Session, username: str, monitored: bool) -> models.User:
    """Shared monitoring action for the desktop app and Telegram creator card."""
    handle = (username or "").strip().lstrip("@").strip("/")
    if not handle:
        raise ValueError("A username is required.")
    user = find_creator(db, handle) or get_or_create_creator(db, handle)
    if monitored:
        profile, _ = probe_creator(db, handle)
        if profile:
            user.platform_uid = profile.get("owner_id") or user.platform_uid
            user.nickname = (profile.get("fullname") or "").strip() or user.nickname
            user.avatar_url = (profile.get("avatar") or "").strip() or user.avatar_url
        user.last_checked = None
        user.status = UserStatus.monitoring
        user.enabled = True
        user.scan_failures = 0
        user.last_error = None
    user.monitored = bool(monitored)
    db.commit()
    return user


def _get_or_create_user(db: Session, username: str) -> models.User:
    state_key = f"ig:@{username.lower()}"
    user = db.scalar(select(models.User).where(models.User.state_key == state_key))
    if user:
        return user
    user = models.User(
        state_key=state_key,
        platform="instagram",
        username=username,
        nickname=username,
        status=UserStatus.active,
        monitored=False,       # never scanned - Instagram is download-only
        folder_name=naming.account_folder_name(username, username),
    )
    db.add(user)
    db.flush()
    log.info("created Instagram creator @%s", username)
    return user


class _Recorder:
    """Stores downloaded media one item at a time.

    Written as an object rather than a loop so the same code serves both a
    finished batch and a download that is still running: streaming records each
    file the moment gallery-dl finishes it, which is what lets a profile
    download start handing media back in seconds instead of after the whole
    account has been fetched.

    Per-creator state (the folder, the set of ids already stored) is looked up
    once and reused, so recording the two-hundredth item costs the same as the
    first.
    """

    def __init__(self, db: Session, source: str) -> None:
        self.db = db
        self.source = source
        self.total = 0
        self.files: list[dict] = []
        self.creators: set[str] = set()
        self._users: dict[str, tuple] = {}

    def _for(self, username: str) -> tuple:
        state = self._users.get(username)
        if state is None:
            user = _get_or_create_user(self.db, username)
            state = (user, _account_dir(self.db, user),
                     repo.Downloads.completed_video_ids(self.db, user.id))
            self._users[username] = state
        return state

    def _name(self, user, meta: dict) -> None:
        """Adopt the creator's real name once Instagram tells us it.

        `_get_or_create_user` seeds the nickname with the handle because that is
        all a filename carries. The metadata has the display name, so the first
        download from an account is what turns "nasa" into "NASA" everywhere it
        is shown, the profile card included.
        """
        full = str(meta.get("fullname") or meta.get("full_name") or "").strip()
        if full and user.nickname != full:
            user.nickname = full

    def add(self, it: ig_downloader.IGItem) -> dict | None:
        """Store one downloaded file. Returns what to hand back, or None."""
        user, acct, already = self._for(it.username)
        self._name(user, it.meta)
        self.creators.add(it.username)
        dest = acct / it.path.name
        post_type = PostType.video if it.is_video else PostType.image

        if it.video_id in already:
            # Already recorded - keep the existing file, drop the fresh temp copy.
            existing = self.db.scalar(
                select(models.Download).where(
                    models.Download.user_id == user.id,
                    models.Download.video_id == it.video_id,
                )
            )
            entry = {"path": str(dest), "post_type": post_type,
                     "video_id": it.video_id, "username": it.username,
                     "download_id": existing.id if existing else None,
                     "title": (existing.title if existing else "") or "",
                     "meta": it.meta}
            self.files.append(entry)
            return entry

        try:
            if dest.exists():
                dest.unlink()
            shutil.move(str(it.path), str(dest))
        except Exception as exc:
            log.warning("[@%s] could not store %s: %s", it.username, it.path.name, exc)
            return None

        # Before the row is written and before the file goes to MEGA, so the
        # recorded path, the size and the copy in the cloud all describe the
        # playable file rather than the one Instagram happened to serve.
        # Instagram hands out VP9 in .mp4 and .webp images, both of which are
        # valid files that most players and devices refuse to open.
        dest = media_convert.to_playable(dest, it.is_video)
        size = dest.stat().st_size if dest.exists() else 0
        title = str(it.meta.get("description") or it.meta.get("caption") or "").strip()
        record = repo.Downloads.add(
            self.db, user_id=user.id, video_id=it.video_id, title=title[:500],
            file_path=str(dest), file_size=size, post_type=post_type,
            strategy="gallery-dl", status=DownloadStatus.completed, source=self.source,
        )
        self.db.commit()
        # Instagram media is a finished download too; nothing about the platform
        # it came from should decide whether it reaches MEGA.
        storage_service.store_download(self.db, record, dest)
        already.add(it.video_id)
        user.last_download = datetime.now(timezone.utc)
        self.total += 1
        self.db.commit()

        # The download id travels with the file because `store_download` may
        # have just moved the bytes to MEGA and deleted the local copy. A caller
        # holding only `dest` would find nothing there.
        entry = {"path": str(dest), "post_type": post_type,
                 "video_id": it.video_id, "username": it.username,
                 "download_id": record.id, "title": title, "meta": it.meta}
        self.files.append(entry)
        return entry

    def result(self) -> dict:
        return {"count": self.total, "files": self.files,
                "creators": sorted(self.creators)}




def archive_path() -> Path:
    """Where gallery-dl records what it has already fetched.

    Beside the cookies, in the data directory, so it survives an app update and
    travels with the rest of the install's state.
    """
    from ..core.config import settings as _cfg
    return Path(_cfg.cookies_path).parent / "instagram-archive.sqlite3"


def _seed_archive(db: Session, archive: Path, url: str) -> None:
    query = select(models.Download).join(models.User).where(
        models.User.platform == platforms.INSTAGRAM,
        models.Download.status == DownloadStatus.completed)
    handle = ig_urls.extract_username(url)
    if handle:
        query = query.where(func.lower(models.User.username) == handle.lower())
    media_ids = [row.video_id.rsplit("_", 1)[-1] for row in db.scalars(query)
                 if row.remote_path or (row.file_path and Path(row.file_path).is_file())]
    ig_downloader.archive_media_ids(archive, media_ids)


def download_url(db: Session, url: str, *, source: str = "instagram",
                 limit: int | None = None, on_item=None,
                 use_archive: bool = False, check_stop=None) -> dict:
    """Download an Instagram post/reel/profile. Returns a result dict:
    {ok, count, files, creators} or {ok:False, error}."""
    url = (url or "").strip()
    if not ig_urls.is_instagram_url(url):
        return {"ok": False, "error": "That's not an Instagram URL."}
    if not cookie_service.has_instagram_cookies():
        return {"ok": False, "error": "No Instagram cookies set. Add them in Settings → Cookies."}

    cookies = cookie_service.instagram_cookies_path()
    archive = archive_path() if use_archive else None
    if archive:
        _seed_archive(db, archive, url)
    cap = limit if limit is not None else (DEFAULT_PROFILE_LIMIT if ig_urls.is_profile_url(url) else 0)
    try:
        sleep = float(settings_service.get(db, "instagram_sleep"))
    except Exception:
        sleep = IG_SLEEP_SECS

    tmp = Path(tempfile.mkdtemp(prefix="lora1-ig-"))
    try:
        # Always recorded item by item, never as a batch at the end.
        #
        # gallery-dl is a separate process, so storing a file the moment it
        # lands overlaps our work with its next download instead of queueing
        # behind the whole run. Measured on a highlights reel: 3m37s of
        # downloading followed by 75s of converting and uploading, with the two
        # never running at the same time. Interleaved, the second cost mostly
        # disappears into the first.
        #
        # `handle` is called on this thread by the poll loop, so the session is
        # never touched from two places at once.
        recorder = _Recorder(db, source)

        def handle(it) -> None:
            entry = recorder.add(it)
            if entry is None:
                raise RuntimeError("The downloaded file could not be stored.")
            if entry is not None and on_item is not None:
                on_item(entry)

        _, err = ig_downloader.run(url, cookies, tmp, limit=cap,
                                   sleep=sleep, on_item=handle,
                                   archive=archive, check_stop=check_stop)
        if not recorder.files:
            # `empty` separates "gallery-dl ran fine, there was simply nothing
            # new" from "it failed". Monitoring needs that distinction: with the
            # archive in play, finding nothing is the usual outcome and must not
            # count against the creator's failure streak.
            # "Nothing new" is the expected result of almost every monitoring
            # request, so it is debug. A real failure is still worth seeing.
            if err:
                log.debug("Instagram: %s failed: %s", url, err)
            else:
                log.debug("Instagram: nothing new at %s", url)
            return {"ok": False, "empty": not err,
                    "error": err or "No media found (login required / private / bad URL)."}
        result = recorder.result()
        if err:
            result["warning"] = err
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    creators = ", ".join(f"@{c}" for c in result["creators"])
    events_service.emit(
        db, ActivityType.download_completed,
        f"Instagram: downloaded {result['count']} item(s) from {creators}",
        meta={"count": result["count"], "creators": result["creators"], "source": source},
        log_source=LogSource.download,
        notify=(NotificationType.success, "Instagram download", f"{result['count']} item(s) from {creators}"),
    )
    # Only worth a line when something actually arrived.
    if result["count"]:
        log.info("Instagram: saved %d item(s) from %s", result["count"], creators)
    else:
        log.debug("Instagram: nothing new from %s", creators)
    return {"ok": True, **result}


def download_profile(db: Session, username: str, *, source: str = "instagram",
                     limit: int | None = None, on_item=None, use_archive: bool = False,
                     check_stop=None) -> dict:
    """Everything reachable for one profile: posts, highlights and stories.

    gallery-dl exposes these as three separate extractors, so this is three runs
    rather than one. They are aggregated into the same shape `download_url`
    returns, so callers do not need to care that it took more than one pass.

    Partial success is success. An account with no active stories is the normal
    case, not a failure, and a highlights reel that will not load should not
    throw away the posts that already downloaded. Only a run where every part
    came back empty is reported as an error, and then with the first real
    reason rather than a generic one.
    """
    username = (username or "").lstrip("@").strip("/")
    if not username:
        return {"ok": False, "error": "I couldn't read a username from that link."}
    if not cookie_service.has_instagram_cookies():
        return {"ok": False, "error": "No Instagram cookies set. Add them in Settings, under Cookies."}

    # Stories first, then highlights, then posts. Measured on this library, the
    # posts phase alone runs six to eleven minutes while stories and highlights
    # are usually a handful of files, so this order puts media in the chat in
    # seconds instead of minutes. Posts still arrive; they just arrive last,
    # and stories are the part that expires.
    parts = (
        ("stories", ig_urls.stories_url(username), None),
        ("highlights", ig_urls.highlights_url(username), None),
        ("posts", ig_urls.profile_url(username), limit),
    )

    total = 0
    files: list[dict] = []
    creators: set[str] = set()
    per_part: dict[str, int] = {}
    errors: dict[str, str] = {}

    for name, url, cap in parts:
        if check_stop:
            check_stop()
        try:
            result = download_url(db, url, source=source, limit=cap, on_item=on_item,
                                  use_archive=use_archive, check_stop=check_stop)
        except ig_downloader.DownloadStopped:
            raise
        except Exception as exc:
            log.warning("[@%s] instagram %s failed: %s", username, name, exc)
            errors[name] = str(exc)
            per_part[name] = 0
            continue
        if not result.get("ok"):
            # "nothing there" and "it broke" arrive the same way from gallery-dl,
            # so the reason is kept for the error path and otherwise ignored.
            if not result.get("empty"):
                errors[name] = str(result.get("error") or "Instagram did not finish this category.")
                log.warning("[@%s] instagram %s incomplete: %s", username, name, errors[name])
            per_part[name] = 0
            continue
        per_part[name] = int(result.get("count") or 0)
        if result.get("warning"):
            errors[name] = str(result["warning"])
        total += per_part[name]
        files.extend(result.get("files") or [])
        creators.update(result.get("creators") or [])

    if total == 0 and not files:
        reason = next((e for e in errors.values() if e), "")
        return {"ok": use_archive and not errors, "empty": not errors, "count": 0,
                "error": reason or f"Nothing downloadable found for @{username}.",
                "parts": per_part, "errors": errors}

    log.info("[@%s] instagram profile: %s", username,
             ", ".join(f"{k} {v}" for k, v in per_part.items()))
    return {
        "ok": True,
        "count": total,
        "files": files,
        "creators": sorted(creators) or [username],
        "parts": per_part,
        "errors": errors,
    }


def enqueue_profile_download(db: Session, user: models.User):
    """One durable, restartable queue entry for the whole Instagram profile."""
    if user.platform != platforms.INSTAGRAM:
        raise ValueError("An Instagram creator is required.")
    # Earlier Download All versions could attach a TikTok namesake's secUid.
    # It has no meaning for an Instagram account and must not be reused.
    user.sec_uid = None
    identifier = f"instagram-profile:{user.id}"
    existing = repo.Queue.active_for_video(db, user.id, identifier)
    if existing:
        return existing
    job = repo.Queue.enqueue(db, user_id=user.id, username=user.username,
                            video_id=identifier, url=ig_urls.profile_url(user.username),
                            title=f"Instagram posts, highlights and stories from @{user.username}", source="bulk")
    db.commit()
    publish("queue_job", {"id": job.id, "status": "pending", "progress": 0})
    return job


def process_profile_job(db: Session, job, user, stop_signal) -> bool:
    """Use Instagram's extractors, durable duplicate history and queue controls."""
    saved = 0

    def check_stop():
        signal = stop_signal()
        if signal in ("paused", "cancelled"):
            raise ig_downloader.DownloadStopped(signal)

    def on_item(entry):
        nonlocal saved
        saved += 1
        job.title = f"Instagram @{user.username}: {saved} item(s) saved"
        db.commit()
        publish("queue_job", {"id":job.id, "status":"processing", "progress":0})

    try:
        result = download_profile(db, user.username, source=job.source, limit=0,
                                  use_archive=True, on_item=on_item, check_stop=check_stop)
        check_stop()
    except ig_downloader.DownloadStopped as exc:
        job.status = exc.status
        job.worker = None
        job.finished_at = datetime.now(timezone.utc) if exc.status == "cancelled" else None
        job.started_at = None if exc.status == "paused" else job.started_at
        db.commit()
        publish("queue_job", {"id":job.id, "status":exc.status, "progress":job.progress})
        return False
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "count": saved}
    errors = result.get("errors") or {}
    success = bool(result.get("ok")) and not errors
    job.status = JobStatus.completed if success else JobStatus.failed
    job.progress = 100 if success else 0
    job.finished_at = datetime.now(timezone.utc)
    job.error = None if success else ("; ".join(f"{part}: {reason}" for part, reason in errors.items())
                                    or result.get("error") or "Instagram couldn't retrieve this profile.")
    count = int(result.get("count") or 0)
    job.title = f"Instagram @{user.username}: {count} item(s) saved" if count else f"Instagram posts, highlights and stories from @{user.username}"
    db.commit()
    publish("queue_job", {"id":job.id, "status":job.status, "progress":job.progress})
    if not success:
        events_service.emit(db, ActivityType.download_failed, f"Instagram @{user.username}: {job.error}",
                            user_id=user.id, log_source=LogSource.download,
                            notify=(NotificationType.error, "Instagram download incomplete", job.error))
    return success


def find_creator(db: Session, username: str) -> models.User | None:
    """An Instagram creator already in the library, or None.

    Scoped to the platform: a TikTok creator with the same handle is a different
    person as far as this app is concerned.
    """
    handle = (username or "").strip().lstrip("@").strip("/")
    if not handle:
        return None
    from sqlalchemy import func

    return db.scalar(
        select(models.User).where(
            models.User.platform == platforms.INSTAGRAM,
            func.lower(models.User.username) == handle.lower(),
        )
    )


def probe_creator(db: Session, username: str) -> tuple[dict | None, str]:
    """Resolve an Instagram handle against Instagram itself.

    Returns the account's real details, or None with a reason. One request,
    hard-bounded, so the caller can show an answer rather than a spinner that
    never resolves.
    """
    handle = (username or "").strip().lstrip("@").strip("/")
    if not handle:
        return None, "Enter a username."
    if not cookie_service.has_instagram_cookies():
        return None, "no-cookies"
    try:
        sleep = float(settings_service.get(db, "instagram_sleep"))
    except Exception:
        sleep = IG_SLEEP_SECS
    profile, error = ig_downloader.probe_profile(
        ig_urls.profile_url(handle), cookie_service.instagram_cookies_path(), sleep=sleep)
    if profile is None:
        return None, error

    # Instagram answered for some account; make sure it is the one that was
    # asked for. A handle that redirects elsewhere would otherwise be added
    # under the typed name while pointing at somebody else entirely.
    returned = (profile.get("username") or "").strip()
    if returned.lower() != handle.lower():
        return None, f"mismatch:{returned}"
    return profile, ""
