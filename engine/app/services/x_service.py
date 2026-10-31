"""X downloads, shared by desktop, monitoring and Telegram."""
from __future__ import annotations
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy.orm import Session
from .. import models, platforms, repositories as repo
from ..models.enums import ActivityType, DownloadStatus, LogSource, PostType, UserStatus
from ..core.events import publish
from ..tiktok import naming
from ..x import profiles, urls, void_media
from . import cookie_service, events_service, settings_service, storage_service

_download_lock = threading.RLock()

def find_creator(db: Session, username: str):
    return repo.Users.by_username(db, urls.normalize_handle(username), platform=platforms.X)

def get_or_create_creator(db: Session, username: str):
    handle = urls.normalize_handle(username)
    user = find_creator(db, handle)
    if user is None:
        user = models.User(state_key=platforms.state_key(platforms.X, handle.lower()), platform=platforms.X,
                           username=handle, nickname=handle, monitored=False, status=UserStatus.active,
                           folder_name=naming.account_folder_name(handle, handle))
        db.add(user)
        db.flush()
    return user

def probe_creator(db: Session, username: str) -> tuple[dict | None, str]:
    try:
        handle = urls.normalize_handle(username)
        profile = profiles.probe(handle, cookie_service.x_cookies_path())
        if profile["username"].lower() != handle.lower():
            return None, "X returned a different account. Check the username."
        return profile, ""
    except Exception as exc:
        return None, str(exc)

def _update_profile(user, profile: dict) -> None:
    user.nickname = profile.get("fullname") or user.nickname
    user.avatar_url = profile.get("avatar") or user.avatar_url
    user.platform_uid = str(profile.get("owner_id") or user.platform_uid or "") or None
    if profile.get("followers") is not None:
        user.followers = int(profile["followers"])
    if profile.get("posts") is not None:
        user.total_posts = int(profile["posts"])

def set_monitoring(db: Session, username: str, monitored: bool):
    handle = urls.normalize_handle(username)
    profile, error = probe_creator(db, handle) if monitored else (None, "")
    if error == "not-found":
        raise ValueError("That X creator could not be found.")
    user = get_or_create_creator(db, handle)
    if profile:
        if user.platform_uid and profile.get("owner_id") and user.platform_uid != profile["owner_id"]:
            raise ValueError("This username now belongs to a different X account.")
        _update_profile(user, profile)
    user.monitored = bool(monitored)
    if monitored:
        user.enabled = True
        user.status = UserStatus.monitoring
        user.last_checked = None
        user.scan_failures = 0
        user.last_error = None
    db.commit()
    return user

def _existing_entry(db, user, identifier):
    from sqlalchemy import select
    record = db.scalar(select(models.Download).where(models.Download.user_id == user.id,
                        models.Download.video_id == identifier, models.Download.status == DownloadStatus.completed))
    if record and (record.remote_path or (record.file_path and Path(record.file_path).is_file())):
        return {"path": record.file_path or "", "post_type": record.post_type, "video_id": identifier,
                "username": user.username, "download_id": record.id, "title": record.title or "", "meta": {}}
    return None

