"""Compatibility keys for existing profiles and client connections.

These are historical data/protocol identifiers, never engine display names.
They stay centralized here so a branding change cannot orphan saved media or
encrypted cloud snapshots.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

LEGACY_DATABASE = "wevdl.db"
LEGACY_DOWNLOAD_FOLDER = "WevDL"
CLOUD_SNAPSHOT_FOLDER = "/WevDL/.app-data/v1"
LEGACY_TOKEN_HEADER = b"x-solstice-token"
LEGACY_TOKEN_COOKIE = "solstice_token"

_ENVIRONMENT_ALIASES = {
    "LORA1_DATA_DIR": "SOLSTICE_DATA_DIR",
    "LORA1_HOST": "SOLSTICE_HOST",
    "LORA1_PORT": "SOLSTICE_PORT",
    "LORA1_AUTH_TOKEN": "SOLSTICE_AUTH_TOKEN",
    "LORA1_FRONTEND_DIR": "SOLSTICE_FRONTEND_DIR",
    "LORA1_PROFILES_ROOT": "WEVDL_PROFILES_ROOT",
    "LORA1_MANAGED": "WEVDL_DESKTOP_MANAGED",
    "LORA1_CLOUD_SYNC_DISABLED": "WEVDL_CLOUD_SYNC_DISABLED",
}


def apply_environment_aliases() -> None:
    for current, legacy in _ENVIRONMENT_ALIASES.items():
        if current not in os.environ and legacy in os.environ:
            os.environ[current] = os.environ[legacy]


def upgrade_profile_database(directory: Path) -> Path:
    """Copy a historical SQLite profile using its backup API; keep the source."""
    target = directory / "lora1.db"
    legacy = directory / LEGACY_DATABASE
    if target.exists() or not legacy.is_file():
        return target
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "lora1-migration.db"
    try:
        with closing(sqlite3.connect(legacy.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("The saved profile could not be verified.")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
