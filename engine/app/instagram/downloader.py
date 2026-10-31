"""gallery-dl wrapper - download Instagram media to a local folder.

gallery-dl (not yt-dlp) is used because yt-dlp's Instagram extractor refuses
image posts. gallery-dl downloads photos, carousels, reels, videos and stories,
reading the same Netscape cookies.txt. We run it into a caller-owned temp dir,
then hand back the media files it produced (the caller moves + records them).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from contextlib import closing
from pathlib import Path

from ..core.logging_config import get_logger

log = get_logger("instagram.downloader")


def archive_item(archive: Path, item: IGItem) -> None:
    """Acknowledge media only after the application has stored it successfully."""
    media_id = str(item.meta.get("media_id") or item.video_id.rsplit("_", 1)[-1])
    if not media_id.isdigit():
        raise ValueError("Instagram returned an invalid media id.")
    archive_media_ids(archive, [media_id])


def archive_media_ids(archive: Path, media_ids) -> None:
    """Seed duplicate prevention from durable local/cloud download history."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(archive, timeout=60)) as connection, connection:
        connection.execute("CREATE TABLE IF NOT EXISTS archive (entry TEXT PRIMARY KEY) WITHOUT ROWID")
        connection.executemany("INSERT OR IGNORE INTO archive(entry) VALUES (?)",
                               (("instagram" + str(value),) for value in media_ids if str(value).isdigit()))

_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}


@dataclass
class IGItem:
    username: str
    video_id: str   # gallery-dl's filename stem (unique per media item)
    path: Path
    is_video: bool
    # Whatever gallery-dl recorded about the post. Empty when the sidecar was
    # missing or unreadable, which every consumer has to cope with anyway.
    meta: dict = field(default_factory=dict)


class DownloadStopped(Exception):
    """A queued Instagram profile was paused or cancelled by its owner."""
    def __init__(self, status: str):
        self.status = status
        super().__init__(status)


def _sidecar(media: Path) -> dict:
    """The metadata gallery-dl wrote for this file.

    It names the sidecar after the whole filename (`a.mp4` -> `a.mp4.json`),
    but the plain-stem form is checked too so a change of convention does not
    silently drop every caption.
    """
    for candidate in (media.with_suffix(media.suffix + ".json"),
                      media.with_suffix(".json")):
        try:
            if candidate.is_file():
                return json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            log.debug("unreadable metadata for %s: %s", media.name, exc)
    return {}


def is_available() -> bool:
    try:
        subprocess.run([sys.executable, "-m", "gallery_dl", "--version"],
                       capture_output=True, check=True)
        return True
    except Exception:
        return False


def _reason(proc: subprocess.CompletedProcess) -> str:
    for line in reversed((proc.stderr or "").splitlines()):
        line = line.strip()
        if line:
            return line[:300]
    return "gallery-dl produced no media (login required, private, or unsupported URL)"


def probe_profile(url: str, cookies: Path | None, *, sleep: float = 2.0,
                  timeout: int = 30) -> tuple[dict | None, str]:
    """Resolve a profile against Instagram itself. Returns (profile, error).

    `profile` carries what Instagram says the account is - its canonical
    username, display name, numeric id and profile picture - so the caller can
    show the actual account rather than echoing back what was typed.

    The `/avatar/` endpoint is used because it is the only one that answers for
    every account. The bare profile URL makes no request at all (gallery-dl's
    `user` extractor just expands it), `/posts/` and `/reels/` are currently
    redirected to the home page by Instagram for everyone, and `/highlights/`
    and `/stories/` return nothing for an account that has none - which is not
    the same as the account not existing.

    Hard-bounded: a person is watching a spinner on the other end of this.
    """
    target = url.rstrip("/") + "/avatar/"
    cmd = [sys.executable, "-m", "app.instagram.gallery", "--ignore-config", "-j", "--quiet", "--retries", "0",
           "--sleep-request", str(sleep)]
    if cookies and cookies.exists():
        cmd += ["--cookies", str(cookies)]
    cmd.append(target)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"Instagram did not answer within {timeout}s."
    except Exception as exc:
        return None, str(exc)

    # In JSON mode gallery-dl can exit successfully with an error record on
    # stdout. Its user lookup also masks failed search/web requests as
    # NotFoundError, so that exception does not confirm a missing account.
    if proc.returncode != 0 or _json_errors(proc.stdout):
        reason = _classify(proc)
        return None, "lookup-failed" if reason == "not-found" else reason

    profile = _profile_from_json(proc.stdout)
    if profile is None:
        return None, "no-profile"
    return profile, ""


