"""Account profile + recent-post listing via yt-dlp. Ported from monitor.py."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.logging_config import get_logger

log = get_logger("tiktok.listing")


@dataclass
class AccountMetadata:
    nickname: str = ""
    username: str = ""
    account_id: str = ""
    # The TikTok secUid ("MS4wLj…") - a stable, WAF-resistant handle for the
    # account. yt-dlp can list an account's posts via `tiktokuser:<secUid>` even
    # when the normal @handle listing is WAF-blocked or the account is private.
    sec_uid: str = ""
    avatar_url: str | None = None
    total_posts: int = 0
    followers: int | None = None
    following: int | None = None
    private: bool = False


@dataclass
class AccountListing:
    videos: list[dict] = field(default_factory=list)
    metadata: AccountMetadata = field(default_factory=AccountMetadata)
    ok: bool = False          # account resolved (may still have 0 videos)
    not_found: bool = False   # account genuinely doesn't exist / is unreachable
    error: str | None = None  # human-readable reason a failed scan failed


def _utf8_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
        elif isinstance(value, (int, float)):
            text = str(value).strip()
        else:
            continue
        if text:
            return text
    return ""


def _entries(data: dict) -> list[dict]:
    raw = data.get("entries")
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict)]


def _avatar(data: dict) -> str | None:
    thumbs = data.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        last = thumbs[-1]
        if isinstance(last, dict) and last.get("url"):
            return last["url"]
    return data.get("thumbnail")


def _extract_metadata(data: dict, fallback: str) -> AccountMetadata:
    entries = _entries(data)
    first = entries[0] if entries else {}
    nickname = _first_text(data.get("channel"), data.get("uploader"), first.get("channel"))
    username = _first_text(
        data.get("uploader"), data.get("display_id"), first.get("uploader"), fallback
    ).lstrip("@")
    # uploader_id is the numeric account id; channel_id is the secUid ("MS4wLj…").
    account_id = _first_text(data.get("uploader_id"), first.get("uploader_id"))
    sec_uid = _first_text(data.get("channel_id"), first.get("channel_id"))
    followers = data.get("channel_follower_count") or data.get("follower_count")
    total = data.get("playlist_count") or len(entries)
    return AccountMetadata(
        nickname=nickname,
        username=username,
        account_id=account_id,
        sec_uid=sec_uid if sec_uid.startswith("MS4wLj") else "",
        avatar_url=_avatar(data),
        total_posts=int(total) if total else 0,
        followers=int(followers) if followers else None,
    )


def _videos(data: dict, handle: str) -> list[dict]:
    out = []
    for entry in _entries(data):
        vid = _first_text(entry.get("id"))
        if not vid:
            continue
        url = _first_text(entry.get("webpage_url"), entry.get("url"))
        if not url.startswith("http"):
            if not handle:
                # No handle and no URL from yt-dlp: a fabricated one would only
                # fail later, so drop the entry rather than queue a bad job.
                continue
            url = f"https://www.tiktok.com/@{handle}/video/{vid}"
        out.append({
            "id": vid,
            "url": url,
            "title": _first_text(entry.get("title"), entry.get("description"), entry.get("fulltitle")),
        })
    return out


_REHYDRATION_RE = re.compile(
    r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
    re.DOTALL,
)
_PROFILE_ATTEMPTS = 3


def _meta_from_user_info(user: dict, stats: dict, handle: str) -> AccountMetadata | None:
    """Build AccountMetadata from a TikTok userInfo {user, stats} pair."""
    if not (user.get("uniqueId") or user.get("id")):
        return None
    stats = stats or {}
    followers = stats.get("followerCount")
    following = stats.get("followingCount")
    video_count = stats.get("videoCount")
    return AccountMetadata(
        nickname=_first_text(user.get("nickname"), user.get("uniqueId"), handle),
        username=_first_text(user.get("uniqueId"), handle),
        account_id=_first_text(user.get("id")),
        sec_uid=_first_text(user.get("secUid")),
        avatar_url=_first_text(user.get("avatarLarger"), user.get("avatarMedium"), user.get("avatarThumb")) or None,
        total_posts=int(video_count) if str(video_count or "").isdigit() else 0,
        followers=int(followers) if str(followers or "").isdigit() else None,
        following=int(following) if str(following or "").isdigit() else None,
        private=bool(user.get("privateAccount")),
    )


def _profile_from_detail_api(session, handle: str, timeout: int) -> AccountMetadata | None:
    """Fallback source: TikTok's user/detail JSON API (avatar + stats)."""
    import time as _t

    try:
        resp = session.get(
            "https://www.tiktok.com/api/user/detail/",
            params={"uniqueId": handle, "WebIdLastTime": str(int(_t.time()))},
            timeout=timeout,
        )
        if resp.status_code != 200 or "json" not in resp.headers.get("content-type", "").lower():
            return None
        if not resp.text.strip():
            return None
        info = resp.json().get("userInfo", {})
        meta = _meta_from_user_info(info.get("user", {}), info.get("stats", {}) or info.get("statsV2", {}), handle)
        if meta:
            log.debug("[@%s] profile resolved via detail API", handle)
        return meta
    except Exception as exc:
        log.debug("[@%s] detail API fallback failed: %s", handle, exc)
        return None


