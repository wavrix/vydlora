"""Resolve device-local account profiles before the database is imported."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

SELECTOR = "active-profile.json"


def account_key(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def resolve_profile(root: Path) -> Path:
    root = root.resolve()
    try:
        document = json.loads((root / SELECTOR).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return root  # Existing installations stay exactly where they are.
    value = document.get("profile")
    if document.get("version") != 1 or not isinstance(value, str):
        raise ValueError("The active account profile could not be read.")
    if value != "." and value != "profiles/" + account_key(document.get("account", "")):
        raise ValueError("The active account profile is invalid.")
    target = (root / value).resolve()
    target.relative_to(root)  # Also rejects junctions pointing outside app data.
    return target


def activate(root: Path, target: Path, account: str) -> None:
    root, target = root.resolve(), target.resolve()
    value = target.relative_to(root).as_posix()
    expected = "profiles/" + account_key(account)
    if value not in (".", expected):
        raise ValueError("The account profile location is invalid.")
    temporary = root / (SELECTOR + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "profile": value, "account": account}), encoding="utf-8")
    os.replace(temporary, root / SELECTOR)
