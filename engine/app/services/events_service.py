"""Central event emitter - persists activity/log/notification and broadcasts over WS."""

from __future__ import annotations

import json
from typing import Optional

from sqlalchemy.orm import Session

from .. import repositories as repo
from ..core.events import publish
from ..models.enums import ActivityType, LogLevel, LogSource, NotificationType


def emit(
    db: Session,
    activity_type: ActivityType,
    message: str,
    *,
    meta: dict | None = None,
    user_id: int | None = None,
    log_level: LogLevel = LogLevel.info,
    log_source: LogSource = LogSource.system,
    notify: Optional[tuple[NotificationType, str, str]] = None,
) -> None:
    """Record an activity event + log line (+ optional notification) and push live."""
    ev = repo.Activity.add(db, activity_type, message, meta=meta, user_id=user_id)
    repo.Logs.add(db, log_level, log_source, message)
    notif = None
    if notify:
        ntype, title, body = notify
        notif = repo.Notifications.add(db, ntype, title, body)
    db.commit()

    publish("activity", {
        "id": ev.id,
        "type": activity_type.value,
        "message": message,
        "meta": json.loads(ev.meta) if ev.meta else None,
        "user_id": user_id,
        "created_at": ev.created_at.isoformat(),
    })
    if notif:
        publish("notification", {
            "id": notif.id,
            "type": notif.type.value,
            "title": notif.title,
            "body": notif.body,
            "created_at": notif.created_at.isoformat(),
        })


def log_only(db: Session, level: LogLevel, source: LogSource, message: str) -> None:
    repo.Logs.add(db, level, source, message)
    db.commit()
    publish("log", {"level": level.value, "source": source.value, "message": message})
