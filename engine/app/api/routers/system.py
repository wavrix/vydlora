"""System endpoints: settings, cookies, workers, activity, logs, notifications, telegram."""

from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager, ExitStack
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ... import repositories as repo
from ...core.config import settings as cfg
from ...schemas import (
    Ack,
    ActivityOut,
    CookieProfileOut,
    LogOut,
    MegaConfigUpdate,
    MegaConnect,
    NotificationOut,
    Page,
    ResetResult,
    SettingsOut,
    SettingsUpdate,
    StoragePathUpdate,
    TelegramConfigUpdate,
    WorkerOut,
)
from ... import platforms
from ...services import (
    cookie_service,
    mega_service,
    settings_service,
    storage_service,
    telegram_service,
)
from ..deps import PageParams, get_db, page_params, paginate_meta

router = APIRouter(prefix="/api", tags=["system"])

# A Telegram bot token looks like `123456789:AAEr...` - a numeric bot id, a
# colon, then ~35 chars of [A-Za-z0-9_-]. Validated before saving.
TELEGRAM_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")


# ── Settings ──
@router.get("/settings", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db)):
    return SettingsOut(**settings_service.get_all(db))


@router.put("/settings", response_model=SettingsOut)
def update_settings(payload: SettingsUpdate, db: Session = Depends(get_db)):
    values = payload.model_dump(exclude_none=True)
    current = settings_service.get_all(db)
    if values.get("tikdownloader_max_interval", current.get("tikdownloader_max_interval", 30)) < values.get("tikdownloader_min_interval", current["tikdownloader_min_interval"]):
        raise HTTPException(400, "Maximum API interval must be at least the minimum interval.")
    if values.get("scan_spacing_max", current["scan_spacing_max"]) < values.get("scan_spacing_min", current["scan_spacing_min"]):
        raise HTTPException(400, "Maximum scan spacing must be at least the minimum spacing.")
    updated = settings_service.update(db, values)
    from ...workers.manager import manager_instance

    if "download_workers" in values:
        manager_instance.resize(int(values["download_workers"]))
    if "scan_interval" in values:
        manager_instance.reschedule(int(values["scan_interval"]))
    if "story_scan_interval" in values:
        manager_instance.reschedule_stories(int(values["story_scan_interval"]))
    if "x_scan_interval" in values:
        manager_instance.reschedule_x(int(values["x_scan_interval"]))
    if values.get("x_monitor_enabled"):
        manager_instance.trigger_x_sweep()
    if "instagram_scan_interval" in values:
        manager_instance.reschedule_instagram(int(values["instagram_scan_interval"]))
    if values.get("instagram_monitor_enabled"):
        # Sweep shortly after it is switched on, rather than leaving the first
        # check up to a full interval away.
        manager_instance.trigger_instagram_sweep()
    if values.get("monitor_stories"):
        # Kick off a sweep shortly after the user turns stories on, instead of
        # waiting up to a full story interval for the first one.
        manager_instance.trigger_story_sweep()
    if any(key in values for key in ("tikdownloader_min_interval", "tikdownloader_max_interval", "tikdownloader_max_retries")):
        from ...tiktok import downloader

        if "tikdownloader_min_interval" in values:
            downloader.set_min_interval(float(values["tikdownloader_min_interval"]))
        if "tikdownloader_max_interval" in values or "tikdownloader_min_interval" in values:
            downloader.set_max_interval(float(updated["tikdownloader_max_interval"]))
        if "tikdownloader_max_retries" in values:
            downloader.set_max_retries(int(values["tikdownloader_max_retries"]))
    return SettingsOut(**updated)


# ── Storage / downloads directory ──
def _storage_state(db: Session) -> dict:
    active = settings_service.downloads_path(db)
    return {
        "downloads_dir": str(active),
        "default_dir": str(settings_service.managed_downloads_path()),
        "is_custom": settings_service.is_custom_downloads_dir(db),
        "exists": active.exists(),
    }


