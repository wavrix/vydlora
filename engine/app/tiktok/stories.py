"""TikTok Stories - list active stories and download their media.

Stories are followers-only and ephemeral (~24h). They are NOT exposed by yt-dlp
and TikTok's story API needs signed requests, so listing runs through the
logged-in headless browser (see browser.fetch_stories_via_browser), which lets
the page's own JS sign the /api/story/item_list call. The story item JSON is
shaped like a normal post item, so each carries direct (signed) CDN media URLs
we can download with an impersonated HTTP client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..core.logging_config import get_logger
from .browser import fetch_stories_via_browser

log = get_logger("tiktok.stories")


@dataclass
class StoryItem:
    story_id: str
    kind: str  # "video" | "photo"
    media_urls: list[str] = field(default_factory=list)  # video: 1 url; photo: N urls
    cover_url: str | None = None
    created_at: int = 0
    expires_at: int = 0  # epoch ms
    desc: str = ""
    # Author privacy - routes the download: public stories go through
    # tikdownloader.io (clean, no session), private ones use the direct method.
    private: bool = False


def _first_url(url_obj) -> str | None:
    """TikTok media urls come as {'urlList': [mirror1, mirror2, ...]} or a str."""
    if isinstance(url_obj, dict):
        lst = url_obj.get("urlList") or url_obj.get("UrlList") or []
        return lst[0] if lst else None
    if isinstance(url_obj, str) and url_obj.startswith("http"):
        return url_obj
    return None


def parse_story_item(item: dict) -> StoryItem | None:
    """Normalise a raw TikTok story item into a StoryItem with media URLs."""
    story_id = str(item.get("id") or "")
    if not story_id:
        return None
    story_meta = item.get("story") or {}
    created = int(item.get("createTime") or 0)
    expires = int(story_meta.get("ExpiredAt") or 0)
    desc = item.get("desc") or ""
    private = bool((item.get("author") or {}).get("privateAccount"))

    image_post = item.get("imagePost") or {}
    if image_post.get("images"):
        urls: list[str] = []
        for img in image_post["images"]:
            u = _first_url(img.get("imageURL"))
            if u:
                urls.append(u)
        cover = _first_url((image_post.get("cover") or {}).get("imageURL"))
        if urls:
            return StoryItem(story_id, "photo", urls, cover, created, expires, desc, private)

    video = item.get("video") or {}
    if video:
        # Prefer the explicit download address, then playAddr, then the first
        # bitrate mirror. All are signed CDN URLs. Each field may be a plain
        # string OR a {'urlList': [...]} object depending on the payload shape -
        # _first_url handles both (the old code assumed a string, so object-form
        # addresses produced a bad URL and the story silently failed to download).
        url = None
        for key in ("downloadAddr", "playAddr", "download_addr", "play_addr"):
            u = _first_url(video.get(key))
            if u:
                url = u
                break
        if not url:
            for br in video.get("bitrateInfo") or video.get("bitrate_info") or []:
                u = _first_url(br.get("PlayAddr") or br.get("play_addr"))
                if u:
                    url = u
                    break
        cover = _first_url(video.get("cover")) or _first_url(video.get("originCover"))
        if url:
            return StoryItem(story_id, "video", [url], cover, created, expires, desc, private)

    log.debug("story %s had no downloadable media", story_id)
    return None


def list_stories(handle: str, cookies: Path, timeout: int = 30) -> list[StoryItem] | None:
    """The account's currently-active story items.

    [] when there are none, None when the browser could not run the check."""
    raw = fetch_stories_via_browser(handle, cookies, timeout=max(30, timeout))
    if raw is None:
        return None
    # De-dupe by story id (the capture can see the same item across responses).
    out: list[StoryItem] = []
    seen_ids: set[str] = set()
    dropped = 0
    for item in raw:
        parsed = parse_story_item(item)
        if not parsed:
            dropped += 1
            continue
        if parsed.story_id in seen_ids:
            continue
        seen_ids.add(parsed.story_id)
        out.append(parsed)
    if dropped:
        log.warning("[@%s] %d of %d story item(s) had no parseable media and were skipped", handle, dropped, len(raw))
    return out


# ── media download (yt-dlp/tikdownloader can't do stories, so fetch directly) ──
_IMG_HEADERS = {"Referer": "https://www.tiktok.com/", "Accept": "image/avif,image/webp,image/*,*/*"}
_VID_HEADERS = {"Referer": "https://www.tiktok.com/", "Accept": "*/*"}


def _download_url(url: str, dest: Path, cookies: Path, headers: dict) -> int:
    """Download a signed CDN url to dest with an impersonated client + cookies.
    Returns bytes written (0 on failure)."""
    from curl_cffi import requests as cffi_requests

    from .resolver import load_netscape_cookies

    session = cffi_requests.Session(impersonate="chrome136")
    session.headers.update(headers)
    if cookies and cookies.exists():
        try:
            load_netscape_cookies(session, cookies)
        except Exception:
            pass
    resp = session.get(url, timeout=60)
    if resp.status_code != 200 or not resp.content:
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    return len(resp.content)


def download_story(item: StoryItem, target_dir: Path, cookies: Path) -> tuple[Path | None, int]:
    """Download a story's media into target_dir.

    Both kinds land directly in the Stories folder, named by story id:
    `<id>.mp4` for a video, `<id>.jpg` for a photo. Returns (primary_path,
    total_bytes); primary_path is None on total failure."""
    if item.kind == "video":
        dest = target_dir / f"{item.story_id}.mp4"
        n = _download_url(item.media_urls[0], dest, cookies, _VID_HEADERS)
        if n > 10_000:
            return dest, n
        dest.unlink(missing_ok=True)
        return None, 0

    # Photo story → one file, not a folder holding one file. Every photo story
    # seen has exactly one image, so the folder was a wrapper around nothing:
    # it made the Stories listing a mix of files and directories for no reason.
    #
    # A story carrying more than one image would lose the extras to a single
    # name, so those keep a suffixed file beside the first rather than a folder
    # around all of them. The first image stays the one the library points at.
    primary: Path | None = None
    total = 0
    for i, url in enumerate(item.media_urls):
        name = f"{item.story_id}.jpg" if i == 0 else f"{item.story_id}_{i + 1:02d}.jpg"
        dest = target_dir / name
        n = _download_url(url, dest, cookies, _IMG_HEADERS)
        if n > 2_000:
            total += n
            if primary is None:
                primary = dest
        else:
            dest.unlink(missing_ok=True)
    return (primary, total) if primary else (None, 0)
