"""Users / Monitor endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ... import repositories as repo
from ...schemas import (
    Ack,
    AllPostsPreview,
    BulkImport,
    BulkImportResult,
    DownloadAllResult,
    DownloadOut,
    LogOut,
    Page,
    PostOut,
    UserCreate,
    UserOut,
    UserPreview,
)
from ... import platforms
from ...services import stats_service, user_service
from ..deps import PageParams, get_db, page_params, paginate_meta

router = APIRouter(prefix="/api/users", tags=["users"])


@router.post("/preview", response_model=UserPreview)
def preview_user(payload: UserCreate, db: Session = Depends(get_db)):
    return user_service.preview(db, payload.username)


@router.post("", response_model=UserOut, status_code=201)
def add_user(payload: UserCreate, db: Session = Depends(get_db)):
    user = user_service.add(db, payload.username)
    return user_service.to_out(db, user)


@router.post("/import", response_model=BulkImportResult)
def import_users(payload: BulkImport, db: Session = Depends(get_db)):
    return user_service.bulk_import(db, payload.usernames)


@router.get("", response_model=Page[UserOut])
def list_users(
    params: PageParams = Depends(page_params),
    search: str | None = None,
    status: str | None = None,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    platform: str | None = None,
    db: Session = Depends(get_db),
):
    """Monitored creators. `platform` scopes the list to one site, which is what
    lets the Monitor page show TikTok and Instagram separately."""
    users, total = repo.Users.list_paginated(
        db, params.page, params.page_size, search, status, sort_by, sort_dir,
        platform=platform,
    )
    items = user_service.to_out_many(db, users)
    return {"items": items, **paginate_meta(total, params.page, params.page_size)}


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: int, db: Session = Depends(get_db)):
    user = repo.Users.get(db, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    return user_service.to_out(db, user)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: dict, db: Session = Depends(get_db)):
    user = repo.Users.get(db, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    # `monitored` is settable so a creator can be moved on and off the
    # monitoring list without being deleted - the only way an Instagram creator
    # added by a one-off download becomes a monitored one.
    for field in ("enabled", "nickname", "status", "monitored"):
        if field in payload and payload[field] is not None:
            setattr(user, field, payload[field])
    db.commit()
    return user_service.to_out(db, user)


@router.delete("/{user_id}", response_model=Ack)
def delete_user(user_id: int, db: Session = Depends(get_db)):
    if not user_service.delete(db, user_id):
        raise HTTPException(404, "User not found")
    return Ack(detail="User removed")


@router.post("/{user_id}/scan", response_model=Ack)
def scan_user(user_id: int, db: Session = Depends(get_db)):
    user = repo.Users.get(db, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    # Routed by platform. This used to call the TikTok scanner for every
    # creator, so scanning an Instagram account looked the handle up on TikTok
    # and overwrote the row's name and avatar with whoever owns it there.
    if user.platform == platforms.X:
        from ...services import x_monitor_service
        found = x_monitor_service.scan_user(db, user)
        return Ack(detail=f"{found} new item(s) saved")
    if user.platform == platforms.INSTAGRAM:
        from ...services import instagram_monitor_service

        found = instagram_monitor_service.scan_user(db, user)
        return Ack(detail=f"{found} new item(s) saved")

    from ...services import monitor_service

    new = monitor_service.scan_user(db, user, use_browser=True)
    return Ack(detail=f"{new} new post(s) queued")


@router.get("/{user_id}/download-all/preview", response_model=AllPostsPreview)
def download_all_preview(user_id: int, db: Session = Depends(get_db)):
    user = repo.Users.get(db, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.platform != platforms.TIKTOK:
        raise HTTPException(409, "A post count isn't available in advance for this platform. Use its Download action to check and save available media.")
    from ...services import monitor_service

    est = monitor_service.estimate_all_posts(db, user)
    return AllPostsPreview(
        total_posts=est["total_posts"],
        already_downloaded=est["already_downloaded"],
        to_download=est["to_download"],
        estimated_size=est["estimated_size"],
        estimated_size_human=stats_service.human_size(est["estimated_size"]),
    )


@router.post("/{user_id}/download-all", response_model=DownloadAllResult)
def download_all(user_id: int, db: Session = Depends(get_db)):
    user = repo.Users.get(db, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.platform == platforms.INSTAGRAM:
        from ...services import instagram_service

        instagram_service.enqueue_profile_download(db, user)
        return DownloadAllResult(queued=1, total=1)
    if user.platform != platforms.TIKTOK:
        raise HTTPException(409, "Use this platform's Download action.")
    from ...services import monitor_service

    result = monitor_service.download_all_posts(db, user)
    return DownloadAllResult(**result)


@router.get("/{user_id}/posts", response_model=list[PostOut])
def user_posts(user_id: int, limit: int = Query(30, le=200), db: Session = Depends(get_db)):
    return list(repo.Posts.recent_for_user(db, user_id, limit))


@router.get("/{user_id}/downloads", response_model=Page[DownloadOut])
def user_downloads(
    user_id: int, params: PageParams = Depends(page_params), db: Session = Depends(get_db)
):
    items, total = repo.Downloads.list_paginated(db, params.page, params.page_size, user_id=user_id)
    return {"items": list(items), **paginate_meta(total, params.page, params.page_size)}


@router.get("/{user_id}/logs", response_model=list[LogOut])
def user_logs(user_id: int, limit: int = Query(50, le=200), db: Session = Depends(get_db)):
    user = repo.Users.get(db, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    rows, _ = repo.Logs.list_paginated(db, page=1, page_size=limit, search=f"@{user.username}")
    return list(rows)
