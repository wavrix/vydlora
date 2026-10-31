"""Store downloaded media in MEGA instead of on this machine.

MEGA publishes an SDK, but only as C++ with bindings for Java/PHP/.NET - there
is no Python binding on PyPI (`megasdk`, `mega-sdk`, `megasdkpy` do not exist;
the one package that does, `mega.py`, is an unofficial and unmaintained wrapper
around the old web API). So we drive MEGAcmd, which is MEGA's own command-line
client built on that same SDK: the official code path, reached the way the app
already reaches yt-dlp.

MEGAcmd is a client/server pair. `MEGAclient.exe` sends a command to a
background `MEGAcmdServer.exe` that owns the session and the transfers, so the
session survives between calls and a login is only needed once. That server is
single-session, so every call here is serialized behind one lock.

Credentials are never stored. A login uses the password once, in memory, then
keeps MEGAcmd's own *session token* instead - which is what MEGA itself hands
out for exactly this, can be revoked from the account's session list without a
password change, and is useless for signing into the website.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from sqlalchemy.orm import Session

from .. import repositories as repo
from ..core.logging_config import get_logger

log = get_logger("mega")

# MEGAcmd exit codes we act on by name rather than by parsing English text.
RC_OK = 0
RC_NOT_LOGGED_IN = 57

# One MEGAcmd server, one session: concurrent commands race over the same state,
# and `login` in particular is not safe to run twice at once.
_lock = threading.RLock()

# Where MEGAcmd puts itself on Windows. The installer is per-user (LOCALAPPDATA);
# the other two cover a machine-wide install. On Linux and macOS the package
# manager puts `mega-exec` on PATH, which the PATH search below picks up, so
# these Windows-only paths simply miss and cost nothing.
_CLIENT_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "MEGAcmd" / "MEGAclient.exe",
    Path(os.environ.get("ProgramFiles", "")) / "MEGAcmd" / "MEGAclient.exe",
    Path(os.environ.get("ProgramFiles(x86)", "")) / "MEGAcmd" / "MEGAclient.exe",
)

# Settings keys. The session token is a credential, so it is write-only over the
# API - `state()` reports whether one exists, never what it is.
SESSION_KEY = "mega_session"
BOUND_ACCOUNT_KEY = "_cloud_account"
ENABLED_KEY = "mega_enabled"
FOLDER_KEY = "mega_folder"
DEFAULT_FOLDER = "/Vydlora"


class MegaError(RuntimeError):
    """A MEGAcmd command failed. The message is meant for the user."""


@dataclass(frozen=True)
class Result:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == RC_OK

    @property
    def message(self) -> str:
        """The useful half of MEGAcmd's output: it prefixes errors with a
        timestamp and a level, which is noise in anything shown to a person."""
        raw = (self.err or self.out or "").strip()
        cleaned = re.sub(r"\[[\d_.\-]+ \w+ \w+\s*", "", raw).rstrip("]").strip()
        return cleaned or f"MEGAcmd exited with code {self.rc}"


# ── locating and running MEGAcmd ──
def client_path() -> Path | None:
    for candidate in _CLIENT_CANDIDATES:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    # MEGAcmd names its client differently per platform: MEGAclient.exe on
    # Windows, mega-exec on Linux and macOS. Both take the same verbs, so the
    # rest of this module does not care which one it found.
    for name in ("MEGAclient", "mega-exec"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def is_installed() -> bool:
    return client_path() is not None


# Paths are passed exactly as they are, never quoted.
#
# MEGAcmd's own documentation tells you to quote paths containing spaces, which
# is advice for its interactive shell. Through MEGAclient's argv the opposite
# holds: arguments already arrive whole, so added quotes become part of the path
# and the command fails with "Invalid argument". Verified against MEGAcmd 2.6.0
# with `lcd` into a folder named `Some Name - (@handle)`: unquoted succeeds,
# quoted fails. It matters because every creator folder has spaces in it.

def _run(args: list[str], *, timeout: float = 120, secret: bool = False) -> Result:
    """Run one MEGAcmd command. Never raises for a command that merely failed."""
    exe = client_path()
    if not exe:
        return Result(127, "", "MEGAcmd is not installed")
    with _lock:
        try:
            # A cold MEGAclient starts a persistent server which can inherit
            # its output handles. Pipes then never reach EOF, and subprocess.run
            # can hang even while cleaning up a timeout on Windows. Files let
            # us wait for the client alone and read its completed output.
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                proc = subprocess.run(
                    [str(exe), *args], stdout=stdout, stderr=stderr,
                    # No interactive password/confirmation prompts.
                    stdin=subprocess.DEVNULL,
                    timeout=timeout,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                stdout.seek(0)
                stderr.seek(0)
                out = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return Result(1, "", f"MEGAcmd timed out after {timeout:.0f}s")
        except Exception as exc:
            return Result(1, "", str(exc))
    result = Result(proc.returncode, out, err)
    if not result.ok:
        # A password must never reach the log, so a secret command names only
        # its verb.
        shown = args[0] if secret else " ".join(args)
        log.debug("mega %s -> rc=%s %s", shown, result.rc, result.message)
    return result


# ── session ──
# Asking MEGAcmd who is signed in means spawning a process and waiting on its
# server. That is a few hundred milliseconds idle, but seconds while downloads
# are saturating the machine - and the Settings pages ask on every navigation,
# which put a multi-second stall in front of tab switches and, worse, held the
# UI's busy flag long enough to silently swallow clicks on other settings.
#
# The answer changes only when this app signs in or out, both of which clear the
# cache below, so a short TTL costs nothing and takes the process spawn off
# every read. A sign-in performed outside the app is picked up within the TTL.
_WHOAMI_TTL = 30.0
_whoami_cache: tuple[float, str] | None = None


def _invalidate_session_cache() -> None:
    global _whoami_cache
    _whoami_cache = None
    _invalidate_quota_cache()


def whoami(*, cached: bool = True) -> str:
    """The logged-in account's email, or "" when there is no session."""
    global _whoami_cache
    if cached and _whoami_cache and (time.monotonic() - _whoami_cache[0]) < _WHOAMI_TTL:
        return _whoami_cache[1]
    account = _whoami_uncached()
    _whoami_cache = (time.monotonic(), account)
    return account


