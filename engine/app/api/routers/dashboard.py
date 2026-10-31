"""Dashboard, analytics, health and global search."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ... import repositories as repo
from ...schemas import (
    DashboardStats,
    HealthReport,
    SearchResults,
    SystemOverview,
    TimePoint,
)
from ...services import stats_service, user_service
from ..deps import get_db
from .downloads import _to_out_many as _download_out_many

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard", response_model=DashboardStats)
def dashboard(db: Session = Depends(get_db)):
    return stats_service.dashboard(db)


@router.get("/overview", response_model=SystemOverview)
def overview(db: Session = Depends(get_db)):
    return stats_service.overview(db)


@router.get("/health", response_model=HealthReport)
def health(db: Session = Depends(get_db)):
    return stats_service.health(db)


@router.get("/analytics/downloads-per-day", response_model=list[TimePoint])
def downloads_per_day(days: int = Query(14, ge=1, le=90), db: Session = Depends(get_db)):
    return [TimePoint(label=l, value=v) for l, v in repo.Downloads.per_day(db, days)]


@router.get("/analytics/downloads-per-hour", response_model=list[TimePoint])
def downloads_per_hour(hours: int = Query(24, ge=1, le=72), db: Session = Depends(get_db)):
    return [TimePoint(label=l, value=v) for l, v in repo.Downloads.per_hour(db, hours)]


@router.get("/analytics/downloads-series", response_model=list[TimePoint])
def downloads_series(
    start: datetime = Query(..., description="ISO start of the window"),
    end: datetime = Query(..., description="ISO end of the window"),
    bucket: str = Query("day", pattern="^(hour|day)$"),
    db: Session = Depends(get_db),
):
    """Time-bucketed completed downloads for the dashboard's range selector.

    Supports any window (1h … 1y + custom). Hourly buckets are capped at 72h of
    span; anything longer falls back to daily buckets to stay sane.
    """
    start = start.astimezone(timezone.utc) if start.tzinfo else start.replace(tzinfo=timezone.utc)
    end = end.astimezone(timezone.utc) if end.tzinfo else end.replace(tzinfo=timezone.utc)
    if bucket == "hour" and (end - start).total_seconds() > 72 * 3600:
        bucket = "day"
    return [TimePoint(label=l, value=v) for l, v in repo.Downloads.series(db, start, end, bucket)]


@router.get("/search", response_model=SearchResults)
def search(q: str = Query(..., min_length=1), db: Session = Depends(get_db)):
    from sqlalchemy import select

    from ... import models

    users, _ = repo.Users.list_paginated(db, 1, 8, search=q)
    downloads, _ = repo.Downloads.list_paginated(db, 1, 8, search=q)
    logs, _ = repo.Logs.list_paginated(db, 1, 8, search=q)
    queue_rows = db.scalars(
        select(models.QueueJob)
        .where(models.QueueJob.username.ilike(f"%{q}%") | models.QueueJob.video_id.ilike(f"%{q}%"))
        .order_by(models.QueueJob.created_at.desc())
        .limit(8)
    ).all()
    return SearchResults(
        users=user_service.to_out_many(db, users),
        downloads=_download_out_many(db, downloads),
        queue=list(queue_rows),
        logs=list(logs),
    )
