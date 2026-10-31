"""UID ↔ handle resolution and cookie loading. Ported from monitor.py / extract_uids.py."""

from __future__ import annotations

import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from ..core.logging_config import get_logger

log = get_logger("tiktok.resolver")


@contextmanager
def ytdlp_cookie_copy(cookies: Path | None) -> Iterator[Path | None]:
    """Yield a throwaway COPY of the cookies file to hand to yt-dlp's --cookies.

    yt-dlp writes the cookie jar back to the --cookies file after every run,
    persisting the site's *response* cookies. For TikTok that response is the
    anti-bot / WAF token set (ttwid, msToken, _waftokenid, …) and it does NOT
    include the HttpOnly login cookies (sessionid, sid_tt, …) - so yt-dlp would
    overwrite the user's real cookies.txt and strip its login session, silently
    breaking private-account access on the very next scan. Copying to a temp
    file means any write-back lands on the disposable copy; the real file is
    never touched. Yields None when there's no cookie file (public-only).
    """
    if not cookies or not cookies.exists():
        yield None
        return
    tmpdir = Path(tempfile.mkdtemp(prefix="lora1-ck-"))
    tmp: Path | None = tmpdir / "cookies.txt"
    try:
        # ONLY the copy is guarded, and the yield sits outside that guard.
        #
        # It used to wrap the yield too, which made this swallow the caller's
        # failures instead of its own: a yt-dlp timeout inside the `with` body
        # was thrown back in at the yield, caught here, logged as "cookie copy
        # failed ... running yt-dlp without cookies" (it never re-ran), and then
        # the second yield turned it into "generator didn't stop after throw()".
        # So every scan timeout surfaced as a bogus cookie error with the real
        # cause destroyed - which is what stranded accounts on "could not be
        # scanned after 3 attempts" and made authenticated fetches look like a
        # cookie problem.
        try:
            shutil.copy2(cookies, tmp)
        except Exception as exc:
            log.warning("could not copy the cookie file (%s); running yt-dlp without cookies", exc)
            tmp = None
        yield tmp
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

UID_RE = re.compile(r"^\d{5,20}$")

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def is_uid(value: str) -> bool:
    return bool(UID_RE.match(value.strip()))


def load_netscape_cookies(session, cookies_path: Path) -> None:
    for line in cookies_path.read_text(encoding="utf-8").splitlines():
        line = line.rstrip("\n")
        # Browser exports prefix HttpOnly cookies with "#HttpOnly_" - these are
        # real cookie lines (TikTok's auth cookies sessionid/sid_tt/sid_guard are
        # all HttpOnly), so we must NOT skip them as comments.
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("\t")
        if len(parts) != 7:
            continue
        domain, _flag, _path, _secure, _expiry, name, value = parts
        session.cookies.set(name, value, domain=domain)


def _session(cookies: Path):
    from curl_cffi import requests as cffi_requests

    headers = dict(BASE_HEADERS)
    headers["Referer"] = "https://www.tiktok.com/"
    s = cffi_requests.Session(impersonate="chrome136")
    s.headers.update(headers)
    if cookies.exists():
        try:
            load_netscape_cookies(s, cookies)
        except Exception as exc:
            log.debug("could not load cookies: %s", exc)
    return s


def _extract_handle_for_uid(html: str, uid: str) -> Optional[str]:
    uid_esc = re.escape(uid)
    m = re.search(r'"id"\s*:\s*"' + uid_esc + r'"[^{}]{0,200}?"uniqueId"\s*:\s*"([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'"uniqueId"\s*:\s*"([^"]+)"[^{}]{0,200}?"id"\s*:\s*"' + uid_esc + r'"', html)
    return m.group(1) if m else None


def _extract_uid_for_handle(html: str, handle: str) -> Optional[str]:
    handle_esc = re.escape(handle)
    m = re.search(
        r'"uniqueId"\s*:\s*"' + handle_esc + r'"[^{}]{0,200}?"id"\s*:\s*"(\d+)"', html, re.IGNORECASE
    )
    if m:
        return m.group(1)
    m = re.search(
        r'"id"\s*:\s*"(\d+)"[^{}]{0,200}?"uniqueId"\s*:\s*"' + handle_esc + r'"', html, re.IGNORECASE
    )
    return m.group(1) if m else None


