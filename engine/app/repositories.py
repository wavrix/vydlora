"""Repository layer - all DB queries live here (Repository Pattern).

Stateless functions taking a Session, grouped by aggregate. Services compose
these; routers may call read helpers directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import case, desc, event, func, or_, select, update
from sqlalchemy.orm import Session

from . import models
from .core import worker_wake
from .models.enums import (
    ActivityType,
    DownloadStatus,
    JobStatus,
    LogLevel,
    LogSource,
    NotificationType,
    UserStatus,
    WorkerState,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────── USERS ───────────────────────────
class Users:
    @staticmethod
    def get(db: Session, user_id: int) -> Optional[models.User]:
        return db.get(models.User, user_id)

    @staticmethod
    def by_state_key(db: Session, state_key: str) -> Optional[models.User]:
        return db.scalar(select(models.User).where(models.User.state_key == state_key))

    @staticmethod
    def by_username(db: Session, username: str, *, platform: str | None = None) -> Optional[models.User]:
        username = username.lstrip("@")
        query = select(models.User).where(func.lower(models.User.username) == username.lower())
        if platform:
            query = query.where(models.User.platform == platform)
        return db.scalar(query)

    @staticmethod
    def create(db: Session, **kwargs) -> models.User:
        user = models.User(**kwargs)
        db.add(user)
        db.flush()
        return user

    @staticmethod
    def all_enabled(db: Session, platform: str | None = None) -> Sequence[models.User]:
        stmt = select(models.User).where(
            models.User.enabled.is_(True),
            models.User.monitored.is_(True),
            models.User.status != UserStatus.deleted,
        )
        if platform:
            stmt = stmt.where(models.User.platform == platform)
        return db.scalars(stmt).all()

    @staticmethod
    def due_for_scan(db: Session, cutoff: datetime,
                     platform: str | None = None) -> Sequence[models.User]:
        """Monitored accounts due for a scan (never checked, or last checked
        before `cutoff`), most-overdue first so a bounded sweep is fair.

        `platform` is not optional in practice: the two sweeps check entirely
        different sites, and an Instagram creator handed to the TikTok scanner
        fails every time, three times over, and is then disabled as a gone
        account. Every caller passes one.
        """
        stmt = (
            select(models.User)
            .where(
                models.User.enabled.is_(True),
                models.User.monitored.is_(True),
                models.User.status != UserStatus.deleted,
                or_(models.User.last_checked.is_(None), models.User.last_checked < cutoff),
            )
            .order_by(models.User.last_checked.asc())  # NULLs first in SQLite
        )
        if platform:
            stmt = stmt.where(models.User.platform == platform)
        return db.scalars(stmt).all()

    @staticmethod
    def count(db: Session) -> int:
        # Only monitored accounts count toward the "Monitored" stat; ad-hoc
        # Telegram download creators are excluded.
        return db.scalar(
            select(func.count(models.User.id)).where(
                models.User.monitored.is_(True),
                models.User.status != UserStatus.deleted,
            )
        ) or 0

    @staticmethod
    def list_paginated(
        db: Session,
        page: int = 1,
        page_size: int = 25,
        search: str | None = None,
        status: str | None = None,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
        monitored_only: bool = True,
        platform: str | None = None,
        downloaded_only: bool = False,
    ) -> tuple[Sequence[models.User], int]:
        # Exclude removed accounts (consistent with count() / all_enabled above),
        # so a deleted account never lingers in the list.
        stmt = select(models.User).where(models.User.status != UserStatus.deleted)
        if monitored_only:
            stmt = stmt.where(models.User.monitored.is_(True))
        if downloaded_only:
            stmt = stmt.where(select(models.Download.id).where(
                models.Download.user_id == models.User.id,
                models.Download.status == DownloadStatus.completed,
            ).exists())
        if platform:
            stmt = stmt.where(models.User.platform == platform)
        if search:
            like = f"%{search}%"
            stmt = stmt.where(or_(models.User.username.ilike(like), models.User.nickname.ilike(like)))
        if status:
            stmt = stmt.where(models.User.status == status)

        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

        sort_col = getattr(models.User, sort_by, models.User.created_at)
        stmt = stmt.order_by(desc(sort_col) if sort_dir == "desc" else sort_col)
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        return db.scalars(stmt).all(), total


# ─────────────────────────── POSTS ───────────────────────────
class Posts:
    @staticmethod
    def seen_ids(db: Session, user_id: int) -> set[str]:
        # Regular posts only - stories live in the same table but are deduped
        # against their own id set (a story id must never suppress a post scan).
        rows = db.scalars(
            select(models.Post.video_id).where(
                models.Post.user_id == user_id, models.Post.is_story.is_(False)
            )
        ).all()
        return set(rows)

    @staticmethod
    def seen_story_ids(db: Session, user_id: int) -> set[str]:
        rows = db.scalars(
            select(models.Post.video_id).where(
                models.Post.user_id == user_id, models.Post.is_story.is_(True)
            )
        ).all()
        return set(rows)

    @staticmethod
    def exists(db: Session, user_id: int, video_id: str) -> bool:
        return db.scalar(
            select(func.count(models.Post.id)).where(
                models.Post.user_id == user_id, models.Post.video_id == video_id
            )
        ) > 0

    @staticmethod
    def add(db: Session, user_id: int, video_id: str, url: str, title: str = "", downloaded: bool = False, is_story: bool = False) -> models.Post:
        post = models.Post(user_id=user_id, video_id=video_id, url=url, title=title, downloaded=downloaded, is_story=is_story)
        db.add(post)
        db.flush()
        return post

    @staticmethod
    def mark_downloaded(db: Session, user_id: int, video_id: str) -> None:
        post = db.scalar(
            select(models.Post).where(models.Post.user_id == user_id, models.Post.video_id == video_id)
        )
        if post:
            post.downloaded = True

    @staticmethod
    def downloaded_ids(db: Session, user_id: int) -> set[str]:
        rows = db.scalars(
            select(models.Post.video_id).where(
                models.Post.user_id == user_id, models.Post.downloaded.is_(True)
            )
        ).all()
        return set(rows)

    @staticmethod
    def recent_for_user(db: Session, user_id: int, limit: int = 20) -> Sequence[models.Post]:
        return db.scalars(
            select(models.Post).where(models.Post.user_id == user_id).order_by(desc(models.Post.created_at)).limit(limit)
        ).all()

    @staticmethod
    def pending_count(db: Session, user_id: int) -> int:
        return db.scalar(
            select(func.count(models.Post.id)).where(
                models.Post.user_id == user_id, models.Post.downloaded.is_(False)
            )
        ) or 0


# ───────────────────────── DOWNLOADS ─────────────────────────
class Downloads:
    @staticmethod
    def add(db: Session, **kwargs) -> models.Download:
        dl = models.Download(**kwargs)
        db.add(dl)
        db.flush()
        return dl

    @staticmethod
    def total(db: Session) -> int:
        return db.scalar(
            select(func.count(models.Download.id)).where(models.Download.status == DownloadStatus.completed)
        ) or 0

    @staticmethod
    def today(db: Session) -> int:
        start = datetime.combine(_utcnow().date(), datetime.min.time())
        return db.scalar(
            select(func.count(models.Download.id)).where(
                models.Download.created_at >= start, models.Download.status == DownloadStatus.completed
            )
        ) or 0

    @staticmethod
    def storage_used(db: Session) -> int:
        return db.scalar(select(func.coalesce(func.sum(models.Download.file_size), 0))) or 0

    @staticmethod
    def completed_video_ids(db: Session, user_id: int) -> set[str]:
        """Video IDs actually downloaded (completed) for a user - the source of
        truth for 'already saved', independent of baseline/seen markers."""
        rows = db.scalars(
            select(models.Download.video_id).where(
                models.Download.user_id == user_id,
                models.Download.status == DownloadStatus.completed,
            )
        ).all()
        return set(rows)

    @staticmethod
    def avg_size(db: Session) -> int:
        avg = db.scalar(
            select(func.avg(models.Download.file_size)).where(
                models.Download.file_size > 0, models.Download.status == DownloadStatus.completed
            )
        )
        return int(avg) if avg else 0

    @staticmethod
    def list_paginated(
        db: Session, page: int = 1, page_size: int = 24, search: str | None = None,
        user_id: int | None = None, sort_by: str = "created_at", sort_dir: str = "desc",
        post_type: str | None = None,
        source: str | None = None, created_from: datetime | None = None,
        created_before: datetime | None = None,
        source_group: str | None = None,
    ) -> tuple[Sequence[models.Download], int]:
        stmt = select(models.Download)
        if user_id:
            stmt = stmt.where(models.Download.user_id == user_id)
        if post_type:
            stmt = stmt.where(models.Download.post_type == post_type)
        if source:
            stmt = stmt.where(models.Download.source == source)
        # The web library presents four categories without changing stored
        # provenance or the existing exact `source` filter.
        if source_group == "monitor":
            stmt = stmt.where(models.Download.source.in_(("monitor", "catchup")))
        elif source_group == "manual":
            stmt = stmt.where(models.Download.source.notin_(("monitor", "catchup", "story", "telegram")))
        elif source_group in ("story", "telegram"):
            stmt = stmt.where(models.Download.source == source_group)
        elif source_group:
            raise ValueError("Unknown download source group")
        if created_from is not None:
            stmt = stmt.where(models.Download.created_at >= created_from)
        if created_before is not None:
            stmt = stmt.where(models.Download.created_at < created_before)
        if search:
            like = f"%{search}%"
            # The creator is half of what anyone searches a media library for, and
            # it lives on the joined user row rather than on the download, so
            # matching only title/video_id quietly found nothing for a handle.
            stmt = stmt.outerjoin(models.User, models.Download.user_id == models.User.id).where(
                or_(
                    models.Download.title.ilike(like),
                    models.Download.video_id.ilike(like),
                    models.User.username.ilike(like),
                    models.User.nickname.ilike(like),
                )
            )
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        sort_col = getattr(models.Download, sort_by, models.Download.created_at)
        stmt = stmt.order_by(desc(sort_col) if sort_dir == "desc" else sort_col)
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        return db.scalars(stmt).all(), total

    @staticmethod
    def per_day(db: Session, days: int = 14) -> list[tuple[str, int]]:
        start = _utcnow() - timedelta(days=days)
        rows = db.execute(
            select(func.date(models.Download.created_at), func.count(models.Download.id))
            .where(models.Download.created_at >= start)
            .group_by(func.date(models.Download.created_at))
        ).all()
        counts = {str(r[0]): r[1] for r in rows}
        out = []
        for i in range(days - 1, -1, -1):
            day = (_utcnow() - timedelta(days=i)).date()
            out.append((day.isoformat(), counts.get(day.isoformat(), 0)))
        return out

    @staticmethod
    def per_hour(db: Session, hours: int = 24) -> list[tuple[str, int]]:
        start = _utcnow() - timedelta(hours=hours)
        rows = db.execute(
            select(func.strftime("%Y-%m-%d %H", models.Download.created_at), func.count(models.Download.id))
            .where(models.Download.created_at >= start)
            .group_by(func.strftime("%Y-%m-%d %H", models.Download.created_at))
        ).all()
        counts = {r[0]: r[1] for r in rows}
        out = []
        for i in range(hours - 1, -1, -1):
            ts = _utcnow() - timedelta(hours=i)
            key = ts.strftime("%Y-%m-%d %H")
            out.append((ts.strftime("%H:00"), counts.get(key, 0)))
        return out

    @staticmethod
    def series(
        db: Session, start: datetime, end: datetime, bucket: str = "day"
    ) -> list[tuple[str, int]]:
        """Completed-download counts bucketed by hour or day across [start, end].

        Gaps are filled with zeros so the caller always gets a contiguous series.
        Backs the dashboard's time-range insights (1h … 1y + custom windows).
        """
        if bucket == "hour":
            fmt, step = "%Y-%m-%d %H", timedelta(hours=1)
            cursor = start.replace(minute=0, second=0, microsecond=0)

            def label(ts: datetime) -> str:
                return ts.strftime("%H:00")
        else:
            fmt, step = "%Y-%m-%d", timedelta(days=1)
            cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)

            def label(ts: datetime) -> str:
                return ts.strftime("%b %d")

        rows = db.execute(
            select(func.strftime(fmt, models.Download.created_at), func.count(models.Download.id))
            .where(
                models.Download.created_at >= start,
                models.Download.created_at <= end,
                models.Download.status == DownloadStatus.completed,
            )
            .group_by(func.strftime(fmt, models.Download.created_at))
        ).all()
        counts = {r[0]: r[1] for r in rows}

        out: list[tuple[str, int]] = []
        guard = 0
        while cursor <= end and guard < 1000:  # guard caps pathological custom ranges
            out.append((label(cursor), counts.get(cursor.strftime(fmt), 0)))
            cursor += step
            guard += 1
        return out

    @staticmethod
    def top_creators(db: Session, limit: int = 5) -> list[tuple[models.User, int]]:
        rows = db.execute(
            select(models.User, func.count(models.Download.id).label("c"))
            .join(models.Download, models.Download.user_id == models.User.id)
            .where(models.Download.status == DownloadStatus.completed)
            .group_by(models.User.id)
            .order_by(desc("c"))
            .limit(limit)
        ).all()
        return [(r[0], r[1]) for r in rows]


# ─────────────────────────── QUEUE ───────────────────────────
def _wake_workers(session: Session) -> None:
    worker_wake.notify()


class Queue:
    @staticmethod
    def enqueue(db: Session, **kwargs) -> models.QueueJob:
        job = models.QueueJob(**kwargs)
        db.add(job)
        db.flush()
        # Nudge an idle worker, but only once the row is committed and therefore
        # visible to the worker's own session. Signalling at flush time would
        # wake it onto a queue that still looks empty and waste the nudge, so it
        # is deferred to this session's next commit.
        event.listen(db, "after_commit", _wake_workers, once=True)
        return job

    @staticmethod
    def get(db: Session, job_id: int) -> Optional[models.QueueJob]:
        return db.get(models.QueueJob, job_id)

    @staticmethod
    def by_statuses(db: Session, statuses) -> Sequence[models.QueueJob]:
        return db.scalars(select(models.QueueJob).where(models.QueueJob.status.in_(statuses))).all()

    # ── Pause / resume - one transition reused by per-item AND bulk actions, so
    #    they can never diverge (claim_next only ever claims `pending`, so a
    #    `paused` job is simply never started). ──
    @staticmethod
    def pause(job: models.QueueJob) -> bool:
        """Pause an active (pending/processing) job. Returns True if it changed."""
        if job.status in (JobStatus.pending, JobStatus.processing):
            job.status = JobStatus.paused
            return True
        return False

    @staticmethod
    def resume(job: models.QueueJob) -> bool:
        """Return a paused job to the pending queue. Returns True if it changed."""
        if job.status == JobStatus.paused:
            job.status = JobStatus.pending
            job.progress = 0.0
            job.worker = None
            job.error = None
            job.started_at = None
            job.finished_at = None
            return True
        return False

    @staticmethod
    def claim_next(db: Session, worker: str,
                   exclude_sources: "Sequence[str] | None" = None) -> Optional[models.QueueJob]:
        """Atomically claim the oldest pending job for a worker.

        The claim is a compare-and-swap: the UPDATE only matches while the row is
        still `pending`, so exactly one worker can win it and the losers see
        rowcount 0 and move on to the next job.

        Selecting the row and then assigning to it was not safe. Nothing
        re-checked the status between the read and the write, so every idle
        worker read the same row and every one of them wrote `processing` to it -
        one queued post got downloaded once per worker, all of them writing the
        same file and publishing conflicting progress for the same job id.
        Postgres was covered by FOR UPDATE SKIP LOCKED; SQLite, which this app
        actually runs on, had nothing.
        """
        stmt = (
            select(models.QueueJob)
            .where(models.QueueJob.status == JobStatus.pending)
            .order_by(models.QueueJob.created_at)
            .limit(1)
        )
        # Pausing monitoring holds the work monitoring created, and nothing else.
        # Telegram and manual downloads run through this same queue, so stopping
        # the queue wholesale would silently break the bot - which is exactly
        # what "pause monitoring, keep Telegram" must not do.
        if exclude_sources:
            stmt = stmt.where(models.QueueJob.source.notin_(list(exclude_sources)))
        if db.bind.dialect.name != "sqlite":
            stmt = stmt.with_for_update(skip_locked=True)

        # Bounded: each miss means another worker took that job, so the next
        # attempt sees a different one. Gives up rather than spinning if the
        # queue is being drained faster than this worker can claim.
        for _ in range(8):
            job = db.scalar(stmt)
            if not job:
                return None

            now = _utcnow()
            won = db.execute(
                update(models.QueueJob)
                .where(
                    models.QueueJob.id == job.id,
                    models.QueueJob.status == JobStatus.pending,
                )
                .values(
                    status=JobStatus.processing,
                    worker=worker,
                    started_at=now,
                    attempts=models.QueueJob.attempts + 1,
                )
            ).rowcount
            db.commit()

            if won == 1:
                # Mirror the winning UPDATE onto the instance the worker will
                # use, rather than refreshing it. `status` is a String column,
                # not an Enum, so a refreshed row hands back the raw value and
                # every `job.status.value` downstream fails.
                job.status = JobStatus.processing
                job.worker = worker
                job.started_at = now
                job.attempts = (job.attempts or 0) + 1
                return job
            db.expire_all()  # someone else claimed it; re-read and try the next
        return None

    @staticmethod
    def cancel_for_user(db: Session, user_id: int) -> tuple[int, int]:
        """Stop a creator's downloads when they're removed: delete their
        not-yet-started jobs and flag any in-flight one `cancelled` so the worker
        aborts it at the next progress tick. Does NOT commit - the caller commits
        atomically (together with the user delete) so a worker can't claim a
        pending job in between. Returns (removed_pending, cancelled_processing)."""
        cancelled = (
            db.query(models.QueueJob)
            .filter(
                models.QueueJob.user_id == user_id,
                models.QueueJob.status == JobStatus.processing,
            )
            .update({models.QueueJob.status: JobStatus.cancelled}, synchronize_session=False)
        )
        removed = (
            db.query(models.QueueJob)
            .filter(
                models.QueueJob.user_id == user_id,
                models.QueueJob.status.in_([JobStatus.pending, JobStatus.paused]),
            )
            .delete(synchronize_session=False)
        )
        return removed, cancelled

    @staticmethod
    def clear_active(db: Session) -> tuple[int, int]:
        """Clear the whole active queue: delete every not-yet-started job and flag
        any in-flight one `cancelled` so its worker aborts at the next progress
        tick. Does NOT commit. Returns (removed_pending, cancelled_processing)."""
        cancelled = (
            db.query(models.QueueJob)
            .filter(models.QueueJob.status == JobStatus.processing)
            .update({models.QueueJob.status: JobStatus.cancelled}, synchronize_session=False)
        )
        removed = (
            db.query(models.QueueJob)
            .filter(models.QueueJob.status.in_([JobStatus.pending, JobStatus.paused]))
            .delete(synchronize_session=False)
        )
        return removed, cancelled

    @staticmethod
    def active_video_ids(db: Session, user_id: int) -> set[str]:
        rows = db.scalars(
            select(models.QueueJob.video_id).where(
                models.QueueJob.user_id == user_id,
                models.QueueJob.status.in_([JobStatus.pending, JobStatus.processing, JobStatus.paused]),
            )
        ).all()
        return set(rows)

    @staticmethod
    def requeue_orphans(db: Session) -> int:
        """Return jobs left mid-flight to the queue. Called at startup.

        A job is only `processing` while a live worker owns it, so any that
        survive a restart or a crash belong to a worker that no longer exists and
        would sit there forever - and, since a re-submission joins an active job
        rather than starting a competing one, they would also block that post
        from ever being downloaded again."""
        n = (
            db.query(models.QueueJob)
            .filter(models.QueueJob.status == JobStatus.processing)
            .update(
                {
                    models.QueueJob.status: JobStatus.pending,
                    models.QueueJob.worker: None,
                    models.QueueJob.started_at: None,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return n

    @staticmethod
    def delete_many(db: Session, ids: Sequence[int]) -> int:
        """Remove selected jobs outright.

        Cancelling only stops work; a finished or cancelled row still sits in the
        list. Clearing a selection is the only way to actually take rows out
        without wiping the whole queue.
        """
        if not ids:
            return 0
        return db.query(models.QueueJob).filter(
            models.QueueJob.id.in_(list(ids))
        ).delete(synchronize_session=False)

    @staticmethod
    def latest_for_video(db: Session, user_id: int, video_id: str) -> Optional[models.QueueJob]:
        """The most recent job for a post, whatever state it ended in. Used to
        attach a stage that happens after the download itself has finished."""
        return db.scalars(
            select(models.QueueJob)
            .where(
                models.QueueJob.user_id == user_id,
                models.QueueJob.video_id == video_id,
            )
            .order_by(models.QueueJob.id.desc())
            .limit(1)
        ).first()

    @staticmethod
    def active_for_video(db: Session, user_id: int, video_id: str) -> Optional[models.QueueJob]:
        """An unfinished job for this exact post, if one is already queued.

        Submitting the same link twice used to add a second job, and with several
        workers both ran at once - downloading the same post to the same file and
        reporting two conflicting progress streams for it."""
        return db.scalars(
            select(models.QueueJob)
            .where(
                models.QueueJob.user_id == user_id,
                models.QueueJob.video_id == video_id,
                models.QueueJob.status.in_([JobStatus.pending, JobStatus.processing, JobStatus.paused]),
            )
            .order_by(models.QueueJob.id)
            .limit(1)
        ).first()

    @staticmethod
    def counts(db: Session) -> dict[str, int]:
        rows = db.execute(
            select(models.QueueJob.status, func.count(models.QueueJob.id)).group_by(models.QueueJob.status)
        ).all()
        out = {s.value: 0 for s in JobStatus}
        for status, count in rows:
            key = status.value if hasattr(status, "value") else str(status)
            out[key] = count
        return out

    @staticmethod
    def size(db: Session) -> int:
        return db.scalar(
            select(func.count(models.QueueJob.id)).where(
                models.QueueJob.status.in_([JobStatus.pending, JobStatus.processing])
            )
        ) or 0

    @staticmethod
    def list_paginated(
        db: Session, page: int = 1, page_size: int = 25, status: str | None = None,
    ) -> tuple[Sequence[models.QueueJob], int]:
        stmt = select(models.QueueJob)
        if status:
            stmt = stmt.where(models.QueueJob.status == status)
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        # What is happening now comes first, then what is about to happen, then
        # history. Ordering by creation date alone buried the two or three jobs
        # actually downloading somewhere in a list of hundreds, so the page
        # could not answer "what is it doing right now" without hunting.
        priority = case(
            (models.QueueJob.status == JobStatus.processing, 0),
            (models.QueueJob.status == JobStatus.pending, 1),
            (models.QueueJob.status == JobStatus.paused, 2),
            (models.QueueJob.status == JobStatus.failed, 3),
            else_=4,          # completed / cancelled - the archive
        )
        stmt = (
            stmt.order_by(priority, desc(models.QueueJob.created_at))
            .offset((page - 1) * page_size).limit(page_size)
        )
        return db.scalars(stmt).all(), total


# ─────────────────────────── WORKERS ─────────────────────────
class Workers:
    @staticmethod
    def upsert(db: Session, name: str, kind: str = "download") -> models.WorkerStatus:
        w = db.scalar(select(models.WorkerStatus).where(models.WorkerStatus.name == name))
        if not w:
            w = models.WorkerStatus(name=name, kind=kind)
            db.add(w)
            db.flush()
        return w

    @staticmethod
    def set_state(db: Session, name: str, state: WorkerState, current_job: str | None = None) -> None:
        w = db.scalar(select(models.WorkerStatus).where(models.WorkerStatus.name == name))
        if w:
            w.state = state
            w.current_job = current_job
            w.last_heartbeat = _utcnow()

    @staticmethod
    def heartbeat(db: Session, name: str, processed: int | None = None, failed: int | None = None) -> None:
        """Mark a worker alive, creating its row if missing.

        The row must be re-created here: a system reset deletes every
        WorkerStatus, and an IDLE worker only ever calls this method. When this
        was update-only, such a worker stayed alive but invisible forever - it
        reappeared only if it happened to claim a job (that path upserts) - so
        the dashboard under-reported the pool (e.g. 1/3 workers online).
        """
        w = db.scalar(select(models.WorkerStatus).where(models.WorkerStatus.name == name))
        if not w:
            # Re-register: a live worker sending a heartbeat is by definition online.
            w = models.WorkerStatus(name=name, kind="download", state=WorkerState.online)
            db.add(w)
            db.flush()
        w.last_heartbeat = _utcnow()
        if processed is not None:
            w.processed = processed
        if failed is not None:
            w.failed = failed

    @staticmethod
    def all(db: Session) -> Sequence[models.WorkerStatus]:
        return db.scalars(select(models.WorkerStatus).order_by(models.WorkerStatus.name)).all()

    @staticmethod
    def online_count(db: Session) -> int:
        return db.scalar(
            select(func.count(models.WorkerStatus.id)).where(
                models.WorkerStatus.state.in_([WorkerState.online, WorkerState.busy])
            )
        ) or 0


# ────────────────────────── ACTIVITY ─────────────────────────
class Activity:
    @staticmethod
    def add(db: Session, type_: ActivityType, message: str, meta: dict | None = None, user_id: int | None = None) -> models.ActivityEvent:
        ev = models.ActivityEvent(
            type=type_, message=message, meta=json.dumps(meta) if meta else None, user_id=user_id
        )
        db.add(ev)
        db.flush()
        return ev

    @staticmethod
    def delete_many(db: Session, ids: Sequence[int]) -> int:
        """Remove selected activity entries. Returns how many went."""
        if not ids:
            return 0
        return db.query(models.ActivityEvent).filter(
            models.ActivityEvent.id.in_(list(ids))
        ).delete(synchronize_session=False)

    @staticmethod
    def clear(db: Session) -> int:
        return db.query(models.ActivityEvent).delete(synchronize_session=False)

    @staticmethod
    def list(db: Session, limit: int = 50, type_: str | None = None) -> Sequence[models.ActivityEvent]:
        stmt = select(models.ActivityEvent)
        if type_:
            stmt = stmt.where(models.ActivityEvent.type == type_)
        return db.scalars(stmt.order_by(desc(models.ActivityEvent.created_at)).limit(limit)).all()


# ──────────────────────────── LOGS ───────────────────────────
class Logs:
    @staticmethod
    def add(db: Session, level: LogLevel, source: LogSource, message: str) -> models.LogEntry:
        entry = models.LogEntry(level=level, source=source, message=message)
        db.add(entry)
        db.flush()
        return entry

    @staticmethod
    def list_paginated(
        db: Session, page: int = 1, page_size: int = 50, level: str | None = None,
        source: str | None = None, search: str | None = None,
    ) -> tuple[Sequence[models.LogEntry], int]:
        stmt = select(models.LogEntry)
        if level:
            stmt = stmt.where(models.LogEntry.level == level)
        if source:
            stmt = stmt.where(models.LogEntry.source == source)
        if search:
            stmt = stmt.where(models.LogEntry.message.ilike(f"%{search}%"))
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        stmt = stmt.order_by(desc(models.LogEntry.created_at)).offset((page - 1) * page_size).limit(page_size)
        return db.scalars(stmt).all(), total


# ─────────────────────── NOTIFICATIONS ───────────────────────
class Notifications:
    @staticmethod
    def add(db: Session, type_: NotificationType, title: str, body: str = "") -> models.Notification:
        n = models.Notification(type=type_, title=title, body=body)
        db.add(n)
        db.flush()
        return n

    @staticmethod
    def list(db: Session, limit: int = 50) -> Sequence[models.Notification]:
        return db.scalars(select(models.Notification).order_by(desc(models.Notification.created_at)).limit(limit)).all()

    @staticmethod
    def unread_count(db: Session) -> int:
        return db.scalar(select(func.count(models.Notification.id)).where(models.Notification.read.is_(False))) or 0

    @staticmethod
    def mark_read(db: Session, notif_id: int | None = None) -> None:
        if notif_id:
            n = db.get(models.Notification, notif_id)
            if n:
                n.read = True
        else:
            for n in db.scalars(select(models.Notification).where(models.Notification.read.is_(False))).all():
                n.read = True


# ─────────────────────────── COOKIES ─────────────────────────
class Cookies:
    @staticmethod
    def list(db: Session) -> Sequence[models.CookieProfile]:
        return db.scalars(select(models.CookieProfile).order_by(models.CookieProfile.created_at)).all()

    @staticmethod
    def active(db: Session) -> Optional[models.CookieProfile]:
        return db.scalar(select(models.CookieProfile).where(models.CookieProfile.active.is_(True)))

    @staticmethod
    def by_name(db: Session, name: str) -> Optional[models.CookieProfile]:
        return db.scalar(select(models.CookieProfile).where(models.CookieProfile.name == name))

    @staticmethod
    def add(db: Session, name: str, file_path: str, cookie_count: int = 0, active: bool = False) -> models.CookieProfile:
        c = models.CookieProfile(name=name, file_path=file_path, cookie_count=cookie_count, active=active)
        db.add(c)
        db.flush()
        return c

    @staticmethod
    def set_active(db: Session, profile_id: int) -> None:
        for c in db.scalars(select(models.CookieProfile)).all():
            c.active = c.id == profile_id

    @staticmethod
    def remove(db: Session, profile_id: int) -> Optional[str]:
        """Delete a cookie profile row. Returns its file_path (captured before
        deletion) so the caller can remove the on-disk file. If the active
        profile was removed, the oldest remaining profile is promoted to active."""
        c = db.get(models.CookieProfile, profile_id)
        if not c:
            return None
        path = c.file_path
        was_active = c.active
        db.delete(c)
        db.flush()
        if was_active:
            nxt = db.scalar(select(models.CookieProfile).order_by(models.CookieProfile.created_at))
            if nxt:
                nxt.active = True
                db.flush()
        return path


# ─────────────────────────── SETTINGS ────────────────────────
class Settings:
    @staticmethod
    def get(db: Session, key: str, default=None):
        s = db.scalar(select(models.Setting).where(models.Setting.key == key))
        return json.loads(s.value) if s else default

    @staticmethod
    def set(db: Session, key: str, value) -> None:
        s = db.scalar(select(models.Setting).where(models.Setting.key == key))
        if s:
            s.value = json.dumps(value)
        else:
            db.add(models.Setting(key=key, value=json.dumps(value)))
        db.flush()

    @staticmethod
    def all(db: Session) -> dict:
        return {s.key: json.loads(s.value) for s in db.scalars(select(models.Setting)).all()}