def download_url(db: Session, url: str, *, source: str = "manual", limit: int | None = None,
                 on_item=None, use_archive: bool = False, expected_user=None, discovery=None, on_progress=None) -> dict:
    if urls.is_profile_url(url):
        return download_profile(db, urls.extract_username(url), source=source, limit=limit,
                                on_item=on_item, use_archive=use_archive, expected_user=expected_user, on_progress=on_progress)
    if not urls.post_id(url):
        return {"ok": False, "error": "Use an X profile or post link."}
    files, failures, creators, count = [], [], set(), 0
    # One recorder at a time prevents duplicate rows and concurrent overwrites
    # when a scheduled scan and a Telegram request target the same post.
    with _download_lock, tempfile.TemporaryDirectory(prefix="lora1-x-media-") as folder:
        try:
            post = (void_media.inspect_status(discovery, urls.post_id(url), cookie_path=cookie_service.x_cookies_path())
                    if discovery else void_media.inspect_post(urls.status_url(urls.post_id(url)), cookie_path=cookie_service.x_cookies_path()))
            handle = urls.normalize_handle(post["author"])
            user = get_or_create_creator(db, handle)
            profile = post.get("profile") or {}
            owner_id = str(profile.get("owner_id") or "")
            if expected_user and (handle.lower() != expected_user.username.lower() or
                    (expected_user.platform_uid and owner_id and expected_user.platform_uid != owner_id)):
                raise ValueError("The X account identity changed. Re-add this creator to confirm it.")
            _update_profile(user, profile)
            db.commit()
            creators.add(handle)
            account = settings_service.downloads_path(db) / "X" / (user.folder_name or handle)
            account.mkdir(parents=True, exist_ok=True)
            for item in post["media"]:
                identifier = f"{post['post_id']}_{item['id']}"
                existing = _existing_entry(db, user, identifier)
                if existing:
                    if not use_archive:
                        files.append(existing)
                        if on_item:
                            on_item(existing)
                    continue
                try:
                    report = void_media.download_media(post, item, Path(folder))
                    src = Path(folder) / report["filename"]
                    dest = account / src.name
                    shutil.move(str(src), str(dest))
                    kind = PostType.image if item["kind"] == "image" else PostType.video
                    record = repo.Downloads.add(db, user_id=user.id, video_id=identifier,
                        title=post["text"][:500], file_path=str(dest), file_size=dest.stat().st_size,
                        post_type=kind, strategy="void-x", status=DownloadStatus.completed, source=source)
                    user.last_download = datetime.now(timezone.utc)
                    db.commit()
                    storage_service.store_download(db, record, dest)
                    entry = {"path": str(dest), "post_type": kind, "video_id": identifier,
                             "username": handle, "download_id": record.id, "title": post["text"],
                             "meta": {"description": post["text"], "tweet_id": post["post_id"]}}
                    files.append(entry)
                    count += 1
                    if source == "monitor":
                        from . import telegram_service
                        telegram_service.announce_x_download(db, record, entry)
                    if on_item:
                        on_item(entry)
                except Exception as exc:
                    failures.append(f"Media {item['id']}: {exc}")
        except Exception as exc:
            return {"ok": False, "error": str(exc), "count": count, "files": files}
    if count:
        events_service.emit(db, ActivityType.download_completed, f"X: saved {count} item(s) from @{handle}",
                            user_id=user.id, log_source=LogSource.download)
    return {"ok": not failures or bool(files), "empty": not files and not failures,
            "count": count, "files": files, "creators": sorted(creators),
            "error": failures[0] if failures else "", "warning": "; ".join(failures),
            "errors": {"media": "; ".join(failures)} if failures else {}}

def download_profile(db: Session, username: str, *, source: str = "manual", limit: int | None = None,
                     on_item=None, use_archive: bool = False, expected_user=None, on_progress=None) -> dict:
    handle = urls.normalize_handle(username)
    cap = limit
    files, errors, count = [], {}, 0
    identifiers = profiles.iter_posts(handle, cookie_service.x_cookies_path(), cap)
    processed = 0
    def progress(phase):
        data = {"phase": phase, "username": handle, "platform": platforms.X, "count": count, "posts": processed}
        publish("x_download", data)
        if on_progress:
            try:
                on_progress(data)
            except Exception:
                pass  # A disconnected UI must not stop saving media.
    progress("start")
    try:
        for identifier in identifiers:
            progress("downloading")
            result = download_url(db, urls.status_url(identifier), source=source, on_item=on_item,
                                  use_archive=use_archive, expected_user=expected_user,
                                  discovery=getattr(identifier, "status", None))
            files.extend(result.get("files") or [])
            count += int(result.get("count") or 0)
            processed += 1
            if not result.get("ok") or result.get("warning"):
                errors[str(identifier)] = result.get("warning") or result.get("error") or "Download failed."
            progress("saved")
    except Exception as exc:
        errors["listing"] = str(exc)
    finally:
        close = getattr(identifiers, "close", None)
        if close:
            close()
        progress("done")
    return {"ok": not errors or bool(files), "empty": not files and not errors,
            "count": count, "files": files, "creators": [handle], "errors": errors,
            "error": next(iter(errors.values()), ""),
            "detail": f"Latest {cap} media posts" if cap is not None else "All available media posts", "limit": cap}
