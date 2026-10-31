"""One-time migration to per-platform download folders.

Historically TikTok creators were saved flat in the downloads root
(``<downloads>/<creator>/``) while other platforms were namespaced. Every
platform now gets its own folder (``<downloads>/TikTok/<creator>/``), so this
moves any creator folder still sitting in the old location and rewrites the
stored ``file_path`` of affected downloads.

Why it matters: "already downloaded" is decided by looking for media inside the
creator's folder. Without moving the files, every previously-downloaded post
would look missing and be fetched again.

Idempotent and guarded by a settings flag, so it runs a single time. Failures are
per-creator and never block startup - a creator that can't be moved simply stays
where it is (and is still found, because its DB path is left untouched).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, platforms, repositories as repo
from ..core.logging_config import get_logger
from ..tiktok import naming
from . import settings_service

log = get_logger("layout")

MIGRATION_FLAG = "platform_folders_migrated"


def _folder_name(user: models.User) -> str:
    return user.folder_name or naming.account_folder_name(user.nickname or user.username, user.username)


def _same_location(a: Path, b: Path) -> bool:
    """True when both paths denote the same directory. Windows filesystems are
    case-insensitive, so ``Youtube`` and ``YouTube`` are one folder."""
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def _merge_into(src: Path, dst: Path) -> None:
    """Move the contents of `src` into an existing `dst`, keeping whatever is
    already there (never overwrite), then drop `src` if it ends up empty."""
    dst.mkdir(parents=True, exist_ok=True)
    for entry in list(src.iterdir()):
        target = dst / entry.name
        if target.exists():
            continue
        try:
            shutil.move(str(entry), str(target))
        except Exception as exc:
            log.warning("could not move %s: %s", entry, exc)
    try:
        src.rmdir()          # only succeeds when empty
    except OSError:
        pass


def _rewrite_paths(db: Session, user_id: int, old_dir: Path, new_dir: Path) -> int:
    """Point this creator's stored download paths at the new folder."""
    old_prefix = str(old_dir)
    rows = db.scalars(select(models.Download).where(models.Download.user_id == user_id)).all()
    changed = 0
    for row in rows:
        if not row.file_path:
            continue
        if os.path.normcase(row.file_path).startswith(os.path.normcase(old_prefix) + os.sep):
            row.file_path = str(new_dir) + row.file_path[len(old_prefix):]
            changed += 1
    return changed


def migrate(db: Session) -> dict:
    """Move every creator folder into its platform folder. Returns a summary."""
    base = settings_service.downloads_path(db)
    users = db.scalars(select(models.User)).all()

    moved = 0
    repathed = 0
    for user in users:
        try:
            platform = user.platform or platforms.TIKTOK
            folder = _folder_name(user)
            new_dir = base / platforms.display_name(platform) / folder

            # The legacy spot: flat in the root (TikTok), or the old
            # capitalise()-derived platform folder for everything else.
            candidates = [base / folder, base / platform.capitalize() / folder]
            for old_dir in candidates:
                if _same_location(old_dir, new_dir):
                    continue                       # already in the right place
                if not old_dir.is_dir():
                    continue

                if new_dir.exists():
                    _merge_into(old_dir, new_dir)
                else:
                    new_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old_dir), str(new_dir))

                repathed += _rewrite_paths(db, user.id, old_dir, new_dir)
                moved += 1
                log.info("[@%s] moved %s -> %s", user.username, old_dir.name, new_dir.parent.name)
        except Exception as exc:  # never let one creator stop the migration
            log.error("[@%s] folder migration failed: %s", user.username, exc)
            db.rollback()

    if moved:
        db.commit()
    log.info("platform-folder migration: %d folder(s) moved, %d path(s) rewritten", moved, repathed)
    return {"moved": moved, "repathed": repathed}


def migrate_once(db: Session) -> dict:
    """Run `migrate` a single time, guarded by a persisted flag."""
    if repo.Settings.get(db, MIGRATION_FLAG, None):
        return {"skipped": "already-done"}
    result = migrate(db)
    repo.Settings.set(db, MIGRATION_FLAG, True)
    db.commit()
    return result


# ── duplicate creator folders ──

def _folder_candidates(user: models.User) -> list[str]:
    """Every folder name this creator could have been filed under.

    A creator saved from a one-off link has no profile metadata, so its folder
    was built from the handle alone ("username - (@username)"). Once the same
    creator is monitored and a scan learns the nickname, the name it would build
    is different ("Nickname - (@username)"). Both are generated by the same
    helper, so both are reproducible here without guessing.
    """
    handle = user.username or ""
    names = [
        naming.account_folder_name(user.nickname or handle, handle),
        naming.account_folder_name(handle, handle),
    ]
    if user.folder_name:
        names.append(user.folder_name)
    # Preserve order, drop repeats (the common case: nickname == handle).
    seen: dict[str, None] = {}
    for name in names:
        if name:
            seen.setdefault(name, None)
    return list(seen)


def reconcile_creator_folders(db: Session) -> dict:
    """Fold any stray folder for a creator into the one their library points at.

    Files are moved rather than copied, existing files are never overwritten, and
    the stored `file_path` of every affected download is rewritten - so a merged
    creator keeps working without a re-download, which is the whole point:
    "already downloaded" is decided by looking inside the creator's folder.
    """
    base = settings_service.downloads_path(db)
    users = db.scalars(select(models.User)).all()

    merged = repathed = pinned = 0
    for user in users:
        try:
            platform = user.platform or platforms.TIKTOK
            parent = base / platforms.display_name(platform)

            # The folder the app will use from now on: whatever is pinned, else
            # the name its current metadata produces.
            keep = user.folder_name or _folder_name(user)
            if not user.folder_name:
                user.folder_name = keep      # pin it so this can't recur
                pinned += 1

            keep_dir = parent / keep
            for name in _folder_candidates(user):
                stray = parent / name
                if _same_location(stray, keep_dir) or not stray.is_dir():
                    continue

                if keep_dir.exists():
                    _merge_into(stray, keep_dir)
                else:
                    keep_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(stray), str(keep_dir))

                repathed += _rewrite_paths(db, user.id, stray, keep_dir)
                merged += 1
                log.info("[@%s] merged %s -> %s", user.username, name, keep)
        except Exception as exc:   # one bad creator must not stop the rest
            log.error("[@%s] folder reconcile failed: %s", user.username, exc)
            db.rollback()

    if merged or pinned:
        db.commit()
    log.info(
        "creator-folder reconcile: %d merged, %d path(s) rewritten, %d name(s) pinned",
        merged, repathed, pinned,
    )
    return {"merged": merged, "repathed": repathed, "pinned": pinned}


RECONCILE_FLAG = "creator_folders_reconciled"


def reconcile_once(db: Session) -> dict:
    """Run the reconcile a single time, guarded by a persisted flag. The bug that
    created the split folders is fixed at source, so this only has to clean up
    what it already produced."""
    if repo.Settings.get(db, RECONCILE_FLAG, None):
        return {"skipped": "already-done"}
    result = reconcile_creator_folders(db)
    repo.Settings.set(db, RECONCILE_FLAG, True)
    db.commit()
    return result
