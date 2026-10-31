"""SQLAlchemy ORM models for Lora 1.

Eleven tables with relationships, indexes and constraints. SQLite today;
the same models migrate to PostgreSQL by changing DATABASE_URL.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.database import Base
from .enums import (
    ActivityType,
    DownloadStatus,
    JobStatus,
    LogLevel,
    LogSource,
    NotificationType,
    PostType,
    UserStatus,
    WorkerState,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )


# ─────────────────────────────────────────────────────────────
# USERS
# ─────────────────────────────────────────────────────────────
class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable key surviving handle renames: "uid:<digits>" or "@handle" (TikTok),
    # or "ig:@handle" (Instagram).
    state_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Which site this creator belongs to. TikTok creators are monitored; Instagram
    # creators are on-demand download targets (monitored=False), so they never hit
    # the TikTok scanner.
    platform: Mapped[str] = mapped_column(String(16), default="tiktok", index=True)
    username: Mapped[str] = mapped_column(String(128), index=True)
    nickname: Mapped[str] = mapped_column(String(255), default="")
    tiktok_uid: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    # The platform's own numeric account id for creators that are not TikTok
    # (Instagram's `owner_id`). Captured when the creator is added, so identity
    # is pinned to the account rather than re-derived from a handle - handles
    # are reused, and the same one exists on both sites.
    platform_uid: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    # TikTok secUid ("MS4wLj…") - enables the WAF-resistant / private post listing
    # via yt-dlp's `tiktokuser:<secUid>`. Captured from profile metadata, reused
    # every scan so private/WAF accounts are listed without the headless browser.
    sec_uid: Mapped[str | None] = mapped_column(String(120), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    total_posts: Mapped[int] = mapped_column(Integer, default=0)
    followers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    following: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[UserStatus] = mapped_column(String(20), default=UserStatus.monitoring, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Whether the account is on the monitoring list. Ad-hoc creators created by a
    # Telegram one-off download are stored with monitored=False so they don't
    # appear in Accounts or get scanned, until promoted (e.g. via /user).
    monitored: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    folder_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    last_checked: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_download: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Consecutive failed scans; a live account only flips to 'error' after this
    # crosses a threshold, so a single rate-limited blip doesn't alarm the user.
    scan_failures: Mapped[int] = mapped_column(Integer, default=0)
    success_rate: Mapped[float] = mapped_column(Float, default=100.0)

    posts: Mapped[list["Post"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    downloads: Mapped[list["Download"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def downloaded_count(self) -> int:
        return len([d for d in self.downloads if d.status == DownloadStatus.completed])


# ─────────────────────────────────────────────────────────────
# POSTS  (detected TikTok posts - seen set + new-post tracking)
# ─────────────────────────────────────────────────────────────
class Post(Base, TimestampMixin):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("user_id", "video_id", name="uq_post_user_video"),
        Index("ix_posts_user_downloaded", "user_id", "downloaded"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    video_id: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    post_type: Mapped[PostType] = mapped_column(String(10), default=PostType.video)
    downloaded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Ephemeral 24h stories are tracked here too, flagged so they're deduped and
    # displayed separately from regular posts.
    is_story: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="posts")


# ─────────────────────────────────────────────────────────────
# DOWNLOADS
# ─────────────────────────────────────────────────────────────
class Download(Base, TimestampMixin):
    __tablename__ = "downloads"
    __table_args__ = (Index("ix_downloads_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    video_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Where the media lives in MEGA, when the app stores downloads there. Set
    # once the upload succeeds; `file_path` keeps naming the local slot, which
    # by then holds only the poster image.
    remote_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_type: Mapped[PostType] = mapped_column(String(10), default=PostType.video)
    strategy: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[DownloadStatus] = mapped_column(String(20), default=DownloadStatus.completed, index=True)
    source: Mapped[str] = mapped_column(String(20), default="monitor")  # monitor | telegram | manual
    # Which Telegram chat asked for this, when source is "telegram". Null for
    # everything the app fetched on its own.
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="downloads")


# ─────────────────────────────────────────────────────────────
# QUEUE JOBS
# ─────────────────────────────────────────────────────────────
class QueueJob(Base, TimestampMixin):
    __tablename__ = "queue_jobs"
    __table_args__ = (Index("ix_queue_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    username: Mapped[str] = mapped_column(String(128), default="")
    video_id: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[JobStatus] = mapped_column(String(20), default=JobStatus.pending, index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    worker: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="monitor")
    # Carried through to the Download this job produces, so a finished item keeps
    # the attribution the queue entry had.
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def duration(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None


# ─────────────────────────────────────────────────────────────
# WORKER STATUS
# ─────────────────────────────────────────────────────────────
class WorkerStatus(Base, TimestampMixin):
    __tablename__ = "worker_status"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="download")  # download | monitor | scheduler
    state: Mapped[WorkerState] = mapped_column(String(20), default=WorkerState.offline)
    current_job: Mapped[str | None] = mapped_column(String(128), nullable=True)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ─────────────────────────────────────────────────────────────
# ACTIVITY EVENTS
# ─────────────────────────────────────────────────────────────
class ActivityEvent(Base):
    __tablename__ = "activity_events"
    __table_args__ = (Index("ix_activity_created", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[ActivityType] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(Text)
    meta: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON blob
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


# ─────────────────────────────────────────────────────────────
# TELEGRAM MESSAGES
# ─────────────────────────────────────────────────────────────
class TelegramMessage(Base):
    __tablename__ = "telegram_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    command: Mapped[str | None] = mapped_column(String(32), nullable=True)
    handled: Mapped[bool] = mapped_column(Boolean, default=False)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


# ─────────────────────────────────────────────────────────────
# SETTINGS  (key/value, JSON-encoded values)
# ─────────────────────────────────────────────────────────────
class Setting(Base, TimestampMixin):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text)  # JSON-encoded


# ─────────────────────────────────────────────────────────────
# COOKIE PROFILES
# ─────────────────────────────────────────────────────────────
class CookieProfile(Base, TimestampMixin):
    __tablename__ = "cookie_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    file_path: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rate_limit_hits: Mapped[int] = mapped_column(Integer, default=0)
    cookie_count: Mapped[int] = mapped_column(Integer, default=0)


# ─────────────────────────────────────────────────────────────
# LOGS
# ─────────────────────────────────────────────────────────────
class LogEntry(Base):
    __tablename__ = "logs"
    __table_args__ = (Index("ix_logs_level_created", "level", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[LogLevel] = mapped_column(String(10), index=True)
    source: Mapped[LogSource] = mapped_column(String(20), index=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


# ─────────────────────────────────────────────────────────────
# NOTIFICATIONS
# ─────────────────────────────────────────────────────────────
class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[NotificationType] = mapped_column(String(20), default=NotificationType.info)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, default="")
    read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class CloudRecord(Base):
    """Local merge journal. Never replaces the running database with a cloud copy."""
    __tablename__ = "cloud_records"
    __table_args__ = (UniqueConstraint("account", "key", name="uq_cloud_account_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(320), index=True)
    key: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32))
    local_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[str] = mapped_column(Text)
    baseline: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [
    "User", "Post", "Download", "QueueJob", "WorkerStatus", "ActivityEvent",
    "TelegramMessage", "Setting", "CookieProfile", "LogEntry", "Notification", "CloudRecord",
]