def _whoami_uncached() -> str:
    result = _run(["whoami"], timeout=30)
    if result.rc == RC_NOT_LOGGED_IN:
        return ""            # the ordinary "not connected yet" answer
    if not result.ok:
        # Anything else means MEGAcmd itself is unwell, which is worth saying:
        # it looks identical to being signed out and is fixed very differently.
        log.warning("MEGAcmd could not report the session: %s", result.message)
        return ""
    # "Account e-mail: name@example.com" - take what follows the colon.
    text = (result.out or "").strip()
    _, _, tail = text.partition(":")
    return (tail or text).strip()


def _session_token() -> str:
    result = _run(["session"], timeout=30)
    if not result.ok:
        return ""
    # The token is the last long field of "Your (secret) session is: <token>".
    # Matching on shape rather than on the sentence keeps this working if the
    # wording changes.
    for candidate in reversed((result.out or "").split()):
        if len(candidate) > 40:
            return candidate.strip()
    return ""


def restore_session(token: str) -> None:
    """Restore a preserved session after a cancelled/failed account switch."""
    with _lock:
        _run(["logout", "--keep-session"], timeout=60)
        _invalidate_session_cache()
        if token and not _run(["login", token], timeout=120, secret=True).ok:
            log.warning("The previous MEGA session needs reconnecting.")
        _invalidate_session_cache()


