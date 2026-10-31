"""Instagram URL parsing helpers.

The four shapes that matter, and what gallery-dl does with each:

    /<user>/                              instagram:user        posts
    /<user>/highlights/                   instagram:highlights  every highlight
    /stories/<user>/                      instagram:stories     active stories
    /stories/highlights/<id>/             instagram:highlights  one highlight
    /p/<code>/  /reel/<code>/  /tv/<code> instagram:post|reel   one post

Verified against gallery_dl.extractor.find(), which is what actually runs.
"""

from __future__ import annotations

import re

_IG_RE = re.compile(r"instagram\.com", re.IGNORECASE)
_POST_RE = re.compile(r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", re.IGNORECASE)

# A shared highlight is /stories/highlights/<numeric id>/. It has to be tested
# before the story pattern, and excluded from it: `stories/([A-Za-z0-9_.]+)`
# happily matched the literal segment "highlights" and reported it as the
# username, so a highlight link was fetched as the stories of a user named
# "highlights" and came back empty.
_HIGHLIGHT_ID_RE = re.compile(r"instagram\.com/stories/highlights/(\d+)", re.IGNORECASE)
_USER_HIGHLIGHTS_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]+)/highlights", re.IGNORECASE)
_STORY_RE = re.compile(r"instagram\.com/stories/(?!highlights)([A-Za-z0-9_.]+)", re.IGNORECASE)
_USER_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]+)/?(?:[?#]|$)", re.IGNORECASE)

# Path segments that are NOT usernames.
_RESERVED = {
    "p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct", "s",
    "highlights", "saved", "tagged",
}

# What a link is. `kind` is the single place that decides, so the bot and the
# service cannot disagree about what they were handed.
POST = "post"
STORY = "story"
HIGHLIGHT = "highlight"
PROFILE = "profile"


def is_instagram_url(text: str) -> bool:
    return bool(_IG_RE.search(text or ""))


def extract_shortcode(url: str) -> str:
    """The post/reel shortcode, or '' for a profile/story URL."""
    m = _POST_RE.search(url or "")
    return m.group(1) if m else ""


def extract_username(url: str) -> str:
    """The @username for a profile, story or highlights URL, or '' otherwise.

    A shared highlight (/stories/highlights/<id>/) carries no username at all -
    the id is the only thing in it - so this correctly returns ''.
    """
    url = url or ""
    m = _USER_HIGHLIGHTS_RE.search(url)
    if m and m.group(1).lower() not in _RESERVED:
        return m.group(1)
    m = _STORY_RE.search(url)
    if m and m.group(1).lower() not in _RESERVED:
        return m.group(1)
    m = _USER_RE.search(url)
    if m and m.group(1).lower() not in _RESERVED:
        return m.group(1)
    return ""


def is_highlight_url(url: str) -> bool:
    url = url or ""
    return bool(_HIGHLIGHT_ID_RE.search(url) or _USER_HIGHLIGHTS_RE.search(url))


def is_story_url(url: str) -> bool:
    return bool(_STORY_RE.search(url or ""))


def is_profile_url(url: str) -> bool:
    """True for a bare profile URL: no post, no story, no highlight."""
    if extract_shortcode(url) or is_story_url(url) or is_highlight_url(url):
        return False
    return bool(extract_username(url))


def kind(url: str) -> str:
    """Which of the four shapes this is, or '' if it is none of them.

    Ordered most specific first: a highlight URL also looks like a story URL
    until highlights have been ruled out.
    """
    if not is_instagram_url(url):
        return ""
    if extract_shortcode(url):
        return POST
    if is_highlight_url(url):
        return HIGHLIGHT
    if is_story_url(url):
        return STORY
    if extract_username(url):
        return PROFILE
    return ""


# ── building the URLs gallery-dl expects ──
def profile_url(username: str) -> str:
    return f"https://www.instagram.com/{username.lstrip('@').strip('/')}/"


def highlights_url(username: str) -> str:
    return f"https://www.instagram.com/{username.lstrip('@').strip('/')}/highlights/"


def stories_url(username: str) -> str:
    return f"https://www.instagram.com/stories/{username.lstrip('@').strip('/')}/"
