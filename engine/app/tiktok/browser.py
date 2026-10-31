"""Headless-browser profile fetch (Playwright).

TikTok's WAF (SlardarWAF) serves a challenge page to plain HTTP clients even with
valid cookies, so avatar + follower/video counts can't be scraped over curl_cffi.
A real headless Chromium loads the page, lets the challenge resolve, and exposes
the embedded rehydration JSON - which we parse for the full profile.

Playwright's sync API is bound to the thread that created it, but FastAPI serves
sync routes across a threadpool. So all Playwright work runs in ONE dedicated
owner thread; callers submit requests over a queue and block for the result. The
browser stays warm between calls. If Playwright/Chromium is unavailable, the
public function returns None and callers fall back to the HTTP path.
"""

from __future__ import annotations

import itertools
import os
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from ..core.logging_config import get_logger
from .listing import AccountMetadata, _meta_from_user_info

log = get_logger("tiktok.browser")

_REHYDRATION_ID = "__UNIVERSAL_DATA_FOR_REHYDRATION__"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
_STEALTH = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "window.chrome={runtime:{}};"
    "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
)

_available: bool | None = None
# One serialized Chromium serves every caller, so a background sweep and a
# person waiting on a message share the same queue. A story sweep across 90
# creators occupies it for minutes at ~20s per fetch, which is fine for the sweep
# and fatal for anything interactive: a Telegram post lookup queued behind it hit
# its wait, gave up, and delivered a caption with no metadata in it.
#
# So the queue is ordered. Interactive work goes first; sweeps fill the gaps.
# Ties break on arrival order, and the counter also keeps the tuples comparable
# when priorities match (the payloads themselves are not).
PRIORITY_INTERACTIVE = 0
PRIORITY_BACKGROUND = 10

_req_q: "queue.PriorityQueue" = queue.PriorityQueue()
_req_seq = itertools.count()

# ── Admission control ──────────────────────────────────────────────────────
#
# One browser serves every caller, and a fetch takes tens of seconds. With a
# large roster the queue can grow past the point where anything joining it can
# still be served before its caller gives up - and the old code queued those
# requests anyway, then faithfully executed them after the caller had gone.
#
# That is a collapse, not a slowdown: each abandoned fetch occupies the browser
# for ~40s producing a result nobody reads, which pushes the NEXT caller past
# its deadline too. Once the backlog crossed the timeout budget, essentially
# every request timed out forever while the browser stayed 100% busy.
#
# Three rules keep it healthy:
#   1. Every request carries a deadline; the worker discards what has expired
#      rather than doing dead work.
#   2. A caller that cannot be served in time is refused immediately, so it
#      falls back to the HTTP path in milliseconds instead of blocking a scan
#      thread for a minute to learn the same thing.
#   3. Duplicate requests for the same target share one fetch.
_inflight_lock = threading.Lock()
_inflight: dict[tuple[str, str], list] = {}     # (op, target) -> waiters
_queue_depth = 0

# Rolling estimate of how long one fetch takes, used to decide whether a new
# caller can be served in time. Seeded high so the first few requests are
# admitted conservatively rather than optimistically.
_avg_duration = 25.0
_AVG_SMOOTHING = 0.2


def _note_duration(seconds: float) -> None:
    global _avg_duration
    _avg_duration = (1 - _AVG_SMOOTHING) * _avg_duration + _AVG_SMOOTHING * seconds


def queue_stats() -> dict:
    with _inflight_lock:
        return {"depth": _queue_depth, "avg_seconds": round(_avg_duration, 1),
                "inflight": len(_inflight)}
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_SENTINEL = object()


def is_available() -> bool:
    global _available
    if _available is None:
        try:
            import playwright.sync_api  # noqa: F401

            _available = True
        except Exception:
            _available = False
    return _available


def _netscape_to_pw_cookies(path: Path) -> list[dict]:
    cookies: list[dict] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw[len("#HttpOnly_"):] if raw.startswith("#HttpOnly_") else raw
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, _flag, cpath, secure, expiry, name, value = parts
        cookies.append({
            "name": name, "value": value, "domain": domain, "path": cpath or "/",
            "secure": secure.upper() == "TRUE",
            "expires": int(expiry) if expiry.isdigit() else -1,
        })
    return cookies


def _add_cookies(context, handle: str, cookies: Path) -> None:
    if cookies.exists():
        try:
            context.add_cookies(_netscape_to_pw_cookies(cookies))
        except Exception as exc:
            log.debug("[@%s] add_cookies failed: %s", handle, exc)


