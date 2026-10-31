"""Cookie profile management + active-cookie resolution with rotation support."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from .. import repositories as repo
from ..core.config import settings as cfg
from ..core.logging_config import get_logger

log = get_logger("cookies")

# Cookies are sensitive, so they live next to the configured cookies file (the
# app-data dir), NOT under the downloads folder - which the user may relocate to
# e.g. ~/Downloads/Lora 1.
COOKIES_DIR = cfg.cookies_path.parent / "cookies"

# The cookie that proves a logged-in TikTok session. Without it (it's HttpOnly,
# so exporters often miss it, or the export was taken while logged out), TikTok
# treats every request as anonymous and never reveals private / followers-only
# posts - the #1 reason "my cookies don't work". `sessionid` is definitive;
# the others usually travel with it and are checked for a friendlier message.
LOGIN_COOKIE_PRIMARY = "sessionid"
LOGIN_COOKIE_NAMES = {"sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt", "uid_tt_ss"}


def _cookie_names(text: str) -> set[str]:
    """The set of cookie names present in a Netscape cookies.txt body."""
    names: set[str] = set()
    for raw in text.splitlines():
        line = raw[len("#HttpOnly_"):] if raw.startswith("#HttpOnly_") else raw
        line = line.rstrip()
        if line and not line.startswith("#"):
            parts = line.split("\t")
            if len(parts) == 7:
                names.add(parts[5])
    return names


def has_login_cookies(text: str) -> bool:
    """True when the file carries an authenticated TikTok session (`sessionid`)."""
    return LOGIN_COOKIE_PRIMARY in _cookie_names(text)


def profile_is_authenticated(path: Path) -> bool:
    """Whether the on-disk cookie file represents a logged-in session."""
    try:
        return has_login_cookies(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return False


def _count_cookie_lines(text: str) -> int:
    """Count valid Netscape cookie rows (7 tab-separated fields). Tolerates the
    `#HttpOnly_` prefix some exporters emit, and CRLF / stray whitespace."""
    n = 0
    for raw in text.splitlines():
        line = raw[len("#HttpOnly_"):] if raw.startswith("#HttpOnly_") else raw
        line = line.rstrip()
        if line and not line.startswith("#") and len(line.split("\t")) == 7:
            n += 1
    return n


def _count_cookies(path: Path) -> int:
    if not path.exists():
        return 0
    return _count_cookie_lines(path.read_text(encoding="utf-8", errors="ignore"))


def _discover_cookie_file() -> Path | None:
    """Find a usable cookies.txt even when no DB profile is registered.

    Order: the configured path, COOKIES_DIR/cookies.txt, then the most recently
    modified *.txt in COOKIES_DIR. This catches cookies dropped on disk or
    uploaded when the profile row went missing.
    """
    if cfg.cookies_path.exists():
        return cfg.cookies_path
    preferred = COOKIES_DIR / "cookies.txt"
    if preferred.exists():
        return preferred
    if COOKIES_DIR.exists():
        txts = sorted((p for p in COOKIES_DIR.glob("*.txt") if p.name not in {"instagram.txt", "x.txt"}), key=lambda p: p.stat().st_mtime, reverse=True)
        if txts:
            return txts[0]
    return None


def active_cookies_path(db: Session) -> Path:
    """Return the active cookie profile's file, with on-disk fallbacks."""
    profile = repo.Cookies.active(db)
    if profile:
        p = Path(profile.file_path)
        if p.exists():
            return p
    found = _discover_cookie_file()
    return found if found else cfg.cookies_path


def save_uploaded(db: Session, name: str, content: bytes, make_active: bool = True) -> "object":
    """Save (or replace) an uploaded cookies.txt and register/refresh its profile.

    Re-uploading a file with the same name UPDATES the existing profile rather
    than inserting a duplicate (the `name` column is unique - a plain insert here
    is what made "replace the cookies file" silently fail). The content is
    validated before the on-disk file is overwritten, so a malformed export can't
    clobber a working one. Raises ValueError with a user-facing message on a bad
    file.
    """
    # Validate the *uploaded* bytes before touching disk.
    text = content.decode("utf-8", errors="ignore") if content else ""
    count = _count_cookie_lines(text)
    if count == 0:
        raise ValueError(
            "No valid cookies found in that file. Export a Netscape-format "
            "cookies.txt (tab-separated, one cookie per line), e.g. with the "
            "“Get cookies.txt LOCALLY” browser extension, then try again."
        )

    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_", ".")) or "cookies.txt"
    dest = COOKIES_DIR / safe
    dest.write_bytes(content)

    existing = repo.Cookies.by_name(db, safe)
    if existing:
        existing.file_path = str(dest)
        existing.cookie_count = count
        existing.is_valid = True
        existing.rate_limit_hits = 0  # a fresh file gets a clean slate
        profile = existing
    else:
        profile = repo.Cookies.add(db, name=safe, file_path=str(dest), cookie_count=count, active=make_active)

    if make_active:
        repo.Cookies.set_active(db, profile.id)
    db.commit()
    authed = has_login_cookies(text)
    log.info(
        "Saved cookie profile '%s' (%d cookies, %s)%s",
        safe, count, "logged-in session" if authed else "NO login session (anonymous)",
        " [replaced]" if existing else "",
    )
    if not authed:
        log.warning(
            "Cookie file '%s' has no `sessionid`; TikTok will treat requests as "
            "logged-out, so private/followers-only posts won't be accessible. "
            "Re-export cookies.txt while signed in to TikTok.",
            safe,
        )
    return profile


