"""Platform routing for the universal downloader.

A pasted URL is classified into one of three engines:

    tiktok     → tikdownloader.io, falling back to yt-dlp   (app/tiktok)
    instagram  → gallery-dl (yt-dlp refuses IG image posts) (app/instagram)
    generic    → yt-dlp, which covers every other supported site

Only TikTok creators are monitored; everything else is an on-demand download
target (``monitored=False``), matching how Instagram already behaves.
"""

from __future__ import annotations

import re

TIKTOK = "tiktok"
INSTAGRAM = "instagram"
X = "x"
GENERIC = "generic"

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def looks_like_url(text: str) -> bool:
    return bool(_URL_RE.match((text or "").strip()))


def detect(url: str) -> str:
    """Classify a URL into the engine that should handle it."""
    from .instagram import urls as ig_urls
    from .x import urls as x_urls
    from .tiktok import urls as tt_urls

    url = (url or "").strip()
    if tt_urls.is_tiktok_url(url):
        return TIKTOK
    if ig_urls.is_instagram_url(url):
        return INSTAGRAM
    if x_urls.is_x_url(url):
        return X
    return GENERIC


# Folder-friendly display names. Keys are the stored `User.platform` values
# (yt-dlp extractor keys, lowercased). Anything unlisted falls back to a
# capitalised form, so a newly supported site still gets a sensible folder.
_DISPLAY_NAMES = {
    TIKTOK: "TikTok",
    INSTAGRAM: "Instagram",
    "youtube": "YouTube",
    "twitter": "Twitter",
    "x": "X",
    "facebook": "Facebook",
    "reddit": "Reddit",
    "twitch": "Twitch",
    "vimeo": "Vimeo",
    "soundcloud": "SoundCloud",
    "dailymotion": "Dailymotion",
    "pinterest": "Pinterest",
    "tumblr": "Tumblr",
    "bilibili": "Bilibili",
    "vk": "VK",
    GENERIC: "Other",
}


def display_name(platform: str) -> str:
    """The download-folder name for a platform (``TikTok``, ``YouTube``, …)."""
    key = (platform or GENERIC).strip().lower()
    return _DISPLAY_NAMES.get(key) or key.capitalize() or "Other"


def state_key(platform: str, handle: str) -> str:
    """Stable, platform-scoped creator key.

    TikTok keeps its historical bare ``@handle`` form so existing rows keep
    matching; every other platform is namespaced (``yt:@handle``).
    """
    handle = (handle or "").lstrip("@")
    if platform == TIKTOK:
        return f"@{handle}"
    if platform == INSTAGRAM:
        return f"ig:@{handle}"
    return f"{platform}:@{handle}"


# Where a creator's page lives, per platform. Kept here rather than in the
# caller so the Telegram captions, the bot's buttons and anything added later
# all point at the same place.
_PROFILE_URLS = {
    TIKTOK: "https://www.tiktok.com/@{handle}",
    INSTAGRAM: "https://www.instagram.com/{handle}/",
    X: "https://x.com/{handle}",
}


def profile_url(platform: str, handle: str) -> str:
    """A creator's profile page, or '' when the platform has no known form.

    Returning '' rather than guessing matters: a caption that links a handle to
    a URL pattern invented for an unsupported site sends people to a 404 with
    the bot's name on it.
    """
    handle = (handle or "").strip().lstrip("@").strip("/")
    if not handle:
        return ""
    template = _PROFILE_URLS.get((platform or "").strip().lower())
    return template.format(handle=handle) if template else ""
