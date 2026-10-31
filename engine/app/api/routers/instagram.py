"""Instagram endpoints: cookie management + on-demand download."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...schemas import Ack
from ...services import cookie_service, instagram_service
from ..deps import get_db

router = APIRouter(prefix="/api/instagram", tags=["instagram"])


class InstagramCookieStatus(BaseModel):
    present: bool
    cookie_count: int
    file_path: str = ""


class InstagramDownloadRequest(BaseModel):
    url: str = Field(..., min_length=4)
    limit: int | None = Field(None, ge=1, le=500)


class InstagramDownloadResult(BaseModel):
    ok: bool
    count: int = 0
    creators: list[str] = []
    error: str | None = None


# ── Cookies ──
@router.get("/cookies", response_model=InstagramCookieStatus)
def cookie_status():
    return InstagramCookieStatus(
        present=cookie_service.has_instagram_cookies(),
        cookie_count=cookie_service.instagram_cookie_count(),
        file_path=str(cookie_service.instagram_cookies_path()),
    )


@router.post("/cookies", response_model=InstagramCookieStatus)
async def upload_cookies(file: UploadFile = File(...)):
    content = await file.read()
    try:
        count = cookie_service.save_instagram_cookies(content)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return InstagramCookieStatus(present=True, cookie_count=count)


@router.delete("/cookies", response_model=Ack)
def delete_cookies():
    removed = cookie_service.remove_instagram_cookies()
    return Ack(detail="Instagram cookies removed" if removed else "No Instagram cookies to remove")


# ── Download ──
@router.post("/download", response_model=InstagramDownloadResult)
def download(payload: InstagramDownloadRequest, db: Session = Depends(get_db)):
    result = instagram_service.download_url(db, payload.url, source="manual", limit=payload.limit)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "Instagram download failed")
    return InstagramDownloadResult(ok=True, count=result["count"], creators=result["creators"])


class InstagramMonitorRequest(BaseModel):
    username: str
    monitored: bool = True


@router.post("/monitor", response_model=Ack)
def monitor_creator(payload: InstagramMonitorRequest, db: Session = Depends(get_db)):
    """Put an Instagram creator on the monitoring list, or take them off it.

    Creates the creator row when it does not exist yet, so a handle can be
    monitored before anything has been downloaded from it. The row is the same
    shape a one-off download would have produced; only `monitored` differs.
    """
    handle = (payload.username or "").strip().lstrip("@").strip("/")
    if not handle:
        raise HTTPException(400, "A username is required.")

    instagram_service.set_monitoring(db, handle, payload.monitored)
    verb = "now monitored" if payload.monitored else "no longer monitored"
    return {"ok": True, "detail": f"@{handle} is {verb}."}


class InstagramPreview(BaseModel):
    exists: bool
    username: str
    nickname: str = ""
    avatar_url: str = ""
    user_id: str = ""
    known: bool = False          # already in the library
    addable: bool = True         # an unavailable lookup must not block adding
    error: str = ""


@router.get("/preview", response_model=InstagramPreview)
def preview_creator(username: str, db: Session = Depends(get_db)):
    """Resolve a handle against Instagram before it is added.

    Returns the account Instagram actually has for that handle - its display
    name, numeric id and profile picture - so the person adding it can see they
    got the right one. Answers within a bounded time whatever happens.
    """
    handle = (username or "").strip().lstrip("@").strip("/")
    if not handle:
        return InstagramPreview(exists=False, username="", addable=False,
                                error="Enter a username.")

    profile, reason = instagram_service.probe_creator(db, handle)
    existing = instagram_service.find_creator(db, handle)

    if profile is not None:
        return InstagramPreview(
            exists=True,
            username=profile.get("username") or handle,
            nickname=profile.get("fullname") or profile.get("username") or handle,
            avatar_url=profile.get("avatar") or "",
            user_id=profile.get("owner_id") or "",
            known=existing is not None,
        )

    # The codes come from the probe; the sentences are written here, where the
    # audience is a person deciding what to do next.
    if reason.startswith("mismatch:"):
        other = reason.split(":", 1)[1]
        return InstagramPreview(
            exists=False, username=handle, addable=False,
            error=f"Instagram resolved @{handle} to @{other}. Add @{other} instead.")

    messages = {
        # gallery-dl's generic NotFoundError can also mean its lookup was
        # blocked. Neither it nor an empty profile proves this handle is absent.
        "not-found": (f"Instagram couldn't verify @{handle} right now. It can still be added.", True),
        "no-profile": (f"Instagram returned no profile details for @{handle}. It can still be added.", True),
        "lookup-failed": (f"Instagram couldn't verify @{handle} right now. It can still be added.", True),
        "no-cookies": ("No Instagram cookies are set, so the handle can't be "
                       "checked. It can still be added.", True),
        "signed-out": ("Your Instagram sign-in isn't being accepted, so the handle "
                       "can't be checked. It can still be added.", True),
        "private": (f"@{handle} is private and this account doesn't follow them. "
                    "They can still be added.", True),
        "rate-limited": ("Instagram is rate limiting us, so the handle can't be "
                         "checked right now. It can still be added.", True),
    }
    text, addable = messages.get(reason, (f"Couldn't check @{handle}: {reason}", True))
    return InstagramPreview(exists=False, username=handle, addable=addable,
                            known=existing is not None, error=text)
