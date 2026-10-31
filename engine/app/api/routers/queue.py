"""Download queue endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ... import repositories as repo
from ...core.events import publish
from ...models.enums import JobStatus
from ...schemas import Ack, Page, QueueCounts, QueueJobOut
from ..deps import PageParams, get_db, page_params, paginate_meta

router = APIRouter(prefix="/api/queue", tags=["queue"])


@router.get("", response_model=Page[QueueJobOut])
def list_queue(
    params: PageParams = Depends(page_params),
    status: str | None = None,
    db: Session = Depends(get_db),
):
    items, total = repo.Queue.list_paginated(db, params.page, params.page_size, status)
    return {"items": list(items), **paginate_meta(total, params.page, params.page_size)}


@router.get("/counts", response_model=QueueCounts)
def queue_counts(db: Session = Depends(get_db)):
    return QueueCounts(**repo.Queue.counts(db))


@router.post("/{job_id}/retry", response_model=Ack)
def retry_job(job_id: int, db: Session = Depends(get_db)):
    job = repo.Queue.get(db, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.status = JobStatus.pending
    job.error = None
    job.progress = 0.0
    job.worker = None
    job.started_at = None
    job.finished_at = None
    db.commit()
    publish("queue_job", {"id": job.id, "status": "pending", "progress": 0})
    return Ack(detail="Job requeued")


@router.post("/{job_id}/cancel", response_model=Ack)
def cancel_job(job_id: int, db: Session = Depends(get_db)):
    job = repo.Queue.get(db, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.status = JobStatus.cancelled
    job.finished_at = datetime.now(timezone.utc)
    db.commit()
    publish("queue_job", {"id": job.id, "status": "cancelled"})
    return Ack(detail="Job cancelled")


@router.post("/{job_id}/pause", response_model=Ack)
def pause_job(job_id: int, db: Session = Depends(get_db)):
    job = repo.Queue.get(db, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if repo.Queue.pause(job):
        db.commit()
        publish("queue_job", {"id": job.id, "status": job.status.value})
    return Ack(detail="Job paused")


@router.post("/{job_id}/resume", response_model=Ack)
def resume_job(job_id: int, db: Session = Depends(get_db)):
    job = repo.Queue.get(db, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if repo.Queue.resume(job):
        db.commit()
        publish("queue_job", {"id": job.id, "status": job.status.value, "progress": 0})
    return Ack(detail="Job resumed")


@router.post("/pause-all", response_model=Ack)
def pause_all(db: Session = Depends(get_db)):
    """Pause every active (queued or downloading) job. In-flight files finish;
    nothing new starts. Same transition as the per-item pause."""
    jobs = repo.Queue.by_statuses(db, [JobStatus.pending, JobStatus.processing])
    changed = sum(1 for j in jobs if repo.Queue.pause(j))
    db.commit()
    publish("queue_job", {"bulk": True, "status": "paused"})
    return Ack(detail=f"Paused {changed} job(s)")


@router.post("/resume-all", response_model=Ack)
def resume_all(db: Session = Depends(get_db)):
    """Resume every paused job back into the queue."""
    jobs = repo.Queue.by_statuses(db, [JobStatus.paused])
    changed = sum(1 for j in jobs if repo.Queue.resume(j))
    db.commit()
    publish("queue_job", {"bulk": True, "status": "pending"})
    return Ack(detail=f"Resumed {changed} job(s)")


@router.post("/clear", response_model=Ack)
def clear_queue(db: Session = Depends(get_db)):
    """Clear the active queue: delete every queued/paused job and cancel any
    in-flight download (its worker aborts mid-transfer). Completed/failed history
    is left intact - use clear-completed for that."""
    removed, cancelled = repo.Queue.clear_active(db)
    db.commit()
    publish("queue_job", {"bulk": True, "status": "cleared"})
    return Ack(detail=f"Cleared {removed} queued + cancelled {cancelled} in-flight job(s)")


@router.post("/clear-completed", response_model=Ack)
def clear_completed(db: Session = Depends(get_db)):
    from ... import models

    deleted = (
        db.query(models.QueueJob)
        .filter(models.QueueJob.status.in_([JobStatus.completed, JobStatus.cancelled]))
        .delete(synchronize_session=False)
    )
    db.commit()
    return Ack(detail=f"Cleared {deleted} job(s)")


class BulkRemoveRequest(BaseModel):
    ids: list[int] = Field(..., min_length=1)


@router.post("/bulk-remove", response_model=Ack)
def bulk_remove(payload: BulkRemoveRequest, db: Session = Depends(get_db)):
    """Take a selection of jobs out of the list.

    Distinct from cancelling, which only stops work and leaves the row in place.
    Anything still running is cancelled first so a worker cannot finish onto a
    row that no longer exists.
    """
    from ... import models

    running = (
        db.query(models.QueueJob)
        .filter(
            models.QueueJob.id.in_(payload.ids),
            models.QueueJob.status == JobStatus.processing,
        )
        .update({models.QueueJob.status: JobStatus.cancelled}, synchronize_session=False)
    )
    if running:
        db.commit()

    removed = repo.Queue.delete_many(db, payload.ids)
    db.commit()
    publish("queue_job", {"bulk": True, "status": "cleared"})
    return Ack(detail=f"Removed {removed} job(s)")
