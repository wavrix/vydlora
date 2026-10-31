"""Shared enums used by models and schemas."""

from __future__ import annotations

import enum


class UserStatus(str, enum.Enum):
    active = "active"
    monitoring = "monitoring"
    checking = "checking"
    error = "error"
    rate_limited = "rate_limited"
    private = "private"
    deleted = "deleted"
    disabled = "disabled"


class JobStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    paused = "paused"


class DownloadStatus(str, enum.Enum):
    completed = "completed"
    failed = "failed"


class WorkerState(str, enum.Enum):
    online = "online"
    offline = "offline"
    busy = "busy"
    error = "error"


class LogLevel(str, enum.Enum):
    debug = "DEBUG"
    info = "INFO"
    warning = "WARNING"
    error = "ERROR"


class LogSource(str, enum.Enum):
    system = "system"
    worker = "worker"
    download = "download"
    telegram = "telegram"
    monitor = "monitor"
    api = "api"


class ActivityType(str, enum.Enum):
    user_added = "user_added"
    user_removed = "user_removed"
    download_started = "download_started"
    download_completed = "download_completed"
    download_failed = "download_failed"
    queue_completed = "queue_completed"
    worker_restart = "worker_restart"
    telegram_received = "telegram_received"
    cookie_rotation = "cookie_rotation"
    rate_limit = "rate_limit"
    user_private = "user_private"
    cookie_expired = "cookie_expired"
    scan_completed = "scan_completed"


class NotificationType(str, enum.Enum):
    info = "info"
    success = "success"
    warning = "warning"
    error = "error"


class PostType(str, enum.Enum):
    video = "video"
    image = "image"