def authenticate(email: str, password: str) -> tuple[str, str]:
    """Authenticate without ever writing a session into the running profile."""
    email = (email or "").strip()
    if not email or not password:
        raise MegaError("Enter both the MEGA email and password.")
    if not is_installed():
        raise MegaError("MEGAcmd is not installed on this machine.")
    with _lock:
        connected = whoami(cached=False).strip().lower()
        previous = _session_token() if connected else ""
        if connected == email.lower() and previous:
            return connected, previous  # Includes a sign-in completed with 2FA in MEGAcmd.
        # Saved profiles must keep valid sessions when another account opens.
        _run(["logout", "--keep-session"], timeout=60)
        _invalidate_session_cache()
        try:
            result = _run(["login", email, password], timeout=120, secret=True)
            if not result.ok:
                text = result.message.lower()
                if "two" in text or "2fa" in text or "factor" in text:
                    raise MegaError("This MEGA account requires two-factor authentication. Complete sign-in with MEGAcmd first.")
                # MEGAcmd may repeat command arguments in errors. Never return
                # its raw login output, which could contain the password.
                raise MegaError("Could not sign in to MEGA. Check your email, password, and connection.")
            account = whoami(cached=False).strip().lower()
            token = _session_token()
            if not account or account != email.lower() or not token:
                raise MegaError("MEGA could not verify the new session. Try connecting again.")
            return account, token
        except Exception:
            restore_session(previous)
            raise


def login(db: Session, email: str, password: str) -> str:
    """Log MEGAcmd into an account and remember the session token.

    The password is used for this one call and never written anywhere. Returns
    the account email on success; raises MegaError with a user-facing message.
    """
    with _lock:
        account, token = authenticate(email, password)
        repo.Settings.set(db, SESSION_KEY, token)
        repo.Settings.set(db, BOUND_ACCOUNT_KEY, account)
        db.commit()
    log.info("MEGA session established for %s", account)
    return account


def logout(db: Session) -> None:
    with _lock:
        _run(["logout"], timeout=60)
        _invalidate_session_cache()
        repo.Settings.set(db, SESSION_KEY, "")
        repo.Settings.set(db, ENABLED_KEY, False)
        db.commit()
    log.info("MEGA session cleared")


def ensure_session(db: Session) -> bool:
    """Make sure MEGAcmd is logged in, restoring the stored session if needed.

    Called before every transfer rather than once at startup: MEGAcmd's server
    can be restarted or logged out from under us, and a download must not be
    lost because of it.
    """
    from .account_profile_service import switching
    if switching() or not is_installed():
        return False
    with _lock:
        bound = repo.Settings.get(db, BOUND_ACCOUNT_KEY, "")
        account = whoami(cached=not bool(bound))
        if account:
            return not bound or account.strip().lower() == bound
        token = (repo.Settings.get(db, SESSION_KEY, "") or "").strip()
        if not token:
            return False
        _invalidate_session_cache()
        result = _run(["login", token], timeout=120, secret=True)
        if not result.ok:
            log.warning("stored MEGA session no longer works: %s", result.message)
            return False
        log.info("restored MEGA session from stored token")
        return not bound or whoami(cached=False).strip().lower() == bound


# ── remote paths ──
def remote_root(db: Session) -> str:
    folder = (repo.Settings.get(db, FOLDER_KEY, DEFAULT_FOLDER) or DEFAULT_FOLDER).strip()
    if not folder.startswith("/"):
        folder = "/" + folder
    return folder.rstrip("/") or DEFAULT_FOLDER


def remote_dir_for(db: Session, local_dir: Path) -> str:
    """Mirror the local library layout under the MEGA root.

    The whole path below the downloads folder is reproduced, at whatever depth.
    This used to take the last two segments on the assumption that a path is
    always `<Platform>/<Creator>` - true for a post, wrong for anything nested
    under the creator. A story lives in `<Platform>/<Creator>/Stories`, so its
    last two segments are `<Creator>/Stories` and the platform folder was
    dropped: stories landed in `/Vydlora/<Creator>/Stories`, beside the platform
    folders rather than inside one.
    """
    parts: list[str] = []
    try:
        from . import settings_service

        base = Path(settings_service.downloads_path(db)).resolve()
        parts = [p for p in Path(local_dir).resolve().relative_to(base).parts if p]
    except Exception:
        # Saved before the downloads folder moved, or otherwise outside it:
        # fall back to the old shape rather than uploading to the account root.
        parts = [p for p in Path(local_dir).parts[-2:] if p]
    root = remote_root(db)
    return "/".join([root, *parts]) if parts else root


