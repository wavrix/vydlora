"""gallery-dl profile discovery; media downloads use the Void adapter."""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import queue
import threading
from pathlib import Path
from . import urls

class DiscoveredPost(str):
    def __new__(cls, identifier: str, status=None):
        value = super().__new__(cls, identifier)
        value.status = status
        return value

def _iter_rows(target: str, cookies: Path | None, limit: int | None = None):
    from .void_media import read_cookies
    with tempfile.TemporaryDirectory(prefix="lora1-x-list-") as folder:
        cmd = [sys.executable, "-m", "app.x.gallery", "--ignore-config", "--dump-json", "--quiet",
               "--sleep-request", "2", "-o", "retries=1", "-o", "timeout=25",
               "-o", "extractor.twitter.retweets=false", "-o", "extractor.twitter.ratelimit=abort",
               "-o", "output.jsonl=true", "-o", "output.private=true"]
        if cookies and cookies.is_file():
            copy = Path(folder) / "cookies.txt"
            jar = read_cookies(cookies)
            jar.filename = str(copy)
            jar.save(ignore_discard=True, ignore_expires=True)
            cmd += ["--cookies", str(copy)]
        if limit is not None:
            cmd += ["--post-range", f"1-{max(1, limit)}"]
        cmd.append(target)
        messages = queue.Queue(maxsize=16)
        stopped = threading.Event()
        with (Path(folder) / "stderr.txt").open("w+", encoding="utf-8") as errors:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors, text=True,
                                    encoding="utf-8", errors="replace", bufsize=1,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            def enqueue(value):
                while not stopped.is_set():
                    try:
                        messages.put(value, timeout=0.25)
                        return
                    except queue.Full:
                        pass
            def drain():
                try:
                    for line in proc.stdout:
                        if stopped.is_set():
                            break
                        enqueue(line)
                finally:
                    enqueue(None)
            reader = threading.Thread(target=drain, name="x-discovery-output", daemon=True)
            reader.start()
            try:
                while True:
                    try:
                        line = messages.get(timeout=90)
                    except queue.Empty:
                        raise ValueError("X stopped answering. Try Download again to continue.") from None
                    if line is None:
                        break
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        raise ValueError("X returned an unreadable response.") from None
                    if isinstance(row, list) and row and row[0] == -1:
                        raise ValueError(_error_message(str(row[-1])))
                    yield row
                proc.wait(timeout=5)
                if proc.returncode:
                    errors.flush()
                    errors.seek(0)
                    raise ValueError(_error_message(errors.read()))
            finally:
                stopped.set()
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                reader.join(timeout=2)
                proc.stdout.close()

def iter_posts(handle: str, cookies: Path | None, limit: int | None = None):
    seen = set()
    rows = _iter_rows(urls.profile_url(handle) + "/media", cookies, limit)
    try:
        for row in rows:
            if not isinstance(row, list) or not row or not isinstance(row[-1], dict):
                continue
            data = row[-1]
            identifier = str(data.get("tweet_id") or "")
            if (data.get("user") or {}).get("name", handle).lower() != handle.lower():
                continue
            if identifier.isdigit() and identifier not in seen:
                seen.add(identifier)
                yield DiscoveredPost(identifier, data.get("_lora1_status"))
                if limit is not None and len(seen) >= limit:
                    break
    finally:
        close = getattr(rows, "close", None)
        if close:
            close()

class Listing(list):
    """Keep discovered posts usable when a later page fails."""
    def __init__(self, values=(), error: str = ""):
        super().__init__(values)
        self.error = error

def _error_message(reason: str) -> str:
    reason = reason.lower()
    if "notfound" in reason or "not found" in reason or "suspended" in reason:
        return "not-found"
    if "429" in reason or "rate limit" in reason or "ratelimit" in reason:
        return "X is limiting requests. Try again later."
    if "timeout" in reason or "timed out" in reason:
        return "X took too long to respond. Try Download again to continue."
    return "X could not be reached. Replace your X cookies in Settings → Accounts."

def _query(target: str, cookies: Path | None, limit: int | None = None) -> list:
    from .void_media import read_cookies
    # gallery-dl can refresh its cookie jar; always pass a filtered disposable
    # copy so imported and user-owned cookies are never changed by a probe.
    with tempfile.TemporaryDirectory(prefix="lora1-x-probe-") as folder:
        copy = None
        if cookies and cookies.is_file():
            copy = Path(folder) / "cookies.txt"
            jar = read_cookies(cookies)
            jar.filename = str(copy)
            jar.save(ignore_discard=True, ignore_expires=True)
        return _run_query(target, copy, limit)

def _run_query(target: str, cookies: Path | None, limit: int | None = None) -> list:
    cmd = [sys.executable, "-m", "gallery_dl", "--ignore-config", "--dump-json", "--quiet",
           "--sleep-request", "2", "-o", "retries=1", "-o", "timeout=25", "-o", "extractor.twitter.retweets=false"]
    if cookies and cookies.is_file():
        cmd += ["--cookies", str(cookies)]
    if limit is not None:
        cmd += ["--post-range", f"1-{max(1, limit)}"]
    cmd.append(target)
    media_listing = target.rstrip("/").endswith("/media")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              # Full history can take hours. Individual requests
                              # still have a timeout and bounded retries above.
                              timeout=(180 if limit is not None else None) if media_listing else 45,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise ValueError("X did not answer in time. Try again later.") from None
    if proc.returncode:
        raise ValueError(_error_message(proc.stderr or ""))
    try:
        rows = json.loads(proc.stdout)
    except (ValueError, TypeError):
        raise ValueError("X returned an unreadable response.") from None
    if not isinstance(rows, list):
        raise ValueError("X returned an unreadable response.")
    # DataJob can exit successfully with an error record in its JSON. Do not
    # mistake an interrupted history listing for a completed download.
    error = next((_error_message(str(row[-1])) for row in rows
                  if isinstance(row, list) and row and row[0] == -1), "")
    return Listing(rows, error)

def probe(handle: str, cookies: Path | None) -> dict:
    rows = _query(urls.profile_url(handle) + "/info", cookies)
    for row in rows:
        if isinstance(row, list) and row and isinstance(row[-1], dict):
            data = row[-1]
            if data.get("name"):
                return {"username": str(data["name"]), "fullname": str(data.get("nick") or data["name"]),
                        "owner_id": str(data.get("id") or ""),
                        "avatar": data.get("profile_image") or data.get("profile_image_url_https"),
                        "followers": data.get("followers_count"), "posts": data.get("statuses_count")}
    raise ValueError("X did not return a profile.")

def posts(handle: str, cookies: Path | None, limit: int | None = None) -> list[str]:
    rows = _query(urls.profile_url(handle) + "/media", cookies, limit)
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, list) or not row or not isinstance(row[-1], dict):
            continue
        data = row[-1]
        identifier = str(data.get("tweet_id") or "")
        author = data.get("user") or {}
        # Keep the creator's own media; exclude quoted posts and reposts.
        if author.get("name", handle).lower() != handle.lower():
            continue
        if identifier.isdigit() and identifier not in seen:
            result.append(identifier)
            seen.add(identifier)
    return Listing(result[:limit] if limit is not None else result, getattr(rows, "error", ""))
