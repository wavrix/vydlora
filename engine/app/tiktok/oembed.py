"""Public TikTok oEmbed lookup: one call describes a post and its author.

Shared so every route into a download records the same metadata. It returns not
just the title and cover but the author's handle and display name, which is
enough to file a one-off download under the right creator folder without the
browser profile fetch that path used to require.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.logging_config import get_logger

log = get_logger("tiktok.oembed")

_AUTHOR_RE = re.compile(r"tiktok\.com/@([^/?#&]+)", re.IGNORECASE)
_VIDEO_ID_RE = re.compile(r'data-video-id="(\d+)"')


@dataclass(frozen=True)
class Post:
    """What oEmbed knows about a post. Every field is best-effort: metadata is
    never worth failing a download over, so callers get empty strings instead of
    an exception."""

    title: str = ""
    thumbnail: str = ""
    username: str = ""
    nickname: str = ""
    video_id: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.username)


def lookup(url: str) -> Post:
    try:
        from curl_cffi import requests as cffi_requests

        resp = cffi_requests.get(
            "https://www.tiktok.com/oembed", params={"url": url},
            impersonate="chrome136", timeout=5,
        )
        if resp.status_code != 200:
            return Post()
        data = resp.json() or {}
    except Exception as exc:
        log.debug("oembed lookup failed for %s: %s", url, exc)
        return Post()

    author = _AUTHOR_RE.search(data.get("author_url") or "")
    # The embed markup names the post, so a short link's id comes back from the
    # same call rather than a separate redirect round-trip.
    vid = _VIDEO_ID_RE.search(data.get("html") or "")
    return Post(
        title=data.get("title") or "",
        thumbnail=data.get("thumbnail_url") or "",
        username=author.group(1) if author else "",
        # oEmbed's author_name is the display name, the same value the full
        # profile fetch reports as `nickname` - so the folder name matches.
        nickname=data.get("author_name") or "",
        video_id=vid.group(1) if vid else "",
    )