def is_enabled(db: Session) -> bool:
    return bool(repo.Settings.get(db, ENABLED_KEY, False))


# ── transfers ──
def _account_locked(function):
    @wraps(function)
    def call(*args, **kwargs):
        # Account validation and the transfer must use the same session. A
        # switch cannot slip into the gap between ensure_session and _run.
        with _lock:
            return function(*args, **kwargs)
    return call


@_account_locked
def upload(db: Session, local: Path, remote_dir: str) -> str:
    """Put a file or folder into MEGA. Returns the remote path it now occupies."""
    if not local.exists():
        raise MegaError(f"{local.name} is not on disk")
    if not ensure_session(db):
        raise MegaError("Not connected to MEGA.")

    # -c creates the destination folder; the trailing slash makes MEGAcmd treat
    # it as a directory to put INTO rather than a name to write AS, which is
    # what matters for the very first upload into a creator's folder.
    target = remote_dir.rstrip("/") + "/"
    # A large video can genuinely take a while on a slow uplink; the ceiling is
    # here to release a stuck worker, not to bound a normal transfer.
    result = _run(["put", "-c", str(local), target], timeout=3600)
    if not result.ok:
        raise MegaError(result.message)
    return f"{remote_dir.rstrip('/')}/{local.name}"


@_account_locked
def fetch(db: Session, remote: str, dest_dir: Path) -> Path | None:
    """Bring a stored file back to disk, into dest_dir. Returns the local path."""
    if not ensure_session(db):
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    result = _run(["get", remote, str(dest_dir)], timeout=3600)
    if not result.ok:
        log.warning("could not fetch %s: %s", remote, result.message)
        return None
    landed = dest_dir / remote.rstrip("/").rsplit("/", 1)[-1]
    return landed if landed.exists() else None


@_account_locked
def move(db: Session, src: str, dst: str) -> bool:
    """Move or rename something already in MEGA, without transferring bytes.

    MEGAcmd's `mv` renames when the destination does not exist and moves into
    it when it does, so `dst` must not already be there if a rename is what is
    wanted. Paths are passed unquoted, like every other command here.
    """
    if not ensure_session(db):
        return False
    result = _run(["mv", src, dst], timeout=120)
    if not result.ok:
        log.warning("could not move %s to %s: %s", src, dst, result.message)
    return result.ok


@_account_locked
def remove(db: Session, remote: str) -> bool:
    if not ensure_session(db):
        return False
    # -f skips the confirmation prompt, which has no console to answer it.
    return _run(["rm", "-rf", remote], timeout=120).ok


@_account_locked
def thumbnail(db: Session, remote: str, dest: Path) -> bool:
    """Save MEGA's own generated picture of a stored file to `dest`.

    MEGAcmd generates a thumbnail and a larger preview when it uploads a video,
    so the library keeps a picture of every post - including the private ones
    that have no public cover image anywhere. The preview is the bigger of the
    two and the one worth having; the thumbnail is the fallback.
    """
    if not ensure_session(db):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    for command in ("preview", "thumbnail"):
        if _run([command, remote, str(dest)], timeout=120).ok and dest.exists():
            return True
    return False


# ── account ──
# `mega-df` reports the used/total pair on one line, in raw bytes:
#
#     USED STORAGE:           2008268189                     9.35% of 21474836480
#
# An earlier version of this looked for "used storage"/"total storage" labels
# and a number carrying a unit suffix. Neither is what MEGAcmd prints, so it
# matched nothing and the quota silently read as zero forever. The suffix branch
# stays because `df -h` does print units, and costs nothing to keep.
_USED_RE = re.compile(r"USED\s+STORAGE:\s*([\d.,]+)\s*([KMGTP]?B|BYTES)?", re.I)
_TOTAL_RE = re.compile(r"(?:^|\s)of\s+([\d.,]+)\s*([KMGTP]?B|BYTES)?", re.I)
_UNITS = {None: 1, "": 1, "B": 1, "BYTES": 1, "KB": 1024, "MB": 1024**2,
          "GB": 1024**3, "TB": 1024**4, "PB": 1024**5}


