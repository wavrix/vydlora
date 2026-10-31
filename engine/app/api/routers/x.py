"""X endpoints shared with the desktop's creator and account controls."""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from ..deps import get_db
from ...schemas import Ack
from ...services import cookie_service, x_service

router = APIRouter(prefix="/api/x", tags=["x"])

@router.get("/cookies")
def cookies_status():
    return cookie_service.x_cookie_status()

@router.post("/cookies")
async def upload_cookies(file: UploadFile = File(...)):
    content = await file.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(400, "The cookie file is too large.")
    try:
        cookie_service.save_x_cookies(content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return cookie_service.x_cookie_status()

@router.delete("/cookies", response_model=Ack)
def delete_cookies():
    cookie_service.x_cookies_path().unlink(missing_ok=True)
    return Ack(detail="X disconnected")

class MonitorRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    monitored: bool = True

@router.post("/monitor", response_model=Ack)
def monitor(payload: MonitorRequest, db: Session = Depends(get_db)):
    try:
        user = x_service.set_monitoring(db, payload.username, payload.monitored)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Ack(detail=f"@{user.username}: monitoring {'on' if user.monitored else 'off'}")

@router.get("/preview")
def preview(username: str, db: Session = Depends(get_db)):
    try:
        user = x_service.find_creator(db, username)
        profile, error = x_service.probe_creator(db, username)
    except ValueError as exc:
        return {"exists": False, "addable": False, "error": str(exc)}
    profile = profile or {}
    return {"exists": bool(profile), "username": profile.get("username") or username,
            "nickname": profile.get("fullname") or (user.nickname if user else username),
            "avatar_url": profile.get("avatar") or (user.avatar_url if user else None),
            "user_id": profile.get("owner_id") or (user.platform_uid if user else None),
            "known": bool(user and user.monitored), "addable": error != "not-found", "error": error}

class DownloadRequest(BaseModel):
    url: str = Field(..., min_length=4)
    limit: int | None = Field(None, ge=1)

@router.post("/download")
def download(payload: DownloadRequest, db: Session = Depends(get_db)):
    result = x_service.download_url(db, payload.url, source="manual", limit=payload.limit)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "X download failed.")
    return {key: result.get(key) for key in ("ok", "count", "creators", "errors", "detail")}