def _profile_from_json(raw: str) -> dict | None:
    """Pull the account's own details out of gallery-dl's JSON dump.

    The dump is a list of [kind, ...] entries whose dicts all carry the user
    fields; the first one that has a username is enough.
    """
    try:
        data = json.loads(raw or "")
    except Exception:
        return None

    found: dict = {}

    def walk(node) -> None:
        if isinstance(node, dict):
            if "username" in node and "username" not in found:
                found["username"] = str(node.get("username") or "")
                found["fullname"] = str(node.get("fullname") or "")
                found["owner_id"] = str(node.get("owner_id") or "")
            if "display_url" in node and "avatar" not in found:
                found["avatar"] = str(node.get("display_url") or "")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return found if found.get("username") else None


def _json_errors(raw: str) -> list[str]:
    """Read gallery-dl's [-1, {error, message}] JSON error records."""
    try:
        records = json.loads(raw or "")
    except (ValueError, TypeError):
        return []
    if not isinstance(records, list):
        return []
    errors = []
    for record in records:
        if (isinstance(record, list) and len(record) >= 2
                and record[0] == -1 and isinstance(record[1], dict)):
            error = record[1]
            errors.append(" ".join(str(error.get(key) or "") for key in ("error", "message")).strip())
    return errors


def _classify(proc: subprocess.CompletedProcess) -> str:
    """Turn gallery-dl's failure into something worth showing a person.

    The distinction that matters is between "this account is not there" and
    "we are not signed in", because they need completely different actions and
    Instagram reports the second as a redirect rather than an error.
    """
    diagnostics = "\n".join([proc.stderr or "", *_json_errors(proc.stdout)])
    err = diagnostics.lower()
    # A wrapper may append NotFoundError after a sign-in or rate-limit error.
    # Keep the actionable cause instead of declaring the handle missing.
    if any(token in err for token in ("429", "rate limit", "ratelimit", "too many requests", "please wait")):
        return "rate-limited"
    if any(token in err for token in ("redirect to home", "login", "authenticat", "challenge", "checkpoint")):
        return "signed-out"
    if "private" in err:
        return "private"
    if "notfound" in err or "could not be found" in err:
        return "not-found"
    return diagnostics.strip()[-300:] or _reason(proc)


def _collect(base_dir: Path) -> list[IGItem]:
    """Media files under <base_dir>/instagram/<username>/…  (gallery-dl's layout)."""
    items: list[IGItem] = []
    for f in base_dir.rglob("*"):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext not in _VIDEO_EXTS and ext not in _IMAGE_EXTS:
            continue
        items.append(IGItem(
            username=f.parent.name,   # …/instagram/<username>/<file>
            video_id=f.stem,
            path=f,
            is_video=ext in _VIDEO_EXTS,
            meta=_sidecar(f),
        ))
    return items


# How often the temp folder is checked for newly finished files while
# gallery-dl is still running. Cheap: one listing of a folder holding at most a
# few dozen entries.
_POLL_SECS = 0.5


