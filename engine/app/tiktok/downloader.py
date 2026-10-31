"""Video/photo download: tikdownloader.io primary, yt-dlp fallback.

Ported from monitor.py / lora1.py with an added progress callback so the
queue/worker layer can stream progress over WebSockets.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import subprocess
import sys
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Callable, Optional

from ..core.logging_config import get_logger

log = get_logger("tiktok.downloader")


class DownloadPaused(Exception):
    """Raised from the progress callback to abort an in-flight download when its
    job has been paused (individually or via Stop All). The worker then requeues
    the job as `paused` instead of completing it."""


class DownloadCancelled(DownloadPaused):
    """Raised from the progress callback to abort an in-flight download when its
    job has been cancelled - typically because the creator was removed while a
    download was running. Subclasses DownloadPaused so the downloader's existing
    `except DownloadPaused: raise` guards propagate it unchanged; the worker
    finalises the job as `cancelled` (not requeued)."""

# ─────────────────────────────────────────────────────────────
# Rate-limit protection for tikdownloader.io  (shared by all workers)
# ─────────────────────────────────────────────────────────────
# tikdownloader.io sits behind Cloudflare and rate-limits per client IP, so the
# whole worker pool must behave as ONE polite client:
#   • the page token is cached + reused (no GET /en per download);
#   • token-refresh + ajaxSearch calls pass through a single global governor
#     that spaces them by `_interval` seconds (bursts from parallel workers are
#     serialised - worker count never multiplies the API request rate);
#   • a 429 from ANY worker is shared state: it widens the interval (AIMD) and
#     sets a global cooldown that EVERY worker waits out, so we stop hammering
#     the IP instead of letting the other workers keep tripping the limit;
#   • successes slowly relax the interval back toward the configured base.
# The CDN media transfer itself is NOT paced (different host, not rate-limited).
#
# Defaults (base interval 3.0s ≈ 20 API calls/min, 3 workers) are deliberately
# conservative: bulk "download all" of hundreds of posts then runs without
# tripping Cloudflare. See app/core/config.py for the rationale.

_MIN_INTERVAL = 3.0   # configured base spacing between API calls (seconds)
_MAX_INTERVAL = 30.0  # cap for the adaptive interval / backoff
_MAX_RETRIES = 4      # ajaxSearch attempts before falling back to yt-dlp
_TOKEN_TTL = 600.0    # seconds a scraped token stays valid before refresh

_pace_lock = Lock()   # serialises API calls; held across the spacing sleep
_last_call = 0.0

_state_lock = Lock()  # guards the adaptive governor state below
_interval = _MIN_INTERVAL     # current spacing - grows on 429, decays on success
_cooldown_until = 0.0         # monotonic time before which NO API call may fire
_consecutive_429 = 0
_consecutive_soft = 0         # soft blocks (200-but-empty / 'fail' / IP block)

_token_lock = Lock()
_token_value = ""
_token_ts = 0.0

RateLimitHook = Optional[Callable[[float], None]]
_on_rate_limit: RateLimitHook = None


def set_min_interval(seconds: float) -> None:
    """Set the base spacing between tikdownloader.io API calls (live).

    An explicit change adopts the new base immediately (the adaptive penalty, if
    any, re-grows on the next 429) - so lowering it speeds back up and raising it
    slows down right away."""
    global _MIN_INTERVAL, _interval, _consecutive_429
    with _state_lock:
        _MIN_INTERVAL = max(0.0, float(seconds))
        _interval = _MIN_INTERVAL
        _consecutive_429 = 0
    log.info("tikdownloader base request interval set to %.1fs", _MIN_INTERVAL)


def set_max_interval(seconds: float) -> None:
    """Set the adaptive pacing ceiling without resetting a current penalty."""
    global _MAX_INTERVAL, _interval
    with _state_lock:
        _MAX_INTERVAL = max(_MIN_INTERVAL, float(seconds))
        _interval = min(_interval, _MAX_INTERVAL)


def set_max_retries(n: int) -> None:
    global _MAX_RETRIES
    _MAX_RETRIES = max(1, int(n))


def set_rate_limit_hook(fn: RateLimitHook) -> None:
    """Register a callback invoked (with the cooldown seconds) when a 429 is
    seen, so the service layer can surface user-facing feedback."""
    global _on_rate_limit
    _on_rate_limit = fn


def _pace() -> None:
    """Block until the global governor permits the next API call."""
    global _last_call
    with _pace_lock:  # held across the sleep so calls are strictly serialised
        with _state_lock:
            interval, cooldown = _interval, _cooldown_until
        target = max(_last_call + interval, cooldown)
        wait = target - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def note_rate_limited(retry_after: float | None = None) -> float:
    """Record a 429. Widens the global interval and arms a cooldown that all
    workers honour via `_pace`. Returns the cooldown the caller should sleep."""
    global _interval, _cooldown_until, _consecutive_429
    with _state_lock:
        _consecutive_429 += 1
        if retry_after and retry_after > 0:
            backoff = min(_MAX_INTERVAL * 2, retry_after)
        else:
            # Exponential backoff off the base interval, capped, plus jitter.
            backoff = min(_MAX_INTERVAL, max(_MIN_INTERVAL, 1.0) * (2 ** _consecutive_429))
        backoff += random.uniform(0, 1.0)
        # Multiplicative increase of the steady-state spacing (AIMD).
        _interval = min(_MAX_INTERVAL, max(_interval * 1.5, _MIN_INTERVAL))
        _cooldown_until = max(_cooldown_until, time.monotonic() + backoff)
    if _on_rate_limit:
        try:
            _on_rate_limit(backoff)
        except Exception:  # feedback must never break a download
            pass
    return backoff


# An empty body means only "I'm not giving you this one". Usually that IS load,
# but it is also the answer for a post the service simply cannot fetch, and those
# arrive in runs whenever a creator's backlog is full of them. Treating each one
# as a throttle signal let a handful of unavailable posts widen the interval for
# every worker - the pool slowing itself down over posts no pacing could fix.
# So the penalty now needs the empties to be dense in TIME, not merely
# consecutive: real throttling keeps hitting however long you wait, while
# unavailable posts stay unavailable no matter the spacing.
_SOFT_WINDOW_SECS = 90.0
_soft_times: deque[float] = deque(maxlen=32)


def note_soft_block() -> None:
    """A 200-but-empty / 'fail' response, or a 'your IP is blocked' from the
    yt-dlp fallback - softer than a 429, but still a possible 'back off' signal.
    Only a burst within `_SOFT_WINDOW_SECS` is treated as real pressure."""
    global _interval, _consecutive_soft, _cooldown_until
    fire = 0.0
    with _state_lock:
        now = time.monotonic()
        _soft_times.append(now)
        while _soft_times and now - _soft_times[0] > _SOFT_WINDOW_SECS:
            _soft_times.popleft()
        recent = len(_soft_times)
        _consecutive_soft += 1

        # One or two in a window are just unavailable posts; leave the pool alone.
        if recent >= 3:
            _interval = min(_MAX_INTERVAL, max(_interval * 1.25, _MIN_INTERVAL))
        if recent >= 5:
            fire = min(_MAX_INTERVAL, max(_MIN_INTERVAL, 1.0) * 4) + random.uniform(0, 1.0)
            _cooldown_until = max(_cooldown_until, time.monotonic() + fire)
            _soft_times.clear()   # the cooldown answers this burst; start counting afresh
    if fire and _on_rate_limit:
        try:
            _on_rate_limit(fire)
        except Exception:
            pass


def note_success() -> None:
    """Record a genuinely useful API response; gently relax the adaptive interval."""
    global _interval, _consecutive_429, _consecutive_soft
    with _state_lock:
        _consecutive_429 = 0
        _consecutive_soft = 0
        if _interval > _MIN_INTERVAL:
            _interval = max(_MIN_INTERVAL, _interval * 0.8)  # additive-ish decay


def _retry_after_seconds(resp) -> float | None:
    """Parse a Retry-After header (delta-seconds form) if present."""
    raw = (resp.headers.get("Retry-After") or "").strip() if resp is not None else ""
    if raw.isdigit():
        return float(raw)
    return None

ProgressCb = Optional[Callable[[float], None]]

# tikdownloader.io moved the CSRF-style token from a hidden <input> into inline
# JS (`k_token="…"`). Newer patterns are tried first; the old form-input patterns
# are kept as fallbacks in case the layout reverts.
TOKEN_PATTERNS = [
    re.compile(r'k_token\s*=\s*"([^"]+)"'),
    re.compile(r'APP_Token\s*=\s*"([^"]+)"'),
    re.compile(r'name="token"\s+value="([^"]+)"'),
    re.compile(r'"token"\s*:\s*"([^"]+)"'),
]
LINK_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
HD_KEYWORDS = {"hd", "1080", "no watermark", "without watermark"}
IMAGE_KEYWORDS = {"image", "photo", "jpg", "jpeg", "png", "webp"}

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://tikdownloader.io",
    "Referer": "https://tikdownloader.io/en",
}


@dataclass
class DownloadResult:
    success: bool
    path: Optional[Path] = None
    strategy: str = ""
    is_image: bool = False
    size: int = 0
    error: str = ""


# Why a download failed, and whether trying again could ever help.
#
# Everything used to collapse into "All download strategies failed", because
# ytdlp_download threw its stderr away and returned None. That hid the two facts
# that matter: a post the account cannot see from here will never succeed no
# matter how often it is retried, while a timeout very likely will.
PERMANENT = "permanent"
TRANSIENT = "transient"

# TikTok's wording for a post this IP/region may not see. It names the POST, not
# the account - the same sweep downloads other posts by the same creator fine.
_REGION_BLOCK = "blocked from accessing this post"

_PERMANENT_SIGNS = (
    _REGION_BLOCK,
    "video is private",
    "post is private",
    "content isn't available",
    "video not available",
    "has been removed",
    "account is private",
    "unable to find video",
)
_TRANSIENT_SIGNS = (
    "timed out", "timeout", "429", "too many requests", "temporarily",
    "connection", "read error", "unable to connect", "network", "503", "502",
)


# Last failure per URL. A dict keyed on the URL rather than a single global,
# because three workers fail concurrently and would otherwise overwrite each
# other's reason. Bounded so a long run cannot grow it without limit.
_errors: "OrderedDict[str, str]" = OrderedDict()
_errors_lock = Lock()
_ERRORS_MAX = 256


def _remember_error(url: str, message: str) -> None:
    if not message:
        return
    with _errors_lock:
        _errors[url] = message
        while len(_errors) > _ERRORS_MAX:
            _errors.popitem(last=False)


def last_error(url: str) -> str:
    """The most recent failure message recorded for this URL, if any."""
    with _errors_lock:
        return _errors.get(url, "")


def classify_failure(message: str) -> str:
    """PERMANENT when retrying cannot help, TRANSIENT otherwise.

    Unknown failures are treated as transient on purpose: retrying something
    unrecoverable wastes a little time, whereas giving up on something that
    would have worked loses the post.
    """
    low = (message or "").lower()
    if any(sign in low for sign in _PERMANENT_SIGNS):
        return PERMANENT
    if any(sign in low for sign in _TRANSIENT_SIGNS):
        return TRANSIENT
    return TRANSIENT


def friendly_error(message: str) -> str:
    """The failure as a person should read it."""
    low = (message or "").lower()
    if _REGION_BLOCK in low:
        return "TikTok blocks this post for your region or IP."
    if "private" in low:
        return "This post is private and the saved cookies can't see it."
    if "removed" in low or "not available" in low or "isn't available" in low:
        return "This post is no longer available on TikTok."
    if "timed out" in low or "timeout" in low:
        return "TikTok took too long to respond. It will be retried."
    if "429" in low or "too many" in low:
        return "TikTok is rate limiting us. It will be retried."
    text = (message or "").strip().splitlines()
    return (text[-1][:200] if text else "Download failed.")


def _make_session():
    from curl_cffi import requests as cffi_requests

    s = cffi_requests.Session(impersonate="chrome136")
    s.headers.update(BASE_HEADERS)
    return s


def _get_token(session) -> str:
    resp = session.get("https://tikdownloader.io/en", timeout=30)
    resp.raise_for_status()
    for pat in TOKEN_PATTERNS:
        m = pat.search(resp.text)
        if m:
            return m.group(1)
    return ""


def _get_cached_token(session, force: bool = False) -> str:
    """Return a shared page token, refreshing (with pacing) only when stale."""
    global _token_value, _token_ts
    now = time.monotonic()
    if not force:
        with _token_lock:
            if _token_value and (now - _token_ts) < _TOKEN_TTL:
                return _token_value
    _pace()
    try:
        tok = _get_token(session)
    except Exception as exc:
        log.warning("token refresh failed: %s", exc)
        with _token_lock:
            return _token_value  # fall back to any previously cached token
    if tok:
        with _token_lock:
            _token_value = tok
            _token_ts = time.monotonic()
    return tok


def _parse_links(html: str) -> list[dict]:
    links, seen = [], set()
    for match in LINK_RE.finditer(html):
        href = match.group(1)
        if href in seen or not href.startswith("http"):
            continue
        seen.add(href)
        label = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        label_lower = label.lower()
        url_lower = href.lower().split("?")[0]
        is_image = any(k in label_lower for k in IMAGE_KEYWORDS) or any(
            url_lower.endswith(f".{ext}") for ext in ("jpg", "jpeg", "png", "webp")
        )
        links.append({"label": label, "url": href, "is_image": is_image})
    return links


def _pick_best_video(links: list[dict]) -> dict:
    for link in links:
        if any(k in link["label"].lower() for k in HD_KEYWORDS):
            return link
    return links[0]


def _guess_ext(url: str) -> str:
    url_lower = url.lower().split("?")[0]
    for ext in (".png", ".webp", ".jpeg", ".jpg"):
        if url_lower.endswith(ext):
            return ext
    return ".jpg"


# Intermittent CDN transfer errors (notably curl's HTTP/2 "(92) … INTERNAL_ERROR"
# stream reset) are retryable, unlike a real 4xx. Retry a couple of times before
# giving up to the yt-dlp fallback.
_CDN_RETRIES = 2
_TRANSIENT_HINTS = ("curl: (92", "curl: (56", "curl: (18", "curl: (28", "stream", "reset", "timed out", "eof occurred")


def _is_transient(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(h in s for h in _TRANSIENT_HINTS)


def _cdn_get(session, url: str, **kwargs):
    """GET a CDN media URL, retrying briefly on transient HTTP/2 / connection
    errors. Raises on a non-transient error or once retries are exhausted."""
    for attempt in range(_CDN_RETRIES + 1):
        try:
            r = session.get(url, **kwargs)
            r.raise_for_status()
            return r
        except Exception as exc:
            if attempt < _CDN_RETRIES and _is_transient(exc):
                time.sleep(0.5 * (attempt + 1))
                continue
            raise


def tikdownloader_download(
    tiktok_url: str, output_path: Path, progress_cb: ProgressCb = None
) -> Optional[DownloadResult]:
    try:
        session = _make_session()
        token = _get_cached_token(session)
        if not token:
            log.warning("tikdownloader: could not extract token (site layout changed?)")

        # ajaxSearch is rate-limited by Cloudflare. Every call is globally paced
        # (_pace) and transient 429s are retried with shared exponential backoff
        # (note_rate_limited slows ALL workers) before giving up and letting the
        # caller fall back to yt-dlp.
        resp = None
        attempts = max(1, _MAX_RETRIES)
        for attempt in range(attempts):
            _pace()
            resp = session.post(
                "https://tikdownloader.io/api/ajaxSearch",
                data={"q": tiktok_url, "token": token},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                },
                timeout=30,
            )
            if resp.status_code == 429:
                wait = note_rate_limited(_retry_after_seconds(resp))
                log.warning(
                    "tikdownloader rate-limited (429); backing off %.1fs (attempt %d/%d)",
                    wait, attempt + 1, attempts,
                )
                # A stale token can also present as a block - refresh once.
                if attempt == 0:
                    token = _get_cached_token(session, force=True) or token
                if attempt < attempts - 1:
                    time.sleep(wait)
                continue
            # NB: don't call note_success() here - a 200 can still be a soft block
            # (empty body / "fail"). Success is recorded only once we extract
            # real download links below.
            break

        if resp is None or resp.status_code != 200:
            code = resp.status_code if resp else "no-response"
            if code == 429:
                log.warning("tikdownloader still rate-limited after %d attempts; falling back to yt-dlp", attempts)
            else:
                log.warning("tikdownloader HTTP %s", code)
            return None
        try:
            payload = resp.json()
            if payload.get("status") == "fail":
                log.warning("tikdownloader API fail: %s", str(payload.get("mess") or payload)[:160])
                note_soft_block()  # a 'fail' under load is a soft throttle - back off
                return None
            html = payload.get("data", "") or resp.text
        except Exception:
            html = resp.text

        links = _parse_links(html)
        if not links:
            log.warning("tikdownloader: no download links in response (len=%d)", len(html))
            note_soft_block()  # empty body is how tikdownloader soft-blocks - back off
            return None
        note_success()  # extracted real links - a genuine success

        # ── Image / photo post ──
        image_links = [l for l in links if l["is_image"]]
        if image_links:
            img_dir = output_path.with_suffix("")
            img_dir.mkdir(parents=True, exist_ok=True)
            ok = 0
            total = len(image_links)
            for idx, lnk in enumerate(image_links, start=1):
                img_path = img_dir / f"{idx:03d}{_guess_ext(lnk['url'])}"
                try:
                    r = _cdn_get(session, lnk["url"], headers={"Referer": "https://tikdownloader.io/"}, timeout=120)
                    img_path.write_bytes(r.content)
                    ok += 1
                except Exception as exc:
                    log.warning("image %d failed after retries: %s", idx, exc)
                if progress_cb:
                    progress_cb(idx / total * 100)
            if ok:
                size = sum(p.stat().st_size for p in img_dir.glob("*") if p.is_file())
                return DownloadResult(True, img_dir, "tikdownloader", is_image=True, size=size)
            return None

        # ── Video post ──
        chosen = _pick_best_video(links)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(_CDN_RETRIES + 1):
            try:
                dl = session.get(chosen["url"], headers={"Referer": "https://tikdownloader.io/"}, stream=True, timeout=120)
                dl.raise_for_status()
                downloaded = 0
                total = int(dl.headers.get("content-length", 0))
                with output_path.open("wb") as fh:
                    for chunk in dl.iter_content(chunk_size=65536):
                        if chunk:
                            fh.write(chunk)
                            downloaded += len(chunk)
                            if total and progress_cb:
                                progress_cb(min(99.0, downloaded / total * 100))
                break  # transfer complete
            except DownloadPaused:
                raise  # a pause is not a transient error - let it propagate
            except Exception as exc:
                output_path.unlink(missing_ok=True)  # drop the partial before retrying
                if attempt < _CDN_RETRIES and _is_transient(exc):
                    log.warning("video transfer hiccup (%s); retry %d/%d", str(exc)[:60], attempt + 1, _CDN_RETRIES)
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
        size = output_path.stat().st_size
        if size < 10_000:
            output_path.unlink(missing_ok=True)
            return None
        if progress_cb:
            progress_cb(100.0)
        return DownloadResult(True, output_path, "tikdownloader", size=size)
    except DownloadPaused:
        # Aborted by a pause - drop the partial file so a resume restarts clean,
        # and let it propagate (do NOT fall through to the yt-dlp path).
        try:
            output_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    except Exception as exc:
        log.error("tikdownloader exception: %s", exc)
        return None


def ytdlp_download(url: str, output_path: Path, cookies: Path) -> Optional[DownloadResult]:
    from .resolver import ytdlp_cookie_copy

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # Cookies unlock private / followers-only / region-locked posts. yt-dlp
    # rewrites the --cookies file on exit (persisting TikTok's anti-bot response
    # cookies and dropping the HttpOnly login session), so hand it a disposable
    # COPY - otherwise the user's real cookies.txt gets its login stripped after
    # the first download. Only pass the flag when a cookie file exists.
    with ytdlp_cookie_copy(cookies) as ck:
        cmd = [sys.executable, "-m", "yt_dlp"]
        if ck:
            cmd += ["--cookies", str(ck)]
        cmd += [
            "--impersonate", "Chrome-136",
            "-f", "bestvideo+bestaudio/best",
            "--merge-output-format", "mp4",
            "-o", str(output_path),
            "--no-playlist", "--quiet", url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        low = stderr.lower()
        # Only a genuine throttle slows the pool. "blocked from accessing this
        # post" is region locking on ONE post - the same sweep downloads other
        # posts by the same creator seconds later. Treating it as IP-wide
        # pressure made a handful of region-locked posts throttle every worker
        # for the rest of a bulk run, which is the opposite of what a bulk run
        # needs.
        throttled = ("429" in low or "too many" in low
                     or ("blocked" in low and _REGION_BLOCK not in low))
        if throttled:
            note_soft_block()
        log.error("yt-dlp failed: %s", stderr[-300:])
        # The reason travels with the failure. Returning a bare None is what
        # reduced every one of these to "All download strategies failed".
        _remember_error(url, stderr)
        return None
    final = output_path
    if not final.exists():
        final = next((p for p in output_path.parent.glob(output_path.stem + ".*") if p.stat().st_size > 10_000), None)
    if final and final.exists() and final.stat().st_size > 10_000:
        return DownloadResult(True, final, "yt-dlp", size=final.stat().st_size)
    return None


def download_video(
    url: str, user_dir: Path, video_id: str, cookies: Path, progress_cb: ProgressCb = None,
    private: bool = False,
) -> DownloadResult:
    """tikdownloader first, yt-dlp fallback. Returns a DownloadResult.

    `private=True` skips tikdownloader outright: it has no logged-in session, so
    a private post can only come back empty - which costs a paced API call and
    registers as a soft block against every other worker.
    """
    user_dir.mkdir(parents=True, exist_ok=True)
    output_path = user_dir / f"{video_id}.mp4"

    if private:
        log.info("private post %s - going straight to yt-dlp", video_id)
        result = ytdlp_download(url, output_path, cookies)
        if result:
            if progress_cb:
                progress_cb(100.0)
            return result
        captured = _browser_capture(url, output_path, cookies)
        if captured:
            if progress_cb:
                progress_cb(100.0)
            return captured
        raw = last_error(url)
        return DownloadResult(
            False, strategy="ytdlp_private",
            error=friendly_error(raw) if raw
            else "Private post: the saved cookies don't have access to it.",
        )

    result = tikdownloader_download(url, output_path, progress_cb)
    if result:
        return result

    # yt-dlp fallback handles what tikdownloader can't - notably private /
    # followers-only posts (via cookies). Always attempt it; cookies are used
    # automatically when a profile is configured.
    log.warning("tikdownloader failed; trying yt-dlp%s", " with cookies" if cookies.exists() else "")
    result = ytdlp_download(url, output_path, cookies)
    if result:
        if progress_cb:
            progress_cb(100.0)
        return result
    captured = _browser_capture(url, output_path, cookies)
    if captured:
        if progress_cb:
            progress_cb(100.0)
        return captured
    # Report what actually went wrong. "All download strategies failed" is true
    # and useless; the reason underneath it is the thing worth showing.
    raw = last_error(url)
    return DownloadResult(
        False, strategy="all_failed",
        error=friendly_error(raw) if raw else "All download strategies failed",
    )


# Share links, and links that name themselves as stories. These are the ones the
# browser fallback can actually rescue: a story viewer autoplays, so the media
# request fires on its own. An ordinary post page does not autoplay in a headless
# browser, so nothing is captured and the attempt is ~17s of pure waste - which,
# multiplied across a failing sweep, is worse than the failure it is chasing.
_SHARE_HOSTS = ("vt.tiktok.com", "vm.tiktok.com")
_STORY_MARKERS = ("story_type=", "aweme_type=40", "/story/", "story_uid=")


def _worth_browser_capture(url: str) -> bool:
    low = url.lower()
    return any(h in low for h in _SHARE_HOSTS) or any(m in low for m in _STORY_MARKERS)


def _browser_capture(url: str, output_path: Path, cookies: Path) -> "DownloadResult | None":
    """Last resort: play the page and save what the player fetched.

    Some posts are reachable only through the link that was shared - a story
    share URL carries signed parameters granting access that the creator's
    profile and follow feed do not. tikdownloader gets an empty response for
    those and yt-dlp cannot parse the page, so both report failure on a post
    that plays perfectly in a browser.

    Handles photo-mode posts as photo posts. They pair a still with a separate
    audio track that TikTok serves as `video/mp4`, so treating whatever looks
    like video as the post produced an .mp4 containing an MP3 - unplayable, and
    listed as a video whose duration was really the song's.
    """
    if not _worth_browser_capture(url):
        return None
    try:
        from . import browser as _browser

        captured = _browser.capture_media_via_browser(url, cookies)
        urls = (captured or {}).get("urls") or []
        if not urls:
            return None
        session = _media_session(cookies)

        if captured.get("kind") == "photo":
            # Photo post: a folder of images named like every other photo post,
            # so the library, viewer and MEGA upload all treat it normally.
            folder = output_path.with_suffix("")
            folder.mkdir(parents=True, exist_ok=True)
            total = 0
            # De-duplicate on the bytes, not the URL. TikTok serves the same
            # still from several addresses that differ only in transform
            # parameters, so URL-keyed de-duplication saved the same picture
            # two or three times over.
            seen: set[str] = set()
            index = 0
            for media_url in urls:
                resp = session.get(media_url, timeout=180)
                if resp.status_code != 200 or len(resp.content) < 2_000:
                    continue
                digest = hashlib.sha256(resp.content).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                index += 1
                (folder / f"{index:02d}.jpg").write_bytes(resp.content)
                total += len(resp.content)
            if not total:
                return None
            log.info("downloaded %s via browser capture (%d image(s), %d bytes)",
                     folder.name, len(list(folder.iterdir())), total)
            return DownloadResult(True, path=folder, strategy="browser", is_image=True, size=total)

        resp = session.get(urls[0], timeout=180)
        if resp.status_code != 200 or len(resp.content) < 10_000:
            log.warning("browser-captured media came back empty (HTTP %s)", resp.status_code)
            return None
        # Refuse to write audio (or anything else) under a .mp4 name. An MP4
        # always carries `ftyp` in its header; an ID3 tag means it is an MP3.
        if b"ftyp" not in resp.content[:64]:
            log.warning("browser-captured media is not an MP4 (header %r); discarding",
                        resp.content[:8])
            return None
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(resp.content)
        log.info("downloaded %s via browser capture (%d bytes)", output_path.name, len(resp.content))
        return DownloadResult(True, path=output_path, strategy="browser", size=len(resp.content))
    except Exception as exc:
        log.warning("browser capture failed: %s", exc)
        return None


def _media_session(cookies: Path):
    from curl_cffi import requests as cffi_requests

    from .resolver import load_netscape_cookies

    session = cffi_requests.Session(impersonate="chrome136")
    session.headers.update({"Referer": "https://www.tiktok.com/", "Accept": "*/*"})
    if cookies and cookies.exists():
        try:
            load_netscape_cookies(session, cookies)
        except Exception:
            pass
    return session
