"""Copy an offline local profile into Vydlora without modifying the source.

Run only after stopping the source engine. Private runtime data stays ignored
by Git. SQLite backup includes committed WAL data and paths are relocated to
the copied media; table counts are verified before the import is accepted.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
from app.core.compat import LEGACY_DATABASE


def import_profile(source: Path, target: Path) -> dict:
    source, target = source.resolve(strict=True), target.resolve()
    target.relative_to((ROOT / ".runtime").resolve())
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("Use separate source and destination folders.")
    if target.exists() and any(target.iterdir()):
        raise ValueError("The destination must be empty; existing profiles are never overwritten.")
    shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.db", "*.db-wal", "*.db-shm"))
    report = {"databases": 0, "relocated_paths": 0, "rows": {}}
    source_prefix = str(source).replace("\\", "/").rstrip("/")

    def relocate(value):
        if not isinstance(value, str):
            return value
        normalized = value.replace("\\", "/")
        if normalized.casefold() == source_prefix.casefold():
            return str(target)
        if normalized.casefold().startswith(source_prefix.casefold() + "/"):
            return str(target / normalized[len(source_prefix) + 1:])
        return value

    for database in source.rglob("*.db"):
        # Prefer an already-upgraded active database over its historical backup.
        if database.name == LEGACY_DATABASE and database.with_name("lora1.db").exists():
            continue
        relative = database.relative_to(source)
        if relative.name == LEGACY_DATABASE:
            relative = relative.with_name("lora1.db")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as old:
            with closing(sqlite3.connect(destination)) as copied:
                old.backup(copied)
                tables = [r[0] for r in copied.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                for table in tables:
                    safe_table = '"' + table.replace('"', '""') + '"'
                    before = old.execute(f"SELECT COUNT(*) FROM {safe_table}").fetchone()[0]
                    columns = {r[1] for r in copied.execute(f"PRAGMA table_info({safe_table})")}
                    if "file_path" in columns and "id" in columns:
                        for identifier, value in copied.execute(f"SELECT id, file_path FROM {safe_table}").fetchall():
                            updated = relocate(value)
                            if updated != value:
                                copied.execute(f"UPDATE {safe_table} SET file_path=? WHERE id=?", (updated, identifier))
                                report["relocated_paths"] += 1
                    if table == "settings":
                        row = copied.execute("SELECT value FROM settings WHERE key='downloads_dir'").fetchone()
                        if row:
                            value = json.loads(row[0]); updated = relocate(value)
                            if updated != value:
                                copied.execute("UPDATE settings SET value=? WHERE key='downloads_dir'", (json.dumps(updated),))
                    after = copied.execute(f"SELECT COUNT(*) FROM {safe_table}").fetchone()[0]
                    if before != after:
                        raise RuntimeError("The imported profile failed its row-count verification.")
                    report["rows"][table] = report["rows"].get(table, 0) + after
                copied.commit()
                if copied.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("The imported database could not be verified.")
        report["databases"] += 1
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", type=Path, default=ROOT / ".runtime" / "lora1")
    args = parser.parse_args()
    print(json.dumps(import_profile(args.source, args.target), indent=2))