# TikTok states its own verdict in the profile page's rehydration blob, and it is
# the only source that distinguishes "this account is gone" from "the WAF would
# not serve you the page". yt-dlp reports both as "Unable to extract secondary
# user ID", which is why deleted creators sat in the monitor being retried
# forever under a message about bot checks.
STATUS_OK = 0
STATUS_NOT_FOUND = 10202       # "Couldn't find this account"
STATUS_BANNED = 10221
STATUS_PRIVATE = 10222        # exists, but its posts are not visible to us
ACCOUNT_GONE_CODES = {STATUS_NOT_FOUND, STATUS_BANNED}


@dataclass
class Probe:
    """What the browser could determine about a profile."""

    metadata: AccountMetadata | None = None
    status_code: int | None = None
    private: bool = False

    @property
    def gone(self) -> bool:
        """The account does not exist any more. Terminal - retrying cannot help."""
        return self.status_code in ACCOUNT_GONE_CODES

    @property
    def private_account(self) -> bool:
        """Exists, but its posts are not visible to this login. Emphatically not
        gone - a private creator is still there, and saying otherwise sends
        someone off to check an account that was never missing."""
        return self.status_code == STATUS_PRIVATE or self.private

    @property
    def ok(self) -> bool:
        return self.metadata is not None


def _do_fetch(browser, handle: str, cookies: Path, timeout: int) -> "Probe":
    context = browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 800}, locale="en-US")
    try:
        context.add_init_script(_STEALTH)
        _add_cookies(context, handle, cookies)
        page = context.new_page()
        page.goto(f"https://www.tiktok.com/@{handle}", wait_until="domcontentloaded", timeout=timeout * 1000)
        # First response may be the WAF challenge; it resolves + reloads the real
        # page, so wait for the rehydration node to appear.
        try:
            page.wait_for_selector(f"#{_REHYDRATION_ID}", state="attached", timeout=timeout * 1000)
        except Exception:
            log.debug("[@%s] rehydration node never appeared (WAF/challenge?)", handle)
            return Probe()
        raw = page.evaluate(
            f"() => {{ const el = document.getElementById('{_REHYDRATION_ID}'); return el ? el.textContent : null; }}"
        )
        if not raw:
            return Probe()
        blob = json.loads(raw)
        detail = blob.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {})
        status = detail.get("statusCode")
        info = detail.get("userInfo", {}) or {}
        user = info.get("user", {}) or {}
        meta = _meta_from_user_info(user, info.get("stats", {}) or info.get("statsV2", {}), handle)
        probe = Probe(metadata=meta, status_code=status, private=bool(user.get("privateAccount")))
        if probe.gone:
            log.info("[@%s] TikTok reports this account no longer exists (statusCode=%s)", handle, status)
        elif meta:
            log.info("[@%s] profile via headless browser (followers=%s, posts=%s, private=%s)",
                     handle, meta.followers, meta.total_posts, probe.private)
        return probe
    finally:
        try:
            context.close()
        except Exception:
            pass


# TikTok's photo-mode ("image") posts and stories are a still image plus a
# separate audio track. The audio is served with `content-type: video/mp4`, so a
# naive "is this video?" check picks the soundtrack and saves it as the post -
# an .mp4 that is really an ID3-tagged MP3, which no player shows and which the
# library reports as an 18-second video with no picture.
#
# These markers separate the three things a story page loads.
_PHOTO_MARKER = "photomode"      # the still itself
_AVATAR_MARKER = "-avt-"         # the creator's avatar, never the post
_STATIC_HOSTS = ("ttwstatic.com",)   # TikTok's own UI assets


def _is_real_video(headers: dict, url: str) -> bool:
    content_type = (headers or {}).get("content-type", "")
    if not (content_type.startswith("video/") or "mp4" in content_type):
        return False
    return not any(h in url for h in _STATIC_HOSTS)