@router.get("/storage")
def get_storage(db: Session = Depends(get_db)):
    return _storage_state(db)


@router.put("/storage")
def update_storage(payload: StoragePathUpdate, db: Session = Depends(get_db)):
    """Reject legacy folder selection; working storage is private and managed."""
    try:
        settings_service.set_downloads_dir(db, payload.downloads_dir)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _storage_state(db)


@router.post("/storage/reset")
def reset_storage(db: Session = Depends(get_db)):
    settings_service.reset_downloads_dir(db)
    return _storage_state(db)


# ── MEGA cloud storage ──
# Turning this on makes a finished download move to MEGA instead of staying on
# this machine. The account password never reaches the app's storage: `connect`
# spends it on one login and keeps the session token MEGA returns, and nothing
# here ever sends a credential back out.
@router.get("/mega")
def get_mega(db: Session = Depends(get_db)):
    return mega_service.state(db)


@router.post("/mega/connect")
def connect_mega(payload: MegaConnect, db: Session = Depends(get_db)):
    from ...services.cloud_sync_service import sync, BOUND_ACCOUNT
    from ...services.account_profile_service import prepare_switch, switching
    with _mega_connection_locks():
        # Re-read after taking the locks: simultaneous connects must not bind
        # and then overwrite the same previously unbound profile.
        db.expire_all()
        bound = repo.Settings.get(db, BOUND_ACCOUNT, "")
        if switching() or (bound and bound != payload.email.strip().lower()):
            if not payload.switch_account:
                raise HTTPException(409, "Choose Connect / switch account in Settings > Storage to open a separate profile. Your current data will be preserved.")
            try:
                return prepare_switch(db, payload.email, payload.password)
            except mega_service.MegaError as exc:
                raise HTTPException(400, str(exc))
            except (OSError, ValueError):
                raise HTTPException(400, "Could not save the account profile. Your previous account is unchanged. Check available disk space and try again.")
        try:
            mega_service.login(db, payload.email, payload.password)
        except mega_service.MegaError as exc:
            raise HTTPException(400, str(exc))
        sync.request()
        return mega_service.state(db)


@contextmanager
def _mega_connection_locks():
    from ...services.cloud_sync_service import sync
    with ExitStack() as acquired:
        for lock in (sync._lock, mega_service._lock):
            if not lock.acquire(timeout=10):
                # Do not leave a login queued after the UI's request times out.
                raise HTTPException(409, "A cloud transfer is finishing. Try connecting again in a moment.")
            acquired.callback(lock.release)
        yield


@router.post("/mega/disconnect")
def disconnect_mega(db: Session = Depends(get_db)):
    """Sign out and stop storing downloads in MEGA. Nothing already uploaded is
    touched - it stays in the account, reachable from any MEGA client."""
    mega_service.logout(db)
    from ...services.cloud_sync_service import sync
    sync.request()
    return mega_service.state(db)


@router.get("/mega/sync")
def get_cloud_sync():
    from ...services.cloud_sync_service import sync
    return sync.status()


@router.post("/mega/sync")
def sync_cloud():
    from ...services.cloud_sync_service import sync
    return sync.request()


@router.post("/mega/sync/use-device")
def use_sync_device(db: Session = Depends(get_db)):
    from ...services.cloud_sync_service import sync
    if not mega_service.whoami():
        raise HTTPException(400, "Connect to MEGA first.")
    return sync.take_over()


@router.put("/mega")
def update_mega(payload: MegaConfigUpdate, db: Session = Depends(get_db)):
    if payload.enabled is not None:
        if payload.enabled and not mega_service.whoami():
            raise HTTPException(400, "Connect a MEGA account first.")
        repo.Settings.set(db, mega_service.ENABLED_KEY, bool(payload.enabled))
    if payload.folder is not None:
        folder = payload.folder.strip() or mega_service.DEFAULT_FOLDER
        if not folder.startswith("/"):
            folder = "/" + folder
        repo.Settings.set(db, mega_service.FOLDER_KEY, folder.rstrip("/") or mega_service.DEFAULT_FOLDER)
    db.commit()
    return mega_service.state(db)


