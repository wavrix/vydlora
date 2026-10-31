"""Stats, dashboard aggregation and health reporting."""

from __future__ import annotations

import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import repositories as repo
from ..core.config import settings as cfg
from ..core.events import manager
from ..schemas import (
    DashboardStats,
    HealthReport,
    QueueCounts,
    SystemOverview,
    TimePoint,
    TopCreator,
    WorkerOut,
)

_START_TIME = time.time()


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def overview(db: Session) -> SystemOverview:
    storage = repo.Downloads.storage_used(db)
    total = repo.Downloads.total(db)
    failed = max(0, _failed_downloads(db))
    success_rate = round((total / (total + failed) * 100) if (total + failed) else 100.0, 1)
    return SystemOverview(
        monitored_users=repo.Users.count(db),
        total_downloads=total,
        downloads_today=repo.Downloads.today(db),
        queue_size=repo.Queue.size(db),
        active_workers=repo.Workers.online_count(db),
        storage_used=storage,
        storage_used_human=human_size(storage),
        success_rate=success_rate,
    )


def _failed_downloads(db: Session) -> int:
    from sqlalchemy import func, select

    from .. import models
    from ..models.enums import DownloadStatus

    return db.scalar(
        select(func.count(models.Download.id)).where(models.Download.status == DownloadStatus.failed)
    ) or 0


def dashboard(db: Session) -> DashboardStats:
    counts = repo.Queue.counts(db)
    workers = [WorkerOut.model_validate(w) for w in repo.Workers.all(db)]
    return DashboardStats(
        overview=overview(db),
        downloads_per_day=[TimePoint(label=l, value=v) for l, v in repo.Downloads.per_day(db)],
        downloads_per_hour=[TimePoint(label=l, value=v) for l, v in repo.Downloads.per_hour(db)],
        top_creators=[
            TopCreator(username=u.username, nickname=u.nickname, avatar_url=u.avatar_url, downloads=c)
            for u, c in repo.Downloads.top_creators(db)
        ],
        queue=QueueCounts(**counts),
        workers=workers,
    )


def health(db: Session) -> HealthReport:
    try:
        db.execute(text("SELECT 1"))
        database = "healthy"
    except Exception:
        database = "down"

    cookies = repo.Cookies.list(db)
    cookies_valid = len([c for c in cookies if c.is_valid])
    online = repo.Workers.online_count(db)
    workers_total = len(repo.Workers.all(db))
    storage = repo.Downloads.storage_used(db)
    queue_size = repo.Queue.size(db)

    queue_health = "healthy" if queue_size < 500 else "backed_up"
    # Read the live state, not the env flag: a token configured from the app is
    # stored in settings, so cfg.telegram_enabled reported "disabled" for a bot
    # that was connected and polling.
    from . import telegram_service

    telegram = "running" if telegram_service.is_running() else (
        "enabled" if telegram_service.live_token(db) else "disabled"
    )
    overall = "healthy" if database == "healthy" and online > 0 else "degraded"

    return HealthReport(
        status=overall,
        database=database,
        queue=queue_health,
        telegram=telegram,
        api="healthy",
        workers_online=online,
        workers_total=workers_total,
        cookies_valid=cookies_valid,
        cookies_total=len(cookies),
        storage_used=storage,
        storage_used_human=human_size(storage),
        uptime_seconds=round(time.time() - _START_TIME, 1),
    )
