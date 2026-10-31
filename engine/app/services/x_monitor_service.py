"""Independent X sweep, with global pause and cloud device ownership."""
from datetime import datetime, timedelta, timezone
import threading
from .. import models, platforms, repositories as repo
from ..core.database import session_scope
from ..core.events import publish
from ..models.enums import UserStatus
from . import cookie_service, settings_service, x_service

_lock = threading.Lock()
_users_lock = threading.Lock()
_user_locks = {}
_last_run = None

def last_run_at():
    return _last_run

def deferral_reason(db):
    if not settings_service.get(db, "x_monitor_enabled"):
        return "X monitoring is off."
    if settings_service.get(db, "monitoring_paused"):
        return "Monitoring is paused."
    from .cloud_sync_service import sync
    if not sync.can_automate(db):
        return "Background monitoring runs on another device."
    if not cookie_service.has_x_cookies():
        return "Connect X cookies in Settings → Accounts."
    return ""

def scan_user(db, user):
    if user.platform != platforms.X or not user.monitored or not user.enabled:
        return 0
    with _users_lock:
        lock = _user_locks.setdefault(user.id, threading.Lock())
    if not lock.acquire(blocking=False):
        return 0
    try:
        user.status = UserStatus.checking
        db.commit()
        publish("scan", {"phase": "start", "uid": user.id, "username": user.username, "platform": platforms.X})
        try:
            profile, error = x_service.probe_creator(db, user.username)
            if profile and user.platform_uid and profile.get("owner_id") and profile["owner_id"] != user.platform_uid:
                raise ValueError("This X username now belongs to a different account. Re-add the creator to confirm it.")
            if error == "not-found":
                raise ValueError("This X account could not be found.")
            if profile:
                x_service._update_profile(user, profile)
            result = x_service.download_profile(db, user.username, source="monitor", use_archive=True,
                        limit=int(settings_service.get(db, "x_max_posts_per_scan")), expected_user=user)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        user.last_checked = datetime.now(timezone.utc)
        failures = result.get("errors") or {}
        if not result.get("ok") or failures:
            user.scan_failures = (user.scan_failures or 0) + 1
            user.last_error = (result.get("error") or next(iter(failures.values()), "X check failed."))[:500]
            user.status = UserStatus.error if user.scan_failures >= 3 else UserStatus.monitoring
        else:
            user.scan_failures = 0
            user.last_error = None
            user.status = UserStatus.monitoring
        db.commit()
        count = int(result.get("count") or 0)
        publish("scan", {"phase": "done", "uid": user.id, "username": user.username, "platform": platforms.X,
                         "new": count, "failed": bool(user.last_error)})
        return count
    finally:
        lock.release()

def scan_all(db):
    global _last_run
    if not _lock.acquire(blocking=False):
        return 0
    try:
        if deferral_reason(db):
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=int(settings_service.get(db, "x_scan_interval")))
        ids = [u.id for u in repo.Users.due_for_scan(db, cutoff, platform=platforms.X)]
        total = 0
        for identifier in ids:
            with session_scope() as work:
                if deferral_reason(work):
                    break
                user = work.get(models.User, identifier)
                if user:
                    total += scan_user(work, user)
        _last_run = datetime.now(timezone.utc)
        return total
    finally:
        _lock.release()
