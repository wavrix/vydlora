"""Pydantic v2 schemas (request/response DTOs)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from ..models.enums import (
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

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Generic pagination envelope ──
class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int


# ── Users ──
class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)


class UserPreview(BaseModel):
    username: str
    nickname: str
    avatar_url: Optional[str] = None
    total_posts: int = 0
    followers: Optional[int] = None
    tiktok_uid: Optional[str] = None
    exists: bool = True


class UserUpdate(BaseModel):
    enabled: Optional[bool] = None
    status: Optional[UserStatus] = None
    nickname: Optional[str] = None


class UserOut(ORMModel):
    id: int
    state_key: str
    # Which site this creator is on. The Monitor page groups by it, so it has to
    # travel with the row rather than being inferred from the state key.
    platform: str = "tiktok"
    username: str
    nickname: str
    tiktok_uid: Optional[str]
    avatar_url: Optional[str]
    total_posts: int
    followers: Optional[int]
    following: Optional[int]
    status: UserStatus
    enabled: bool
    monitored: bool = True
    last_checked: Optional[datetime]
    last_download: Optional[datetime]
    success_rate: float
    created_at: datetime
    downloaded_count: int = 0
    pending_count: int = 0
    # Why a creator stopped working. Recorded on every failed scan but never
    # surfaced, so the UI could only show that something was wrong, not what.
    last_error: Optional[str] = None
    scan_failures: int = 0


class AllPostsPreview(BaseModel):
    total_posts: int
    already_downloaded: int
    to_download: int
    estimated_size: int
    estimated_size_human: str


class DownloadAllResult(BaseModel):
    queued: int
    total: int


class BulkImport(BaseModel):
    usernames: list[str]


class BulkImportResult(BaseModel):
    added: list[str]
    skipped: list[str]
    failed: list[str]


# ── Posts ──
class PostOut(ORMModel):
    id: int
    video_id: str
    url: str
    title: str
    post_type: PostType
    downloaded: bool
    created_at: datetime


# ── Downloads ──
class DownloadOut(ORMModel):
    id: int
    user_id: int
    video_id: str
    title: str
    file_path: Optional[str]
    remote_path: Optional[str] = None
    # The local poster image, when one exists. It is what the library grid draws
    # for a post whose media has moved to MEGA and is no longer on this machine.
    poster_path: Optional[str] = None
    file_size: int
    duration: Optional[float]
    thumbnail_url: Optional[str]
    post_type: PostType
    strategy: str
    status: DownloadStatus
    source: str
    created_at: datetime
    username: Optional[str] = None
    nickname: Optional[str] = None


# ── Queue ──
class QueueJobOut(ORMModel):
    id: int
    username: str
    video_id: str
    url: str
    title: str
    status: JobStatus
    progress: float
    worker: Optional[str]
    attempts: int
    error: Optional[str]
    source: str
    duration: Optional[float]
    created_at: datetime


class QueueCounts(BaseModel):
    pending: int = 0
    processing: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    paused: int = 0


# ── Workers ──
class WorkerOut(ORMModel):
    id: int
    name: str
    kind: str
    state: WorkerState
    current_job: Optional[str]
    processed: int
    failed: int
    last_heartbeat: Optional[datetime]


# ── Activity ──
class ActivityOut(ORMModel):
    id: int
    type: ActivityType
    message: str
    meta: Optional[str]
    user_id: Optional[int]
    created_at: datetime


# ── Logs ──
class LogOut(ORMModel):
    id: int
    level: LogLevel
    source: LogSource
    message: str
    created_at: datetime


# ── Notifications ──
class NotificationOut(ORMModel):
    id: int
    type: NotificationType
    title: str
    body: str
    read: bool
    created_at: datetime


# ── Cookies ──
class CookieProfileOut(ORMModel):
    id: int
    name: str
    file_path: str = ""
    active: bool
    is_valid: bool
    last_used: Optional[datetime]
    rate_limit_hits: int
    cookie_count: int
    created_at: datetime
    # Whether the file carries a logged-in TikTok session (`sessionid`). When
    # false, private / followers-only accounts can't be fetched.
    authenticated: bool = True


# ── Settings ──
class SettingsOut(BaseModel):
    mega_keep_local_copies: bool = True
    scan_interval: int
    scan_concurrency: int = 1
    monitor_stories: bool = False
    story_scan_interval: int = 1800
    scan_spacing_min: float = 2.0
    scan_spacing_max: float = 2.0
    catch_up_enabled: bool = True
    download_workers: int
    request_timeout: int
    max_posts_per_scan: int
    tikdownloader_min_interval: float
    tikdownloader_max_interval: float = 30.0
    tikdownloader_max_retries: int
    monitoring_paused: bool
    # Instagram monitoring. A separate set on purpose: the two platforms are
    # swept independently and want different numbers, since a TikTok check is a
    # cheap listing call and an Instagram one is a real request per creator.
    instagram_monitor_enabled: bool = False
    instagram_scan_interval: int = 900
    instagram_scan_concurrency: int = 2
    instagram_max_posts_per_scan: int = 5
    x_monitor_enabled: bool = False
    x_scan_interval: int = 900
    x_max_posts_per_scan: int = 10
    instagram_sleep: float = 2.0
    telegram_send_new_posts: bool = False
    telegram_upload_link_downloads: bool = False
    telegram_public_downloads: bool = True


class SettingsUpdate(BaseModel):
    mega_keep_local_copies: Optional[bool] = None
    scan_interval: Optional[int] = Field(None, ge=5, le=86400)
    scan_concurrency: Optional[int] = Field(None, ge=1, le=20)
    monitor_stories: Optional[bool] = None
    story_scan_interval: Optional[int] = Field(None, ge=300, le=86400)
    scan_spacing_min: Optional[float] = Field(None, ge=0, le=120)
    scan_spacing_max: Optional[float] = Field(None, ge=0, le=120)
    catch_up_enabled: Optional[bool] = None
    download_workers: Optional[int] = Field(None, ge=1, le=64)
    request_timeout: Optional[int] = Field(None, ge=1, le=120)
    max_posts_per_scan: Optional[int] = Field(None, ge=1, le=50)
    tikdownloader_min_interval: Optional[float] = Field(None, ge=0, le=60)
    tikdownloader_max_interval: Optional[float] = Field(None, ge=1, le=300)
    tikdownloader_max_retries: Optional[int] = Field(None, ge=1, le=10)
    monitoring_paused: Optional[bool] = None
    instagram_monitor_enabled: Optional[bool] = None
    # Floor of 60s: every Instagram check is a real request, and a tighter loop
    # buys nothing but rate limiting.
    instagram_scan_interval: Optional[int] = Field(None, ge=60, le=86400)
    # Capped well below TikTok's 20. Each unit is a gallery-dl process, not a
    # lightweight HTTP call.
    instagram_scan_concurrency: Optional[int] = Field(None, ge=1, le=6)
    instagram_max_posts_per_scan: Optional[int] = Field(None, ge=1, le=50)
    x_monitor_enabled: Optional[bool] = None
    x_scan_interval: Optional[int] = Field(None, ge=60, le=86400)
    x_max_posts_per_scan: Optional[int] = Field(None, ge=1, le=50)
    instagram_sleep: Optional[float] = Field(None, ge=0, le=60)
    telegram_send_new_posts: Optional[bool] = None
    telegram_upload_link_downloads: Optional[bool] = None
    telegram_public_downloads: Optional[bool] = None


class TelegramConfigUpdate(BaseModel):
    bot_token: Optional[str] = None
    allowed_ids: Optional[str] = None  # comma-separated chat ids


class StoragePathUpdate(BaseModel):
    downloads_dir: str = Field(..., min_length=1)


class MegaConnect(BaseModel):
    """A MEGA sign-in. The password is used for the one login call and is never
    stored - what persists is the session token MEGA hands back."""

    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=1)
    switch_account: bool = False


class MegaConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    folder: Optional[str] = None


# ── Dashboard / stats ──
class SystemOverview(BaseModel):
    monitored_users: int
    total_downloads: int
    downloads_today: int
    queue_size: int
    active_workers: int
    storage_used: int
    storage_used_human: str
    success_rate: float


class TimePoint(BaseModel):
    label: str
    value: float


class TopCreator(BaseModel):
    username: str
    nickname: str
    avatar_url: Optional[str]
    downloads: int


class DashboardStats(BaseModel):
    overview: SystemOverview
    downloads_per_day: list[TimePoint]
    downloads_per_hour: list[TimePoint]
    top_creators: list[TopCreator]
    queue: QueueCounts
    workers: list[WorkerOut]


class HealthReport(BaseModel):
    status: str
    database: str
    queue: str
    telegram: str
    api: str
    workers_online: int
    workers_total: int
    cookies_valid: int
    cookies_total: int
    storage_used: int
    storage_used_human: str
    uptime_seconds: float


class SearchResults(BaseModel):
    users: list[UserOut]
    downloads: list[DownloadOut]
    queue: list[QueueJobOut]
    logs: list[LogOut]


class Ack(BaseModel):
    ok: bool = True
    detail: str = ""


class ResetResult(BaseModel):
    ok: bool = True
    rows_deleted: int
    files_removed: int