def _as_bytes(number: str, unit: str | None) -> int:
    try:
        return int(float(number.replace(",", "")) * _UNITS[(unit or "").upper() or None])
    except (ValueError, KeyError):
        return 0


# Quota is another process spawn, and it was the rest of the Settings stall once
# the session probe was cached: ~350 ms on every navigation, for a number that
# moves only as fast as uploads finish. A minute of staleness is invisible here
# and takes the spawn off the page-load path entirely.
_QUOTA_TTL = 60.0
_quota_cache: tuple[float, tuple[int, int]] | None = None


def _invalidate_quota_cache() -> None:
    global _quota_cache
    _quota_cache = None


def storage(db: Session, *, cached: bool = True) -> tuple[int, int]:
    """(used, total) bytes in the MEGA account; (0, 0) when unavailable."""
    global _quota_cache
    if cached and _quota_cache and (time.monotonic() - _quota_cache[0]) < _QUOTA_TTL:
        return _quota_cache[1]
    used_total = _storage_uncached(db)
    # Only a real reading is worth remembering: caching a (0, 0) from a blip
    # would blank the quota for a minute for no reason.
    if used_total != (0, 0):
        _quota_cache = (time.monotonic(), used_total)
    return used_total


def _storage_uncached(db: Session) -> tuple[int, int]:
    if not ensure_session(db):
        return 0, 0
    result = _run(["df"], timeout=60)
    if not result.ok:
        return 0, 0
    used = total = 0
    for line in (result.out or "").splitlines():
        m = _USED_RE.search(line)
        if not m:
            continue
        used = _as_bytes(m.group(1), m.group(2))
        # The capacity sits on the same line, after the percentage.
        t = _TOTAL_RE.search(line[m.end():])
        if t:
            total = _as_bytes(t.group(1), t.group(2))
        break
    return used, total


def state(db: Session) -> dict:
    """Everything the Settings page needs. Never returns the session token.

    A transfer holds the command lock for as long as it runs (put/get carry a
    3600s timeout), and the Settings page polls this on every navigation. Letting
    a status read queue behind an upload stalled the page - and, because the app
    holds a busy flag across that call, silently blocked every other action on it,
    "Reset all data" included. So once the quota is cached, a live probe is only
    made when the lock is free and the cache is served during a transfer. The one
    exception is a cold cache: reporting a connected account with 0 storage is
    wrong, so the first reading waits (bounded) for the lock rather than falling
    back to nothing. The lock frees between files, so the wait catches a gap; the
    60s cache then serves every later poll instantly, transfer or not.
    """
    installed = is_installed()
    # Hold the lock across the whole probe rather than test-and-release: _lock is
    # re-entrant, so whoami()/storage() re-acquire it on this same thread without
    # blocking, and no transfer on another thread can slip in mid-probe.
    if installed:
        got = _lock.acquire(timeout=12) if _quota_cache is None else _lock.acquire(blocking=False)
    else:
        got = False
    if got:
        try:
            account = whoami()
            used, total = (storage(db) if account else (0, 0))
        finally:
            _lock.release()
    else:
        account = _whoami_cache[1] if (installed and _whoami_cache) else ""
        used, total = _quota_cache[1] if (installed and _quota_cache) else (0, 0)
    from .cloud_sync_service import sync
    return {
        "installed": installed,
        "connected": bool(account),
        "account": account,
        "enabled": is_enabled(db),
        "folder": remote_root(db),
        "has_session": bool((repo.Settings.get(db, SESSION_KEY, "") or "").strip()),
        "storage_used": used,
        "storage_total": total,
        "sync": sync.status(),
    }