def _do_capture_media(browser, page_url: str, cookies: Path, timeout: int) -> dict:
    """Open a page and report the media it actually played.

    Returns {"kind": "video"|"photo", "urls": [...]} or {} when nothing usable
    appeared. The last resort for a post neither tikdownloader nor yt-dlp can
    serve - notably a story reached through a share link, whose signed
    parameters grant access the creator's profile and follow feed do not.

    The URLs are signed CDN addresses that fetch normally with the same cookies,
    so the download itself stays outside the browser.
    """
    context = browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 800}, locale="en-US")
    videos: list[tuple[str, int]] = []
    photos: list[tuple[str, int]] = []

    def _on_response(resp):
        url = resp.url
        headers = resp.headers or {}
        try:
            size = int(headers.get("content-length") or 0)
        except (TypeError, ValueError):
            size = 0
        content_type = headers.get("content-type", "")
        if content_type.startswith("image/"):
            # Only the post's own stills, never the avatar or UI chrome.
            if _PHOTO_MARKER in url and _AVATAR_MARKER not in url:
                photos.append((url, size))
        elif _is_real_video(headers, url):
            videos.append((url, size))

    try:
        context.add_init_script(_STEALTH)
        _add_cookies(context, "", cookies)
        page = context.new_page()
        page.on("response", _on_response)
        page.goto(page_url, wait_until="domcontentloaded", timeout=timeout * 1000)
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout, 25) * 1000)
        except Exception:
            pass
        # The player starts after the page settles, so media lands a little late.
        page.wait_for_timeout(8000)

        if photos:
            # A photo post wins over any video response, because on this kind of
            # page that response is the soundtrack, not the post.
            best: dict[str, tuple[str, int]] = {}
            for url, size in photos:
                # Same still is fetched at several resolutions; keep the largest
                # of each, keyed on the path before the transform suffix.
                key = url.split("~")[0]
                if key not in best or size > best[key][1]:
                    best[key] = (url, size)
            urls = [u for u, _ in best.values()]
            log.info("captured %d photo-mode image(s) via browser", len(urls))
            return {"kind": "photo", "urls": urls}

        if videos:
            url, size = max(videos, key=lambda pair: pair[1])
            log.info("captured video via browser (%s bytes)", size or "unknown")
            return {"kind": "video", "urls": [url]}
        return {}
    finally:
        try:
            context.close()
        except Exception:
            pass


def _do_fetch_stories(browser, handle: str, cookies: Path, timeout: int) -> list[dict]:
    """Load a profile and capture TikTok's signed story-list responses.

    Loading the profile makes the page's own JS fire the story request with a
    valid signature (X-Bogus / X-Gnarly / msToken) - which is why this must run
    in a real browser with the logged-in cookies. We intercept the response and
    return the raw story item dicts (empty when there's no active story)."""
    context = browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 800}, locale="en-US")
    captured: list[dict] = []

    wanted = handle.lower()

    def _mine(item: dict) -> bool:
        """Keep only stories this creator actually authored.

        The batch endpoint below carries the signed-in account's whole story
        feed, so without this check another creator's story would be captured,
        saved into this creator's folder and recorded against them. Items from
        the profile-specific endpoint always match, so the filter costs nothing
        there and makes the batch source safe to use.
        """
        author = (item.get("author") or {}).get("uniqueId") or ""
        return author.lower() == wanted if author else True

    def _on_response(resp):
        url = resp.url
        if "/api/story/" not in url:
            return
        # The profile's own story list, and the follow-feed batch. The batch is
        # included because TikTok does not always fire the profile-specific call
        # - when it doesn't, the creator's story is only ever visible there, and
        # the account filter keeps it honest.
        is_profile = "/api/story/item_list" in url
        is_batch = "/api/story/batch/item_list" in url
        if not (is_profile or is_batch):
            return
        try:
            body = resp.json()
        except Exception:
            return
        groups: list[list] = []
        if is_profile:
            groups.append(body.get("storyList") or body.get("itemList") or [])
        if is_batch:
            for entry in body.get("batchStoryItemLists") or []:
                if isinstance(entry, dict):
                    groups.append(entry.get("itemList") or entry.get("ItemList") or [])
        for items in groups:
            mine = [it for it in items if isinstance(it, dict) and _mine(it)]
            if mine:
                captured.append({"items": mine})

    try:
        context.add_init_script(_STEALTH)
        _add_cookies(context, handle, cookies)
        page = context.new_page()
        page.on("response", _on_response)
        page.goto(f"https://www.tiktok.com/@{handle}", wait_until="domcontentloaded", timeout=timeout * 1000)
        # Give the story request time to fire + return after the page settles.
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout, 20) * 1000)
        except Exception:
            pass
        page.wait_for_timeout(2500)
        items: list[dict] = []
        for c in captured:
            items.extend(c["items"])
        if items:
            log.info("[@%s] %d active story item(s) via headless browser", handle, len(items))
        return items
    finally:
        try:
            context.close()
        except Exception:
            pass


_LAUNCH_ARGS = ["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"]

