"""Runtime settings - DB-backed with config defaults; editable live."""

from __future__ import annotations

import random
import os
from pathlib import Path

from sqlalchemy.orm import Session

from .. import repositories as repo
from ..core.config import settings as cfg

# Key under which a user-chosen downloads directory override is stored. Kept out
# of DEFAULTS so it isn't surfaced by the generic /settings payload - it has its
# own validated endpoints (it must be a real, writable directory).
DOWNLOADS_DIR_KEY = "downloads_dir"

DEFAULTS = {
    "tikdownloader_max_interval": 30.0,
    "scan_interval": cfg.scan_interval,
    "scan_concurrency": cfg.scan_concurrency,
    "monitor_stories": cfg.monitor_stories,
    "story_scan_interval": cfg.story_scan_interval,
    "scan_spacing_min": cfg.scan_spacing_min,
    "scan_spacing_max": cfg.scan_spacing_max,
    "catch_up_enabled": cfg.catch_up_enabled,
    "download_workers": cfg.download_workers,
    "request_timeout": cfg.request_timeout,
    "max_posts_per_scan": cfg.max_posts_per_scan,
    "tikdownloader_min_interval": cfg.tikdownloader_min_interval,
    "tikdownloader_max_retries": cfg.tikdownloader_max_retries,
    "instagram_sleep": cfg.instagram_sleep,
    "instagram_monitor_enabled": cfg.instagram_monitor_enabled,
    "instagram_scan_interval": cfg.instagram_scan_interval,
    "instagram_scan_concurrency": cfg.instagram_scan_concurrency,
    "instagram_max_posts_per_scan": cfg.instagram_max_posts_per_scan,
    "x_monitor_enabled": False,
    "x_scan_interval": 900,
    "x_max_posts_per_scan": 10,
    "monitoring_paused": False,
    # Telegram automation toggles (read live by the telegram/download services).
    "telegram_send_new_posts": False,
    "telegram_upload_link_downloads": False,
    # Whether chats outside the approved list may send a TikTok link and get the
    # file back. Commands are never available to them either way.
    "telegram_public_downloads": True,
    # Base URL of a self-hosted Telegram Bot API server, e.g.
    # "http://127.0.0.1:8081/bot". Empty means Telegram's hosted API, which caps
    # bot uploads at 50 MB; a local server raises that to 2000 MB.
    "telegram_api_base_url": "",
    # Slot -> Telegram custom-emoji id, e.g. {"views": "5350…"}. Set it and the
    # caption draws that icon instead of the plain emoji; leave it empty and
    # nothing changes. Ids come from a message containing the icons - the bot
    # logs them on arrival.
    "telegram_icon_emoji": {},
    # ── MEGA cloud storage ──
    # On, a finished download is uploaded to MEGA and the local media deleted,
    # leaving only a poster image behind. Off, everything stays on this machine.
    # The account session itself is stored under `mega_session`, which is a
    # credential and deliberately not part of this payload.
    "mega_enabled": False,
    "mega_keep_local_copies": True,
    "mega_folder": "/Vydlora",
    # Shared owner of automatic scans and bot polling; manual work stays local.
    "automation_device": "",
}


def get_all(db: Session) -> dict:
    stored = repo.Settings.all(db)
    return {**DEFAULTS, **stored}


def get(db: Session, key: str):
    return repo.Settings.get(db, key, DEFAULTS.get(key))


def update(db: Session, values: dict) -> dict:
    for key, value in values.items():
        if value is not None and key in DEFAULTS:
            repo.Settings.set(db, key, value)
    db.commit()
    return get_all(db)


def spacing_delay(db: Session) -> float:
    """Seconds to pause before the next per-creator check - a random value in the
    configured [scan_spacing_min, scan_spacing_max] window (jitter avoids a
    fixed request cadence). Returns 0 when spacing is disabled."""
    lo = float(get(db, "scan_spacing_min") or 0)
    hi = float(get(db, "scan_spacing_max") or 0)
    if hi < lo:
        hi = lo
    if hi <= 0:
        return 0.0
    return random.uniform(lo, hi)


def ensure_defaults(db: Session) -> None:
    stored = repo.Settings.all(db)
    for key, value in DEFAULTS.items():
        if key not in stored:
            repo.Settings.set(db, key, value)
    db.commit()


# ── Managed download staging ──
def managed_downloads_path() -> Path:
    """Keep working media in app data, including files awaiting a cloud retry."""
    data = Path(os.environ.get("LORA1_DATA_DIR") or cfg.cookies_path.parent)
    return (data / "cache" / "downloads").resolve()


def downloads_path(db: Session) -> Path:
    """The private staging root; legacy Downloads overrides are migration inputs."""
    return managed_downloads_path()


def is_custom_downloads_dir(db: Session) -> bool:
    return False


def set_downloads_dir(db: Session, raw: str) -> Path:
    raise ValueError("Temporary download storage is managed automatically by Vydlora.")


def reset_downloads_dir(db: Session) -> Path:
    """Clear the override and fall back to the default directory."""
    repo.Settings.set(db, DOWNLOADS_DIR_KEY, "")
    db.commit()
    return managed_downloads_path()