def _resolve_uid_via_browser(uid: str, cookies: Path, timeout: int) -> Optional[str]:
    """Resolve uid → current @handle through the headless browser.

    Loading ``tiktok.com/@<uid>`` redirects to the account's canonical handle
    URL; a real browser resolves TikTok's WAF challenge (which the plain-HTTP
    strategies above no longer beat) and exposes the rehydration JSON, whose
    ``uniqueId`` is the current handle. This is the reliable path for detecting a
    creator who renamed. Returns None when the browser is unavailable or the
    account can't be resolved (deleted / banned)."""
    try:
        from . import browser
    except Exception:  # pragma: no cover
        return None
    if not browser.is_available():
        return None
    try:
        meta = browser.fetch_profile_via_browser(uid, cookies, timeout=max(30, timeout))
    except Exception as exc:
        log.debug("[uid:%s] browser resolution failed: %s", uid, exc)
        return None
    if meta and meta.username:
        handle = meta.username.strip().lstrip("@")
        if handle and handle.lower() != uid.lower():
            return handle
    return None


def resolve_uid_to_username(
    uid: str, cookies: Path, timeout: int = 15, use_browser: bool = False
) -> Optional[str]:
    """Resolve a numeric TikTok User ID to its current @handle (no leading @).

    Tries the cheap HTTP strategies first; with ``use_browser=True`` falls back
    to a headless-browser resolution when they come up empty (they are routinely
    WAF-blocked now), which is what actually recovers a renamed creator.
    """
    handle = _resolve_uid_http(uid, cookies, timeout)
    if handle:
        return handle
    if use_browser:
        return _resolve_uid_via_browser(uid, cookies, timeout)
    return None


def _resolve_uid_http(uid: str, cookies: Path, timeout: int = 15) -> Optional[str]:
    """HTTP-only uid → handle resolution (profile redirect, then detail API)."""
    session = _session(cookies)
    try:
        resp = session.get(f"https://www.tiktok.com/@{uid}", timeout=timeout, allow_redirects=True)
        final_url = str(resp.url)
        m = re.search(r"tiktok\.com/@([^/?#]+)", final_url)
        if m and m.group(1) != uid:
            return m.group(1)
        handle = _extract_handle_for_uid(resp.text, uid)
        if handle:
            return handle
    except Exception as exc:
        log.debug("[uid:%s] strategy 1 failed: %s", uid, exc)

    # Strategy 2 (fallback): internal user/detail API. This endpoint is flaky
    # without browser-injected tokens and routinely answers with an empty body
    # or a Cloudflare/anti-bot HTML challenge - especially on networks TikTok
    # rate-limits. Those are expected misses, so they're logged at DEBUG rather
    # than spamming WARNING; callers treat a None result as "skip this poll".
    try:
        resp = session.get(
            "https://www.tiktok.com/api/user/detail/",
            params={"WebIdLastTime": str(int(time.time())), "uid": uid},
            timeout=timeout,
        )
        content_type = resp.headers.get("content-type", "").lower()
        if resp.status_code != 200 or "json" not in content_type or not resp.text.strip():
            log.debug(
                "[uid:%s] detail API unavailable (status=%s, content-type=%s, len=%d)",
                uid, resp.status_code, content_type or "?", len(resp.text),
            )
            return None
        try:
            data = resp.json()
        except ValueError:
            log.debug("[uid:%s] detail API returned a non-JSON body (likely a challenge page)", uid)
            return None
        user_obj = data.get("userInfo", {}).get("user", {})
        if user_obj.get("id") == uid:
            return user_obj.get("uniqueId")
        log.debug("[uid:%s] detail API had no matching user object", uid)
    except Exception as exc:
        log.debug("[uid:%s] detail API request failed: %s", uid, exc)
    return None


def resolve_handle_to_uid(handle: str, cookies: Path, timeout: int = 15) -> Optional[str]:
    """Resolve an @handle to its numeric TikTok User ID."""
    handle = handle.lstrip("@")
    session = _session(cookies)
    try:
        resp = session.get(f"https://www.tiktok.com/@{handle}", timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            log.warning("[@%s] profile fetch HTTP %s", handle, resp.status_code)
            return None
        return _extract_uid_for_handle(resp.text, handle)
    except Exception as exc:
        log.warning("[@%s] resolve failed: %s", handle, exc)
        return None