# Use the FULL Chromium build, not Playwright's default headless-shell.
#
# headless-shell is a stripped browser, and TikTok can tell. It serves it a
# skeleton page: the markup comes back with untranslated i18n keys
# ("please_input_search_keyword", "for_you") and no player at all, so a story
# has no <video> element and none of its media is ever requested. The same page
# under the full build renders normally and plays the story. That is the whole
# reason a story visible in a browser could not be downloaded here.
#
# It costs more memory and a slower start, which is a fair price for pages that
# actually render.
_CHANNEL = "chromium"


def _launch(pw):
    """Install a missing full build before trying the headless-shell fallback."""
    try:
        return pw.chromium.launch(headless=True, channel=_CHANNEL, args=_LAUNCH_ARGS)
    except Exception as exc:
        # A cached headless shell can launch successfully even when the full
        # build is missing. Recover here, before that fallback hides the error.
        if "executable doesn" in str(exc).lower() and _install_browser():
            try:
                return pw.chromium.launch(headless=True, channel=_CHANNEL, args=_LAUNCH_ARGS)
            except Exception as retry_exc:
                exc = retry_exc
        log.warning("full Chromium unavailable (%s); falling back to the default build, "
                    "which TikTok serves a degraded page to", str(exc).partition("\n")[0])
        return pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)

# One install attempt per process. A machine with no network would otherwise
# re-download on every launch, and the failure is already handled gracefully.
_install_tried = False


def _install_browser() -> bool:
    """Fetch the full Chromium build Playwright needs, once.

    An installed build ships the Playwright driver but not the browser - it is
    ~300MB, which would more than double the installer for something most users
    already have cached from any other Playwright app. Downloading it on first
    real need keeps the installer small and costs nothing on a machine that has
    it. It lands in PLAYWRIGHT_BROWSERS_PATH, so it survives app reinstalls.
    """
    global _install_tried
    if _install_tried:
        return False
    _install_tried = True

    log.info("Full Chromium missing; fetching it now (about 270MB, once)")
    try:
        # -m playwright works frozen too: bootstrap intercepts `-m` and
        # hands over to the module, the same path the downloaders rely on.
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
        )
    except Exception as exc:
        log.error("browser install failed to start: %s", exc)
        return False

    if result.returncode != 0:
        log.error("browser install failed: %s", (result.stderr or "")[-300:])
        return False
    log.info("Chromium installed")
    return True


def _finish(op: str, target: str, holder: dict, ev: threading.Event, result: Any) -> None:
    """Publish a result to the originator and anyone who joined it, and free the
    slot. Always runs, including for a skipped request - otherwise the queue
    depth would drift upward and admission control would slowly refuse
    everything."""
    global _queue_depth
    with _inflight_lock:
        waiters = _inflight.pop((op, target), [])
        _queue_depth = max(0, _queue_depth - 1)
    holder["result"] = result
    ev.set()
    for w_holder, w_ev in waiters:
        w_holder["result"] = result
        w_ev.set()


def _worker_loop() -> None:
    from playwright.sync_api import sync_playwright

    pw = None
    browser = None
    try:
        pw = sync_playwright().start()
        browser = _launch(pw)
        log.info("Headless Chromium ready for profile fetch")
        while True:
            _, _, item = _req_q.get()
            if item is _SENTINEL:
                break
            op, handle, cookies, timeout, holder, ev, deadline = item
            result = None
            try:
                # The caller has already given up. Doing the work now would cost
                # a full fetch for a result nobody reads, and delay whoever is
                # still waiting - which is precisely how the backlog used to
                # feed itself.
                if time.monotonic() >= deadline:
                    log.debug("[%s] skipping expired %s request", handle, op)
                    continue

                started = time.monotonic()
                if op == "stories":
                    result = _do_fetch_stories(browser, handle, cookies, timeout)
                elif op == "media":
                    result = _do_capture_media(browser, handle, cookies, timeout)
                else:
                    result = _do_fetch(browser, handle, cookies, timeout)
                _note_duration(time.monotonic() - started)
            except Exception as exc:
                log.warning("[%s] browser %s fetch failed: %s", handle, op, exc)
                result = Probe() if op == "profile" else None
            finally:
                _finish(op, handle, holder, ev, result)
    except Exception as exc:  # pragma: no cover
        log.error("browser worker crashed: %s", exc)
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            if pw:
                pw.stop()
        except Exception:
            pass