def fetch_profile(handle: str, cookies: Path, timeout: int = 15, use_browser: bool = False) -> AccountMetadata | None:
    """
    Fetch a creator's profile metadata (nickname, avatar, uid, follower/video
    counts) - reliable even for 0-video / private / region-locked accounts.

    Strategy:
      0. Headless browser (when use_browser=True) - the only path that reliably
         beats TikTok's SlardarWAF, so it yields avatar + follower count.
      1. Profile page rehydration JSON via HTTP (usually WAF-blocked now).
      2. Anchored regex on the page JSON (partial).
      3. The user/detail JSON API.

    Returns AccountMetadata, or None only if the account can't be reached at all.
    """
    import time as _t

    from curl_cffi import requests as cffi_requests

    from .resolver import BASE_HEADERS, load_netscape_cookies

    if use_browser:
        try:
            from . import browser as _browser

            bmeta = _browser.fetch_profile_via_browser(handle, cookies, timeout=max(30, timeout))
            if bmeta:
                return bmeta
        except Exception as exc:
            log.debug("[@%s] browser profile fetch errored: %s", handle, exc)

    headers = dict(BASE_HEADERS)
    headers["Referer"] = "https://www.tiktok.com/"
    session = cffi_requests.Session(impersonate="chrome136")
    session.headers.update(headers)
    if cookies.exists():
        try:
            load_netscape_cookies(session, cookies)
        except Exception as exc:
            log.debug("[@%s] cookie load failed: %s", handle, exc)

    partial: AccountMetadata | None = None

    for attempt in range(_PROFILE_ATTEMPTS):
        try:
            resp = session.get(f"https://www.tiktok.com/@{handle}", timeout=timeout, allow_redirects=True)
            html = resp.content.decode("utf-8", errors="replace")
        except Exception as exc:
            log.debug("[@%s] profile fetch attempt %d failed: %s", handle, attempt + 1, exc)
            html = ""

        # 1) full rehydration JSON
        m = _REHYDRATION_RE.search(html)
        if m:
            try:
                blob = json.loads(m.group(1))
                info = blob.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {}).get("userInfo", {})
                meta = _meta_from_user_info(
                    info.get("user", {}), info.get("stats", {}) or info.get("statsV2", {}), handle
                )
                if meta:
                    return meta
            except Exception as exc:
                log.debug("[@%s] rehydration parse failed: %s", handle, exc)

        # 2) anchored regex (account exists but JSON shape unexpected)
        uid_m = re.search(r'"uniqueId"\s*:\s*"' + re.escape(handle) + r'"[^{}]{0,300}?"id"\s*:\s*"(\d+)"', html, re.IGNORECASE)
        if uid_m and partial is None:
            nick_m = re.search(r'"uniqueId"\s*:\s*"' + re.escape(handle) + r'"[^{}]{0,300}?"nickname"\s*:\s*"([^"]+)"', html, re.IGNORECASE)
            partial = AccountMetadata(
                nickname=nick_m.group(1) if nick_m else handle,
                username=handle,
                account_id=uid_m.group(1),
            )

        # Got a challenge / empty page - brief backoff then retry.
        if attempt < _PROFILE_ATTEMPTS - 1:
            _t.sleep(1.2 * (attempt + 1))

    # 3) JSON API fallback (often succeeds when the HTML page is challenge-walled)
    api_meta = _profile_from_detail_api(session, handle, timeout)
    if api_meta:
        return api_meta

    return partial


