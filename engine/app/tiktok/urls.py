"""TikTok URL parsing/normalisation helpers. Ported from lora1.py."""

from __future__ import annotations

import hashlib
import re
import threading

from ..core.logging_config import get_logger

log = get_logger("tiktok.urls")

# Short links (vt./vm.tiktok.com) only give up their video id and handle after a
# redirect round-trip, and every caller needs both - so the same link was being
# resolved twice per submission, once by resolve_video_id and again by
# extract_handle. Measured at ~450ms each, that doubled the delay before a
# download could even be queued. Successes only: a failed resolve must stay
# retryable rather than being cached as the unresolved URL.
_CANONICAL_CACHE: dict[str, str] = {}
_CANONICAL_LOCK = threading.Lock()
_CANONICAL_MAX = 512

TIKTOK_RE = re.compile(r"tiktok\.com", re.IGNORECASE)
VIDEO_ID_RE = re.compile(r"/(?:video|photo)/(\d+)")
HANDLE_RE = re.compile(r"tiktok\.com/@([^/?#&]+)", re.IGNORECASE)
SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}
SHORT_PATH = "tiktok.com/t/"


def is_tiktok_url(text: str) -> bool:
    return bool(TIKTOK_RE.search(text or ""))


def resolve_canonical_url(url: str) -> str:
    if not (any(h in url for h in SHORT_HOSTS) or SHORT_PATH in url):
        return url

    with _CANONICAL_LOCK:
        hit = _CANONICAL_CACHE.get(url)
    if hit:
        return hit

    try:
        from curl_cffi import requests as cffi_requests

        resp = cffi_requests.get(url, impersonate="chrome136", allow_redirects=True, timeout=15)
        resolved = str(resp.url)
    except Exception as exc:
        log.warning("redirect resolve failed: %s", exc)
        return url

    with _CANONICAL_LOCK:
        if len(_CANONICAL_CACHE) >= _CANONICAL_MAX:
            _CANONICAL_CACHE.clear()
        _CANONICAL_CACHE[url] = resolved
    return resolved


def resolve_video_id(url: str) -> str:
    m = VIDEO_ID_RE.search(url)
    if m:
        return m.group(1)
    m = VIDEO_ID_RE.search(resolve_canonical_url(url))
    if m:
        return m.group(1)
    return hashlib.md5(url.encode()).hexdigest()[:12]


def identify(url: str) -> tuple[str, str, "object"]:
    """Resolve (handle, video_id, oembed.Post) with the fewest network calls.

    A full post URL carries both ids in its path and costs nothing. A short link
    (vt./vm.tiktok.com - what TikTok's share sheet produces) needs a lookup, and
    oEmbed answers it while also returning the title, cover and author. That
    makes one call do the work of the redirect resolve *and* the separate
    metadata fetch that used to follow it. The redirect stays as the fallback
    for anything oEmbed declines to describe.
    """
    from . import oembed

    m = HANDLE_RE.search(url)
    handle = m.group(1) if m else ""
    m = VIDEO_ID_RE.search(url)
    video_id = m.group(1) if m else ""
    if handle and video_id:
        return handle, video_id, oembed.Post()

    post = oembed.lookup(url)
    handle = handle or post.username
    video_id = video_id or post.video_id
    if handle and video_id:
        return handle, video_id, post

    canonical = resolve_canonical_url(url)
    if not handle:
        m = HANDLE_RE.search(canonical)
        handle = m.group(1) if m else ""
    if not video_id:
        m = VIDEO_ID_RE.search(canonical)
        video_id = m.group(1) if m else hashlib.md5(url.encode()).hexdigest()[:12]
    return handle, video_id, post


def extract_handle(url: str) -> str:
    m = HANDLE_RE.search(url)
    if m:
        return m.group(1)
    m = HANDLE_RE.search(resolve_canonical_url(url))
    return m.group(1) if m else ""