def _ensure_worker() -> bool:
    global _worker
    with _worker_lock:
        if _worker and _worker.is_alive():
            return True
        if not is_available():
            return False
        _worker = threading.Thread(target=_worker_loop, name="pw-browser", daemon=True)
        _worker.start()
        return True


def _submit(op: str, target: str, cookies: Path, timeout: float, wait: float,
            priority: int) -> Any:
    """Run one browser job, or refuse fast. Returns None when not served.

    Callers that would miss their deadline are turned away here rather than
    blocking for the full wait to discover the same thing - that refusal is what
    keeps a scan sweep moving when the browser is saturated.
    """
    global _queue_depth
    if not _ensure_worker():
        return None

    key = (op, target)
    with _inflight_lock:
        # Someone is already fetching exactly this; wait on their result rather
        # than making the browser do it twice.
        waiters = _inflight.get(key)
        if waiters is not None:
            holder: dict[str, Any] = {}
            ev = threading.Event()
            waiters.append((holder, ev))
            joined = True
        else:
            # Can this be served before the caller gives up? `_queue_depth`
            # ahead of us, each costing roughly `_avg_duration`.
            projected = (_queue_depth + 1) * _avg_duration
            if projected > wait:
                log.debug("[%s] browser busy (%d queued, ~%.0fs each); skipping %s",
                          target, _queue_depth, _avg_duration, op)
                return None
            holder = {}
            ev = threading.Event()
            _inflight[key] = []
            _queue_depth += 1
            joined = False

    if joined:
        if not ev.wait(wait):
            log.warning("[%s] browser %s timed out waiting on a shared fetch", target, op)
            return None
        return holder.get("result")

    deadline = time.monotonic() + wait
    _req_q.put((priority, next(_req_seq),
                (op, target, cookies, timeout, holder, ev, deadline)))
    if not ev.wait(wait):
        log.warning("[%s] browser %s fetch timed out", target, op)
        return None
    return holder.get("result")


def probe_profile_via_browser(handle: str, cookies: Path, timeout: int = 30,
                              priority: int = PRIORITY_BACKGROUND) -> "Probe":
    """Load @handle in headless Chromium and report what TikTok says about it.

    Returns a Probe rather than bare metadata so callers can tell an account
    that is *gone* from one the WAF merely refused to serve. Both look identical
    from yt-dlp, and treating the first as the second is what left deleted
    creators being retried indefinitely.
    """
    handle = handle.lstrip("@")
    return _submit("profile", handle, cookies, timeout, timeout + 30, priority) or Probe()


def fetch_profile_via_browser(handle: str, cookies: Path, timeout: int = 30) -> AccountMetadata | None:
    """Metadata only, for callers that do not care why a fetch came back empty."""
    return probe_profile_via_browser(handle, cookies, timeout).metadata


def fetch_stories_via_browser(handle: str, cookies: Path, timeout: int = 30) -> list[dict] | None:
    """Load @handle in headless Chromium and capture its active story items.

    Returns the raw story items, [] when the creator genuinely has no active
    story, and None when the browser could not serve the request at all. The
    caller needs that distinction: "no stories" is an answer worth remembering,
    while "not served" must not count as a completed check or the creator waits
    a whole interval for a check that never happened.
    """
    handle = handle.lstrip("@")
    return _submit("stories", handle, cookies, timeout, timeout + 30, PRIORITY_BACKGROUND)


# Story pages are inconsistent: the same link yields its media on one load and
# nothing on the next, because the player only starts when TikTok feels like
# serving it. Measured across both a video and a photo story, a single retry
# turns most misses into hits, and a miss is cheap next to failing the download.
_CAPTURE_ATTEMPTS = 2


def capture_media_via_browser(page_url: str, cookies: Path, timeout: int = 45) -> dict:
    """What a page played: {"kind": "video"|"photo", "urls": [...]}, or {}.

    Interactive priority: this only runs when someone is waiting on a download
    that has otherwise failed."""
    for attempt in range(1, _CAPTURE_ATTEMPTS + 1):
        result = _submit("media", page_url, cookies, timeout, timeout + 45,
                         PRIORITY_INTERACTIVE) or {}
        if result.get("urls"):
            return result
        if attempt < _CAPTURE_ATTEMPTS:
            log.info("nothing captured on attempt %d; retrying", attempt)
    return {}


def shutdown() -> None:
    global _worker
    with _worker_lock:
        if _worker and _worker.is_alive():
            _req_q.put((PRIORITY_INTERACTIVE, next(_req_seq), _SENTINEL))
            _worker.join(timeout=10)
        _worker = None