def remove_cookie(db: Session, profile_id: int) -> bool:
    """Delete a cookie profile (the saved reference) and, when we own the file
    (it lives in COOKIES_DIR), the on-disk cookies.txt too. A user-selected file
    kept outside COOKIES_DIR is left in place - only the reference is removed.
    Returns True if a profile was found and removed."""
    path = repo.Cookies.remove(db, profile_id)
    db.commit()
    if path is None:
        return False
    try:
        p = Path(path)
        cookies_dir = COOKIES_DIR.resolve()
        if p.exists() and cookies_dir in p.resolve().parents:
            p.unlink()
            log.info("Removed cookie file %s", p)
    except Exception as exc:
        log.warning("could not delete cookie file %s: %s", path, exc)
    return True


# ── Instagram cookies (single dedicated file, separate from the TikTok jar) ──
# Instagram downloading (gallery-dl) is on-demand and unrelated to the TikTok
# monitoring cookies, so it gets its own file rather than a rotating profile.
INSTAGRAM_COOKIES_FILE = COOKIES_DIR / "instagram.txt"


def instagram_cookies_path() -> Path:
    return INSTAGRAM_COOKIES_FILE


def has_instagram_cookies() -> bool:
    return INSTAGRAM_COOKIES_FILE.exists() and _count_cookies(INSTAGRAM_COOKIES_FILE) > 0


def instagram_cookie_count() -> int:
    return _count_cookies(INSTAGRAM_COOKIES_FILE)


def save_instagram_cookies(content: bytes) -> int:
    """Validate and store an Instagram cookies.txt. Returns the cookie count.
    Raises ValueError on a malformed file (so a bad upload can't clobber a good one)."""
    text = content.decode("utf-8", errors="ignore") if content else ""
    count = _count_cookie_lines(text)
    if count == 0:
        raise ValueError(
            "No valid cookies found in that file. Export a Netscape-format "
            "cookies.txt for instagram.com (e.g. with the “Get cookies.txt LOCALLY” "
            "extension) while logged into your account, and try again."
        )
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    INSTAGRAM_COOKIES_FILE.write_bytes(content)
    log.info("Saved Instagram cookies (%d cookies)", count)
    return count


def remove_instagram_cookies() -> bool:
    try:
        if INSTAGRAM_COOKIES_FILE.exists():
            INSTAGRAM_COOKIES_FILE.unlink()
            log.info("Removed Instagram cookies")
            return True
    except Exception as exc:
        log.warning("could not remove Instagram cookies: %s", exc)
    return False


def x_cookies_path() -> Path:
    return COOKIES_DIR / "x.txt"


def x_cookie_status() -> dict:
    from ..x.void_media import cookie_status, read_cookies
    path = x_cookies_path()
    status = cookie_status(path)
    count = 0
    if status["available"]:
        try:
            count = len(read_cookies(path))
        except ValueError:
            pass
    return {"present": status["available"], "authenticated": status["authenticated"],
            "cookie_count": count, "file_path": str(path), "message": status["message"]}


def has_x_cookies() -> bool:
    return x_cookie_status()["authenticated"]


def save_x_cookies(content: bytes) -> int:
    """Validate before replacing; filter out other sites and preserve the source."""
    import os
    import tempfile
    from ..x.void_media import read_cookies
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="x-upload-", suffix=".txt", dir=COOKIES_DIR)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
        jar = read_cookies(temp)
        names = {c.name for c in jar if not c.is_expired()}
        if not {"auth_token", "ct0"} <= names:
            raise ValueError("Export X cookies while signed in. The file needs an unexpired auth_token and ct0.")
        jar.save(ignore_discard=True, ignore_expires=True)
        temp.replace(x_cookies_path())
        return len(jar)
    finally:
        temp.unlink(missing_ok=True)


def sync_default(db: Session) -> None:
    """Register any on-disk cookies file as a profile if none exist yet."""
    if repo.Cookies.list(db):
        return
    default = _discover_cookie_file()
    if default and default.exists():
        # A cloud deletion must survive startup even when an original cookie
        # export outside our managed folder was deliberately left on disk.
        from sqlalchemy import select
        from .. import models
        import json
        account = repo.Settings.get(db, "_cloud_account", "")
        deleted = db.scalar(select(models.CloudRecord).where(
            models.CloudRecord.account == account,
            models.CloudRecord.key == "cookies:" + default.name,
        )) if account else None
        if deleted and not json.loads(deleted.payload)["alive"]["v"]:
            return
        count = _count_cookies(default)
        profile = repo.Cookies.add(db, name=default.name, file_path=str(default), cookie_count=count, active=True)
        repo.Cookies.set_active(db, profile.id)
        db.commit()
        log.info("Auto-registered cookie profile '%s' (%d cookies) from %s", default.name, count, default)
