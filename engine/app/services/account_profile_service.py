"""Prepare an account's private store, then activate it on desktop restart.

The running engine never changes underneath workers or open API transactions.
The original installation is itself a profile; its files are never moved/deleted.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from .. import repositories as repo
from ..core.database import Base
from ..core.profiles import account_key, activate
from ..core.compat import upgrade_profile_database
from . import mega_service as mega

_pending = threading.Event()
_account = ""


def switching() -> bool:
    return _pending.is_set()


def bound_account(directory: Path) -> str:
    path = upgrade_profile_database(directory)
    if not path.exists():
        return ""
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (mega.BOUND_ACCOUNT_KEY,)).fetchone()
        return str(json.loads(row[0])).strip().lower() if row else ""


def prepare_switch(db: Session, email: str, password: str) -> dict:
    global _account
    from .cloud_sync_service import sync

    email = email.strip().lower()
    raw_root = os.environ.get("LORA1_PROFILES_ROOT")
    if not raw_root or os.environ.get("LORA1_MANAGED") != "1":
        raise mega.MegaError("Account switching needs a managed Lora 1 engine. Start Vydlora with npm start to enable it.")
    root = Path(raw_root).resolve()
    # Match sync's lock order; a snapshot/transfer must finish before login.
    with sync._lock, mega._lock:
        if switching():
            if email != _account:
                raise mega.MegaError("Restart the Lora 1 engine to finish the account switch first.")
            return {"restart_required": True, "account": _account}
        target = root if bound_account(root) == email else root / "profiles" / account_key(email)
        target = target.resolve()
        target.relative_to(root)
        existing = bound_account(target)
        if existing and existing != email:
            raise mega.MegaError("The saved profile belongs to a different account. No data was changed.")
        previous_account = mega.whoami(cached=False)
        previous_token = mega._session_token() if previous_account else ""
        if previous_account and not previous_token:
            raise mega.MegaError("MEGA could not preserve the current session. Try connecting again.")
        _pending.set()  # Pause new automation and transfers until restart.
        try:
            account, token = mega.authenticate(email, password)
            if account.strip().lower() != email:
                raise mega.MegaError("MEGA signed in to a different account. No profile was switched.")
            target.mkdir(parents=True, exist_ok=True)
            engine = create_engine("sqlite:///" + (target / "lora1.db").as_posix())
            try:
                Base.metadata.create_all(engine)
                with Session(engine) as destination:
                    # Keep all existing data/settings when revisiting an account.
                    repo.Settings.set(destination, mega.BOUND_ACCOUNT_KEY, email)
                    repo.Settings.set(destination, mega.SESSION_KEY, token)
                    destination.commit()
            finally:
                engine.dispose()
            activate(root, target, email)
        except Exception:
            # A bad password or a failed disk write must leave the old profile
            # usable, including its still-valid MEGA session.
            try:
                mega.restore_session(previous_token)
            finally:
                _pending.clear()
            raise
        _account = email
        return {"restart_required": True, "account": email}
