"""Move legacy working folders into private app data without losing local files."""
from pathlib import Path
import os
import shutil

from sqlalchemy import select

from .. import models, platforms, repositories as repo
from ..core.logging_config import get_logger
from . import settings_service

log = get_logger("managed-storage")


def _linked(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _move(source: Path, destination: Path, source_root: Path, destination_root: Path) -> int:
    if _linked(source) or _linked(destination):
        raise OSError("Linked folders are left in place")
    source.resolve().relative_to(source_root)
    destination.resolve().relative_to(destination_root)
    if source.is_file() and not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return 1
    if not source.is_dir() or (destination.exists() and not destination.is_dir()):
        # A conflicting file may be the only copy of an old download.
        raise OSError("An existing working file has the same name; both copies are preserved")
    count = 0
    destination.mkdir(parents=True, exist_ok=True)
    for child in list(source.iterdir()):
        count += _move(child, destination / child.name, source_root, destination_root)
    source.rmdir()  # Empty only; never recursively delete a Downloads folder.
    return count


def migrate(db) -> dict:
    destination_root = settings_service.managed_downloads_path()
    default_legacy = Path.home() / "Downloads" / "Vydlora"
    raw = repo.Settings.get(db, settings_service.DOWNLOADS_DIR_KEY, "")
    # An isolated dev/test profile must never move the installed user's files.
    production_root = Path(os.environ.get("APPDATA", str(Path.home()))) / "Vydlora" / "cache" / "downloads"
    roots = [default_legacy] if destination_root == production_root.resolve() else []
    if raw:
        roots.append(Path(raw).expanduser())
    names = {"TikTok", "Instagram", ".cache"}
    names.update(platforms.display_name(user.platform) for user in db.scalars(select(models.User)))
    moved = repathed = 0
    seen = set()
    for candidate in roots:
        if _linked(candidate):
            continue
        old_root = candidate.resolve()
        if old_root in seen or old_root == destination_root or old_root in destination_root.parents or destination_root in old_root.parents:
            continue
        seen.add(old_root)
        for name in names:
            # A malformed stored platform must not turn into a traversal.
            if Path(name).name != name or name in (".", ".."):
                continue
            source = old_root / name
            if source.is_dir():
                try:
                    moved += _move(source, destination_root / name, old_root, destination_root)
                except (OSError, ValueError) as exc:
                    log.warning("A legacy working folder was left in place: %s", exc)
        for row in db.scalars(select(models.Download)):
            if not row.file_path:
                continue
            old = Path(row.file_path)
            try:
                relative = old.resolve().relative_to(old_root)
            except ValueError:
                continue
            target = destination_root / relative
            if relative.parts and relative.parts[0] in names and not old.exists() and (target.exists() or row.remote_path):
                row.file_path = str(target)
                repathed += 1
        # Remove only the now-empty former default folder. Cookies and other
        # user files in a custom folder are never moved or removed.
        if old_root == default_legacy.resolve():
            try:
                old_root.rmdir()
            except OSError:
                pass
    db.commit()
    if moved or repathed:
        log.info("Moved %d legacy working folder(s); updated %d download path(s)", moved, repathed)
    return {"moved": moved, "repathed": repathed}