def _ytdlp_reason(stderr: str, handle: str = "") -> str | None:
    """Extract a concise, user-facing reason from yt-dlp's stderr - the trailing
    `ERROR: …` line if present, else the stderr tail. Strips yt-dlp's
    `[tiktok:user] handle:` prefix noise and its developer-oriented "If you are
    able to…" hint so what surfaces in the UI is just the gist (e.g. "Unable to
    extract secondary user ID")."""
    for raw in reversed((stderr or "").splitlines()):
        line = raw.strip()
        if not line.startswith("ERROR:"):
            continue
        line = line[len("ERROR:"):].strip()
        line = re.sub(r"^\[[^\]]+\]\s*", "", line)  # drop [tiktok:user] extractor tag
        if handle and line.lower().startswith(handle.lower() + ":"):
            line = line[len(handle) + 1:].strip()
        line = re.split(r"\.\s+If you\b", line, maxsplit=1)[0].rstrip(". ")
        return line or None
    tail = (stderr or "").strip()
    return tail[-200:] if tail else None


def _ytdlp_videos(
    username: str, cookies: Path, max_posts: int, timeout: int
) -> tuple[list[dict], AccountMetadata | None, str | None]:
    """Run yt-dlp's flat-playlist listing. Returns (videos, metadata-or-None,
    failure-reason-or-None)."""
    from .resolver import ytdlp_cookie_copy

    handle = username.lstrip("@")
    # yt-dlp rewrites the --cookies file on exit; hand it a disposable copy so it
    # can't strip the login session from the user's real cookies.txt.
    try:
        with ytdlp_cookie_copy(cookies) as ck:
            cmd = [sys.executable, "-m", "yt_dlp"]
            if ck:
                cmd += ["--cookies", str(ck)]
            cmd += [
                "--flat-playlist", "--playlist-end", str(max_posts),
                "--dump-single-json", "--skip-download",
                "--no-warnings", "--quiet", f"https://www.tiktok.com/@{handle}",
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=_utf8_env(), timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        log.warning("[%s] yt-dlp listing timed out after %ds", username, timeout)
        return [], None, f"yt-dlp listing timed out after {timeout}s"

    if result.returncode != 0:
        # Non-zero is expected for accounts with no public videos - not an error.
        reason = _ytdlp_reason(result.stderr, handle)
        log.debug("[%s] yt-dlp listing non-zero: %s", username, (result.stderr or "")[-200:])
        return [], None, reason
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], None, "yt-dlp returned no parseable listing"
    return _videos(data, handle), _extract_metadata(data, fallback=handle), None


# A secUid ("MS4wLj…") is an opaque account id, not a handle. tiktok.com/@<secUid>
# is not a real page, so a post URL built from one can never be downloaded.
def _looks_like_secuid(value: str) -> bool:
    return value.startswith("MS4wLj") or len(value) > 60


