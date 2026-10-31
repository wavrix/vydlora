"""System-level operations: full reset to initial state."""

from __future__ import annotations

import shutil

from sqlalchemy.orm import Session

from .. import models
from .. import repositories as repo
from ..core.config import settings as cfg
from ..core.logging_config import get_logger
from ..models.enums import ActivityType, LogLevel, LogSource, NotificationType
from . import cookie_service, events_service, settings_service

log = get_logger("system")

# Children first so foreign keys are satisfied even with FKs enforced.
_CLEAR_ORDER = [
    models.Download,
    models.Post,
    models.QueueJob,
    models.ActivityEvent,
    models.TelegramMessage,
    models.LogEntry,
    models.Notification,
    models.CookieProfile,
    models.WorkerStatus,
    models.Setting,
    models.User,
]


def _wipe_downloads(root: Path) -> int:
    """Delete everything inside the downloads directory (keeps the dir itself)."""
    removed = 0
    if not root.exists():
        return 0
    for child in root.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            removed += 1
        except Exception as exc:  # pragma: no cover
            log.warning("could not remove %s: %s", child, exc)
    return removed


def reset(db: Session, wipe_files: bool = False) -> dict:
    """
    Clear all data and return the app to its initial state.

    Deletes every row from every table, resets settings to defaults, re-registers
    the default cookie profile, and (optionally) deletes downloaded media on disk.
    Live worker threads simply re-register themselves on their next heartbeat.
    """
    # Preserve the sync identity/journal so the reset publishes tombstones to
    # the same account instead of restoring the erased rows or switching owners.
    identity = {key: repo.Settings.get(db, key, "") for key in ("_cloud_account", "_cloud_device")}
    # Pause monitoring first so the scan loop doesn't repopulate mid-reset.
    settings_service.update(db, {"monitoring_paused": True})

    deleted: dict[str, int] = {}
    for model in _CLEAR_ORDER:
        count = db.query(model).delete(synchronize_session=False)
        deleted[model.__tablename__] = count
    db.commit()

    files_removed = _wipe_downloads(settings_service.downloads_path(db)) if wipe_files else 0

    # Re-seed initial state.
    settings_service.ensure_defaults(db)
    for key, value in identity.items():
        if value:
            repo.Settings.set(db, key, value)
    db.commit()
    cookie_service.sync_default(db)

    total_rows = sum(deleted.values())
    log.warning("SYSTEM RESET: cleared %d rows, wiped %d download folder(s)", total_rows, files_removed)

    # One fresh activity/log entry marking the reset.
    events_service.emit(
        db,
        ActivityType.worker_restart,
        "System reset: all data cleared",
        meta={"rows": total_rows, "files_removed": files_removed, "wiped_files": wipe_files},
        log_level=LogLevel.warning,
        log_source=LogSource.system,
        notify=(NotificationType.warning, "System reset", "All data has been cleared."),
    )

    return {"rows_deleted": total_rows, "tables": deleted, "files_removed": files_removed}