def _ready(path: Path, sizes: dict[Path, int], waited: dict[Path, int]) -> bool:
    """True once a file has stopped growing.

    gallery-dl downloads to `<name>.part` and renames on completion, so a file
    with a media extension is normally already whole. The size check is a second
    line of defence: handing a half-written file to ffmpeg or to Telegram
    produces a corrupt result that looks like a download bug.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size == 0:
        return False

    # The sidecar is written after the file it describes, so its presence is
    # proof the file is finished - no waiting required. Taking it as sufficient
    # is what keeps detection off the critical path: requiring a stable size
    # across two polls as well added up to a second per file, which is the
    # difference between recording overlapping the next download and trailing
    # behind it.
    if _sidecar(path):
        return True

    previous = sizes.get(path)
    sizes[path] = size
    if previous is None or previous != size:
        return False
    # No sidecar and the size has settled. One more poll for it to appear, then
    # the item goes anyway rather than stalling the download over a caption.
    if waited.get(path, 0) >= 1:
        return True
    waited[path] = waited.get(path, 0) + 1
    return False


def run(
    url: str, cookies: Path | None, base_dir: Path,
    *, limit: int = 0, sleep: float = 2.0, timeout: int = 900,
    on_item=None, archive: Path | None = None, check_stop=None,
) -> tuple[list[IGItem], str]:
    """Download `url` into `base_dir` with gallery-dl. Returns (items, error).
    `error` is '' on success (items found).

    With `on_item`, each file is handed over as soon as it finishes rather than
    at the end of the run. A profile download takes six to eleven minutes for
    the posts alone, so waiting for the whole thing before showing anything left
    the person who pressed the button watching a bar with nothing behind it.
    """
    cmd = [
        sys.executable, "-m", "app.instagram.gallery",
        "--ignore-config",
        "--sleep-request", str(sleep),
        "--dest", str(base_dir),
        "--quiet",
        # Writes <file>.json beside each download, carrying the creator's real
        # name, the post caption, likes, dimensions and whether it was a story
        # or a highlight. None of that is recoverable from the filename, which
        # is all this used to read.
        "--write-metadata",
    ]
    if archive:
        # Records what has already been fetched and skips it WITHOUT downloading
        # it again. This is what makes monitoring cheap: a sweep that finds
        # nothing new transfers nothing, rather than pulling the same posts
        # every interval and discarding them after the fact.
        #
        # Only monitoring passes one. An explicit link must always download, or
        # asking for a post a second time would answer "no media found".
        archive.parent.mkdir(parents=True, exist_ok=True)
        cmd += ["--download-archive", str(archive)]
        if on_item is not None:
            # gallery-dl can finish a file before our database/filesystem write
            # succeeds. Keep archive reads, but acknowledge writes ourselves.
            cmd += ["-o", "extractor.instagram.archive-event=[]"]
    if cookies and cookies.exists():
        cmd += ["--cookies", str(cookies)]
    if limit and limit > 0:
        cmd += ["--range", f"1-{limit}"]
    cmd.append(url)

    # Debug, not info: a sweep makes one of these per phase per creator, so at
    # a hundred creators this alone is three hundred lines an interval.
    log.debug("gallery-dl %s (limit=%s)", url, limit or "all")
    if on_item is None:
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return [], f"gallery-dl timed out after {timeout}s"
        items = _collect(base_dir)
        return (items, "") if items else ([], _reason(proc))

    return _run_streaming(cmd, base_dir, timeout=timeout, on_item=on_item, archive=archive, check_stop=check_stop)


def _run_streaming(cmd, base_dir: Path, *, timeout: int, on_item, archive=None, check_stop=None):
    """Run gallery-dl and hand each finished file over while it still runs."""
    delivered: dict[Path, IGItem] = {}
    failed: dict[Path, str] = {}
    sizes: dict[Path, int] = {}
    waited: dict[Path, int] = {}
    deadline = time.monotonic() + timeout

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except Exception as exc:
        return [], str(exc)

    errors = deque(maxlen=30)
    def drain_errors():
        if proc.stderr is not None:
            for line in proc.stderr:
                errors.append(line)
    reader = threading.Thread(target=drain_errors, name="instagram-output", daemon=True)
    reader.start()

    def sweep() -> bool:
        """Hand over every finished file. True when it found any."""
        found = False
        for item in _collect(base_dir):
            if item.path in delivered or item.path in failed or not _ready(item.path, sizes, waited):
                continue
            found = True
            try:
                on_item(item)
                if archive is not None:
                    archive_item(archive, item)
                delivered[item.path] = item
            except Exception as exc:
                # A consumer that throws must not kill the download that is
                # still producing files for it.
                log.warning("instagram item handler failed for %s: %s", item.path.name, exc)
                failed[item.path] = str(exc)
        return found

    try:
        while proc.poll() is None:
            if check_stop:
                check_stop()
            if time.monotonic() > deadline:
                proc.kill()
                sweep()
                return list(delivered.values()), f"gallery-dl timed out after {timeout}s"
            # Only wait when there was nothing to do. Sleeping between every
            # file added the poll interval to each one, so storing a file cost
            # its own time plus a wait, and the run ended up as slow as if the
            # two had never overlapped at all.
            if not sweep():
                time.sleep(_POLL_SECS)
    except BaseException:
        proc.kill()
        raise
    finally:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        reader.join(timeout=5)
        stderr = "".join(errors)

    # One last pass: the final file usually lands between the last poll and the
    # process exiting.
    sweep()

    items = list(delivered.values())
    extraction_failed = bool(proc.returncode) or "[error]" in stderr.lower()
    if failed:
        return items, "Couldn't store Instagram media: " + next(iter(failed.values()))
    if items:
        if extraction_failed:
            fake = subprocess.CompletedProcess(cmd, proc.returncode, "", stderr or "")
            return items, _reason(fake)
        return items, ""
    # A clean exit with nothing to show is not a failure. It is the normal
    # result of a monitoring sweep where the download archive already holds
    # every post, and reporting it as an error would count a healthy creator
    # as failing until it was disabled as unreachable.
    if not extraction_failed:
        return [], ""
    fake = subprocess.CompletedProcess(cmd, proc.returncode, "", stderr or "")
    return [], _reason(fake)