@router.post("/mega/clear-cache", response_model=Ack)
def clear_mega_cache():
    """Drop the local copies of files stored in MEGA. Costs a re-download the
    next time one is played; never loses anything."""
    freed = storage_service.clear_cache()
    return Ack(ok=True, detail=f"Freed {freed / 1024 ** 2:.0f} MB.")


# ── Cookies ──
def _with_auth_flag(profile):
    """Attach the computed `authenticated` flag (read from the file) so the UI can
    warn when a cookie file has no logged-in session."""
    profile.authenticated = cookie_service.profile_is_authenticated(Path(profile.file_path))
    return profile


@router.get("/cookies", response_model=list[CookieProfileOut])
def list_cookies(db: Session = Depends(get_db)):
    return [_with_auth_flag(p) for p in repo.Cookies.list(db)]


@router.post("/cookies/upload", response_model=CookieProfileOut)
async def upload_cookies(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    try:
        profile = cookie_service.save_uploaded(db, file.filename or "cookies.txt", content)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _with_auth_flag(profile)


@router.post("/cookies/{profile_id}/activate", response_model=Ack)
def activate_cookie(profile_id: int, db: Session = Depends(get_db)):
    repo.Cookies.set_active(db, profile_id)
    db.commit()
    return Ack(detail="Cookie profile activated")


@router.delete("/cookies/{profile_id}", response_model=Ack)
def remove_cookie(profile_id: int, db: Session = Depends(get_db)):
    """Remove a cookie profile (the saved reference) and its managed file."""
    if not cookie_service.remove_cookie(db, profile_id):
        raise HTTPException(404, "Cookie profile not found")
    return Ack(detail="Cookie file removed")


# ── Workers ──
@router.get("/workers", response_model=list[WorkerOut])
def list_workers(db: Session = Depends(get_db)):
    return list(repo.Workers.all(db))


@router.post("/workers/scan-now", response_model=Ack)
def scan_now():
    from ...workers.manager import manager_instance

    manager_instance.trigger_scan()
    return Ack(detail="Scan triggered")


@router.get("/workers/scan-status")
def scan_status(platform: str = "tiktok", db: Session = Depends(get_db)):
    """Real next-scan time (from the scheduler) so the UI countdown is accurate,
    plus the configured interval and whether monitoring is paused.

    `platform` picks which sweep is being asked about. They are separate jobs on
    separate intervals, and reporting TikTok's for both made a running Instagram
    monitor look idle.
    """
    from ...workers.manager import manager_instance

    if platform == platforms.X:
        from ...services import x_monitor_service
        nxt = manager_instance.next_x_scan_at()
        reason = x_monitor_service.deferral_reason(db)
        interval = int(settings_service.get(db, "x_scan_interval"))
        last = x_monitor_service.last_run_at()
    elif platform == platforms.INSTAGRAM:
        from ...services import instagram_monitor_service

        nxt = manager_instance.next_instagram_scan_at()
        reason = instagram_monitor_service.deferral_reason(db)
        interval = int(settings_service.get(db, "instagram_scan_interval"))
        last = instagram_monitor_service.last_run_at()
    else:
        from ...services import monitor_service

        nxt = manager_instance.next_scan_at()
        # A tick can fire on schedule and still scan nothing (e.g. deferred while
        # a big download backlog drains). Report that, so callers don't show a
        # countdown for a scan that won't actually do any work.
        reason = monitor_service.scan_deferral_reason(db)
        interval = int(settings_service.get(db, "scan_interval"))
        last = None

    return {
        # When the sweep last completed a tick. A tick that finds nothing due
        # does no work, so without this a healthy idle monitor looks stopped.
        "last_run_at": last.isoformat() if last else None,
        "next_scan_at": nxt.isoformat() if nxt else None,
        "interval": interval,
        "paused": bool(settings_service.get(db, "monitoring_paused")),
        "deferred": bool(reason),
        "deferred_reason": reason,
        "running": nxt is not None,
    }


@router.post("/workers/catch-up-now", response_model=Ack)
def catch_up_now():
    """Manually run the offline catch-up (full backfill of missed posts + active
    stories). Runs even if the auto catch-up setting is off; coalesced if one is
    already in progress."""
    from ...workers.manager import manager_instance

    started = manager_instance.run_catch_up("manual", respect_setting=False)
    return Ack(detail="Catch-up started" if started else "Catch-up already running")


@router.post("/downloads/reconcile", response_model=Ack)
def reconcile_downloads(db: Session = Depends(get_db)):
    """Scan every creator's folder and record existing media (files saved outside
    the app, or before a DB reset) into the database + metadata.json, so the
    catch-up / 'download all' logic won't re-download them. Safe to run anytime."""
    from ...services import reconcile_service

    result = reconcile_service.reconcile_all(db)
    return Ack(detail=f"Imported {result['recorded']} file(s) from {result['creators']} creator(s)")


# ── Activity ──
@router.get("/activity", response_model=list[ActivityOut])
def list_activity(limit: int = 50, type: str | None = None, db: Session = Depends(get_db)):
    return list(repo.Activity.list(db, limit=limit, type_=type))


class ActivityDeleteRequest(BaseModel):
    """Empty `ids` clears the feed. Explicit rather than a separate endpoint, so
    "delete what I selected" and "delete everything" cannot disagree."""

    ids: list[int] = []


@router.post("/activity/delete", response_model=Ack)
def delete_activity(payload: ActivityDeleteRequest, db: Session = Depends(get_db)):
    if payload.ids:
        removed = repo.Activity.delete_many(db, payload.ids)
    else:
        removed = repo.Activity.clear(db)
    db.commit()
    return Ack(ok=True, detail=f"Removed {removed} entr{'y' if removed == 1 else 'ies'}")


# ── Logs ──
@router.get("/logs", response_model=Page[LogOut])
def list_logs(
    params: PageParams = Depends(page_params),
    level: str | None = None,
    source: str | None = None,
    search: str | None = None,
    db: Session = Depends(get_db),
):
    items, total = repo.Logs.list_paginated(db, params.page, params.page_size, level, source, search)
    return {"items": list(items), **paginate_meta(total, params.page, params.page_size)}


def _enum_text(value) -> str:
    """Enum members and plain strings both reach here: rows loaded straight from
    SQLite hand back the raw string (`'INFO'`), while freshly-assigned attributes
    are real enum members. Taking `.value` unconditionally raised AttributeError
    and made this endpoint 500."""
    return str(getattr(value, "value", value) or "")


@router.get("/logs/live")
def live_logs(limit: int = Query(500, ge=1, le=2000), after: int = Query(0, ge=0)):
    """Recent backend log lines, oldest first.

    What the Terminal shows on open, so it has context instead of an empty
    screen until the next event. Live lines arrive over the WebSocket from
    there; this is only the backfill.
    """
    from ...core import logstream

    return {"lines": logstream.recent(limit, after), **logstream.stats()}


@router.get("/logs/export")
def export_logs(level: str | None = None, source: str | None = None, db: Session = Depends(get_db)):
    from fastapi.responses import PlainTextResponse

    rows, _ = repo.Logs.list_paginated(db, 1, 5000, level, source)
    lines = [
        f"{r.created_at.isoformat()} | {_enum_text(r.level):7} | {_enum_text(r.source):8} | {r.message}"
        for r in reversed(list(rows))
    ]
    return PlainTextResponse(
        "\n".join(lines),
        headers={"Content-Disposition": "attachment; filename=wevtik-logs.txt"},
    )


# ── Notifications ──
@router.get("/notifications", response_model=list[NotificationOut])
def list_notifications(limit: int = 50, db: Session = Depends(get_db)):
    return list(repo.Notifications.list(db, limit))


@router.get("/notifications/unread-count")
def unread_count(db: Session = Depends(get_db)):
    return {"count": repo.Notifications.unread_count(db)}


@router.post("/notifications/read", response_model=Ack)
def mark_read(notif_id: int | None = None, db: Session = Depends(get_db)):
    repo.Notifications.mark_read(db, notif_id)
    db.commit()
    return Ack(detail="Marked read")


# ── Reset ──
@router.post("/reset", response_model=ResetResult)
def reset_system(confirm: str = "", wipe_files: bool = False, db: Session = Depends(get_db)):
    """Clear all data and return to the initial state. Requires confirm=RESET."""
    if confirm != "RESET":
        raise HTTPException(400, "Confirmation required: pass confirm=RESET")
    from ...services import system_service

    result = system_service.reset(db, wipe_files=wipe_files)
    return ResetResult(rows_deleted=result["rows_deleted"], files_removed=result["files_removed"])


# ── Image proxy (avatars/thumbnails) ──
# TikTok CDN image URLs are signed and block hotlinking from the browser, so we
# fetch them server-side (impersonated) and cache to disk. Host-allowlisted to
# prevent SSRF.
_ALLOWED_IMG_HOSTS = ("tiktokcdn.com", "tiktokcdn-us.com", "ibyteimg.com", "muscdn.com", "bytecdn.com", "akamaized.net", "cdninstagram.com", "fbcdn.net", "twimg.com")


def _img_cache_dir() -> Path:
    d = settings_service.managed_downloads_path() / ".cache" / "img"
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.get("/media/image")
def proxy_image(url: str = Query(..., min_length=8)):
    host = (urlparse(url).hostname or "").lower()
    if not host or not any(host == h or host.endswith("." + h) for h in _ALLOWED_IMG_HOSTS):
        raise HTTPException(400, "Host not allowed")

    cache = _img_cache_dir() / (hashlib.md5(url.encode()).hexdigest() + ".img")
    if cache.exists() and cache.stat().st_size > 0:
        return FileResponse(str(cache))

    try:
        from curl_cffi import requests as cffi_requests

        resp = cffi_requests.get(
            url,
            impersonate="chrome136",
            headers={"Referer": "https://www.instagram.com/" if host.endswith((".cdninstagram.com", ".fbcdn.net")) else "https://www.tiktok.com/", "Accept": "image/avif,image/webp,image/*,*/*"},
            timeout=20,
        )
        if resp.status_code != 200 or not resp.content:
            raise HTTPException(404, "Image unavailable")
        ctype = resp.headers.get("content-type", "image/jpeg")
        cache.write_bytes(resp.content)
        return Response(content=resp.content, media_type=ctype, headers={"Cache-Control": "public, max-age=86400"})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(404, "Image unavailable")


# ── Telegram ──
def _telegram_state(db: Session) -> dict:
    token = telegram_service.live_token(db)
    return {
        "enabled": bool(token),
        "running": telegram_service.is_running(),
        "token_set": bool(token),
        "token_hint": f"…{token[-4:]}" if len(token) >= 4 else "",
        "allowed_ids": sorted(telegram_service.live_allowed_ids(db)),
    }


@router.get("/telegram/users")
def telegram_users(db: Session = Depends(get_db)):
    """Who is actually using the bot, and how much.

    Built from the two things the bot already records: every message it has ever
    received, and (since downloads carry the requesting chat) every item it
    fetched on someone's behalf. A chat that only ever messaged and was refused
    still appears, with zero downloads - which is exactly what makes this useful
    for deciding who to approve.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import desc, func, select

    from ... import models
    from ...models.enums import DownloadStatus

    allowed = telegram_service.live_allowed_ids(db)

    # Message-side aggregates, one row per chat.
    msg_rows = db.execute(
        select(
            models.TelegramMessage.chat_id,
            func.count(models.TelegramMessage.id),
            func.max(models.TelegramMessage.created_at),
            func.min(models.TelegramMessage.created_at),
            func.max(models.TelegramMessage.username),
        )
        .group_by(models.TelegramMessage.chat_id)
    ).all()

    # Download-side aggregates, keyed the same way.
    dl_rows = db.execute(
        select(
            models.Download.telegram_chat_id,
            func.count(models.Download.id),
            func.coalesce(func.sum(models.Download.file_size), 0),
            func.max(models.Download.created_at),
        )
        .where(
            models.Download.telegram_chat_id.is_not(None),
            models.Download.status == DownloadStatus.completed,
        )
        .group_by(models.Download.telegram_chat_id)
    ).all()
    downloads = {r[0]: (r[1], int(r[2] or 0), r[3]) for r in dl_rows}

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    recent_rows = db.execute(
        select(models.Download.telegram_chat_id, func.count(models.Download.id))
        .where(
            models.Download.telegram_chat_id.is_not(None),
            models.Download.status == DownloadStatus.completed,
            models.Download.created_at >= week_ago,
        )
        .group_by(models.Download.telegram_chat_id)
    ).all()
    recent = {r[0]: r[1] for r in recent_rows}

    users = []
    for chat_id, messages, last_seen, first_seen, handle in msg_rows:
        if chat_id is None:
            continue
        count, size, last_download = downloads.get(chat_id, (0, 0, None))
        users.append({
            "chat_id": chat_id,
            "username": handle or "",
            "approved": chat_id in allowed,
            "messages": messages,
            "downloads": count,
            "downloads_last_7d": recent.get(chat_id, 0),
            "bytes": size,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "last_download": last_download,
        })

    # Busiest first: the useful ordering both for spotting heavy users and for
    # noticing someone who has messaged a lot and downloaded nothing.
    users.sort(key=lambda u: (u["downloads"], u["messages"]), reverse=True)

    return {
        "total": len(users),
        "approved": sum(1 for u in users if u["approved"]),
        "active_last_7d": sum(1 for u in users if u["downloads_last_7d"] > 0),
        "users": users,
    }


@router.get("/telegram/status")
def telegram_status(db: Session = Depends(get_db)):
    from sqlalchemy import desc, select, func
    from datetime import datetime, timezone

    from ... import models

    recent = db.scalars(
        select(models.TelegramMessage).order_by(desc(models.TelegramMessage.created_at)).limit(20)
    ).all()
    return {
        **_telegram_state(db),
        "bot_username": telegram_service.bot_username(),
        "upload_limit_bytes": telegram_service.upload_limit_bytes(db),
        "requests_today": db.scalar(select(func.count(models.TelegramMessage.id)).where(
            models.TelegramMessage.created_at >= datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0))) or 0,
        "recent": [
            {
                "id": m.id,
                "chat_id": m.chat_id,
                "text": m.text,
                "command": m.command,
                "handled": m.handled,
                "created_at": m.created_at.isoformat(),
            }
            for m in recent
        ],
    }


@router.put("/telegram/config")
def update_telegram_config(payload: TelegramConfigUpdate, db: Session = Depends(get_db)):
    """Set the bot token / authorized chat ids and restart the bot to apply them.
    An empty bot_token disables the bot. The token is never returned."""
    if payload.bot_token is not None:
        token = payload.bot_token.strip()
        if token and not TELEGRAM_TOKEN_RE.match(token):
            raise HTTPException(
                400,
                "That doesn't look like a valid bot token. It should look like "
                "123456789:AAE… (copy it from @BotFather).",
            )
        repo.Settings.set(db, "bot_token", token)
    if payload.allowed_ids is not None:
        repo.Settings.set(db, "telegram_allowed_ids", payload.allowed_ids.strip())
    db.commit()
    telegram_service.restart()
    return _telegram_state(db)