def _ytdlp_videos_by_secuid(sec_uid: str, cookies: Path, max_posts: int, timeout: int,
                            handle_hint: str = "") -> list[dict]:
    """List an account's posts via yt-dlp's `tiktokuser:<secUid>` extractor. This
    path works when the normal @handle listing is WAF-blocked, and - crucially -
    for PRIVATE accounts the logged-in cookies follow, which the @handle listing
    can't enumerate. Returns [] on any failure."""
    from .resolver import ytdlp_cookie_copy

    try:
        with ytdlp_cookie_copy(cookies) as ck:
            cmd = [sys.executable, "-m", "yt_dlp"]
            if ck:
                cmd += ["--cookies", str(ck)]
            cmd += [
                "--flat-playlist", "--playlist-end", str(max_posts),
                "--dump-single-json", "--skip-download",
                "--no-warnings", "--quiet", f"tiktokuser:{sec_uid}",
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=_utf8_env(), timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        log.warning("[secUid:%s] tiktokuser listing timed out after %ds", sec_uid[:12], timeout)
        return []
    if result.returncode != 0:
        log.debug("[secUid:%s] tiktokuser listing non-zero: %s", sec_uid[:12], (result.stderr or "")[-200:])
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    # entries carry uploader/handle; derive it for building canonical URLs.
    #
    # The `tiktokuser:` extractor reports the secUid as `uploader`, so taking it
    # at face value produced URLs like tiktok.com/@MS4wLj…/video/<id>. TikTok
    # serves nothing for those, so every post found through this path failed with
    # an unexplained extractor error and was retried until it ran out of
    # attempts. Prefer the caller's known handle, and refuse a secUid outright.
    handle = _first_text(data.get("uploader"), (_entries(data)[0].get("uploader") if _entries(data) else ""))
    handle = handle.lstrip("@")
    if not handle or _looks_like_secuid(handle):
        handle = (handle_hint or "").lstrip("@")
    if not handle or _looks_like_secuid(handle):
        # Without a usable handle the URLs would be broken; yt-dlp's own
        # per-entry webpage_url (when present) is still fine, so pass an empty
        # handle and let _videos keep only what it can build correctly.
        log.warning("[secUid:%s] no usable handle for URL building; "
                    "falling back to yt-dlp's own post URLs", sec_uid[:12])
        handle = ""
    return _videos(data, handle)


def fetch_all_detailed(
    username: str, cookies: Path, timeout: int = 120, hard_cap: int = 2000,
    sec_uid: str = "", use_browser: bool = False,
) -> tuple[list[dict], str]:
    """The account's entire (bounded) back-catalogue, plus the secUid used/found.

    Mirrors `fetch_account`'s resilience - which the old one-line `fetch_all` had
    none of, so a WAF-blocked or private-followed account listed as ZERO posts and
    "download all" reported nothing to do even when the creator had videos:

      1. The plain `@handle` listing (frequently WAF-blocked now, and it can never
         enumerate a private account you follow).
      2. On an empty result, re-list via `tiktokuser:<secUid>` - the WAF-resistant
         path that also covers private accounts - using the caller's stored secUid
         or one resolved from the profile (optionally via the headless browser,
         which is the WAF-proof source).

    Returns (videos, sec_uid) so the caller can persist a newly discovered secUid.
    """
    handle = username.lstrip("@")
    vids, ytdlp_meta, _ = _ytdlp_videos(username, cookies, hard_cap, timeout)
    effective = sec_uid or (ytdlp_meta.sec_uid if ytdlp_meta else "")
    if vids:
        return vids, effective

    # Nothing on the @handle path - resolve a secUid if we don't already have one.
    if not effective:
        profile = fetch_profile(handle, cookies, min(timeout, 30), use_browser=use_browser)
        effective = profile.sec_uid if profile else ""

    if effective:
        by_sec = _ytdlp_videos_by_secuid(effective, cookies, hard_cap, timeout,
                                         handle_hint=username)
        if by_sec:
            log.info("[%s] full listing recovered via secUid (%d post(s))", username, len(by_sec))
            return by_sec, effective

    return vids, effective


def fetch_all(
    username: str, cookies: Path, timeout: int = 120, hard_cap: int = 2000,
    sec_uid: str = "", use_browser: bool = False,
) -> list[dict]:
    """Fetch the account's entire (bounded) back-catalogue of posts."""
    vids, _ = fetch_all_detailed(username, cookies, timeout, hard_cap, sec_uid, use_browser)
    return vids


# Exact wording matters: the monitor matches on it to stop retrying a creator
# that TikTok says is gone, and the app turns it into the message a person sees.
ACCOUNT_GONE = "Account no longer exists on TikTok"


def fetch_account(
    username: str, cookies: Path, max_posts: int = 10, timeout: int = 15,
    use_browser: bool = False, sec_uid: str = "",
) -> AccountListing:
    """
    Resolve an account: authoritative profile metadata + recent video listing.

    The profile fetch is the source of truth for existence and metadata (so
    accounts with 0 videos / private / region-locked still resolve). yt-dlp
    supplies the actual video list. When the normal @handle listing yields no
    videos (WAF-blocked or private-followed accounts), we re-list via
    `tiktokuser:<secUid>` - the secUid comes from the profile metadata or is
    passed in from a previous scan. With use_browser=True the profile is fetched
    via headless Chromium (WAF-proof metadata + secUid).
    """
    handle = username.lstrip("@")

    # When escalating to the browser, ask the browser FIRST.
    #
    # The old order ran the yt-dlp listing before anything else, even on a retry
    # whose whole purpose was to get past the WAF that had just blocked yt-dlp.
    # So every failing creator paid a full listing timeout before the browser was
    # consulted, on every sweep - the bulk of why scans crawled. And because
    # TikTok only states "this account is gone" in the page the browser loads, a
    # deleted creator was retried forever under a bot-check message.
    browser_probe = None
    if use_browser:
        try:
            from . import browser as _browser

            browser_probe = _browser.probe_profile_via_browser(handle, cookies, timeout=max(30, timeout))
            if browser_probe.private_account and not browser_probe.gone:
                # Private is a real, healthy state. Recording it stops the
                # generic failure path from ever reaching a "gone" conclusion
                # for a creator who is simply not sharing with this account.
                log.info("[%s] account is private (TikTok statusCode=%s)",
                         username, browser_probe.status_code)
                meta = browser_probe.metadata or AccountMetadata(username=handle)
                meta.private = True
                return AccountListing(metadata=meta, ok=True, videos=[])
            if browser_probe.gone:
                log.info("[%s] account no longer exists (TikTok statusCode=%s)",
                         username, browser_probe.status_code)
                return AccountListing(
                    metadata=AccountMetadata(username=handle),
                    ok=False, not_found=True, error=ACCOUNT_GONE,
                )
        except Exception as exc:
            log.debug("[@%s] browser probe errored: %s", handle, exc)

    vids, ytdlp_meta, ytdlp_reason = _ytdlp_videos(username, cookies, max_posts, timeout)
    profile = (browser_probe.metadata if browser_probe and browser_probe.metadata
               else fetch_profile(handle, cookies, timeout, use_browser=use_browser))

    # The secUid unlocks the WAF-resistant / private listing path. Prefer a freshly
    # fetched one; fall back to the caller-provided (stored) value.
    effective_secuid = (profile.sec_uid if profile else "") or (ytdlp_meta.sec_uid if ytdlp_meta else "") or sec_uid

    # If the normal listing found nothing but we have a secUid, list via tiktokuser.
    if not vids and effective_secuid:
        by_sec = _ytdlp_videos_by_secuid(effective_secuid, cookies, max_posts, timeout,
                                         handle_hint=handle)
        if by_sec:
            vids = by_sec

    if profile is None and ytdlp_meta is None and not vids:
        # Neither source found the account → genuinely not found / unreachable.
        # Surface yt-dlp's own diagnosis when it has one (e.g. "Unable to extract
        # secondary user ID" - the signature of a deleted/banned/renamed handle)
        # so the failure isn't reduced to an opaque "not found".
        reason = ytdlp_reason or "profile page and listing both unreachable"
        return AccountListing(
            metadata=AccountMetadata(username=handle), ok=False, not_found=True, error=reason
        )

    # Merge: prefer profile (authoritative), fall back to yt-dlp values.
    meta = profile or ytdlp_meta or AccountMetadata(username=handle)
    if ytdlp_meta:
        meta.nickname = meta.nickname or ytdlp_meta.nickname
        meta.account_id = meta.account_id or ytdlp_meta.account_id
        meta.avatar_url = meta.avatar_url or ytdlp_meta.avatar_url
        if not meta.followers and ytdlp_meta.followers:
            meta.followers = ytdlp_meta.followers
    meta.sec_uid = meta.sec_uid or effective_secuid
    if not meta.total_posts:
        meta.total_posts = len(vids)
    meta.username = meta.username or handle

    log.info("[%s] resolved (%d video(s), %d posts, private=%s)", username, len(vids), meta.total_posts, meta.private)
    return AccountListing(videos=vids, metadata=meta, ok=True)
