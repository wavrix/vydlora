"""Adapted from Void/downloader.py: byte-preserving X photo/video/GIF downloads.

Vydlora supplies its managed cookies and temporary directories explicitly.
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import imageio_ffmpeg
import yt_dlp
from yt_dlp.extractor.twitter import TwitterIE

from ..core import media_convert
FFMPEG = media_convert.ffmpeg_path() or imageio_ffmpeg.get_ffmpeg_exe()
QUALITY_NOTE = "Highest quality X exposes. Original upload bytes are not verified. Cookies cannot restore quality X does not serve."


def normalize_url(value: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValueError("Paste an X post link.")
    value = value.strip()
    if not value.startswith(("https://", "http://")):
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"} or parsed.username or parsed.password or parsed.port:
        raise ValueError("Use an x.com or twitter.com post link.")
    match = re.fullmatch(r"/(?:[\w]+|i/web)/status/(\d+)(?:/(?:photo|video)/\d+)?/?", parsed.path)
    if not match:
        raise ValueError("Paste a post link containing /status/ and a post ID.")
    post_id = match[1]
    return f"https://x.com/i/web/status/{post_id}", post_id


def original_image_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.hostname != "pbs.twimg.com" or not parsed.path.startswith("/media/"):
        raise ValueError("Unexpected image host.")
    path = re.sub(r":(?:orig|large|medium|small)$", "", parsed.path)
    query = parse_qs(parsed.query)
    suffix = Path(path).suffix.lstrip(".")
    fmt = query.get("format", [suffix or "jpg"])[0]
    if fmt not in {"jpg", "jpeg", "png", "webp"}:
        raise ValueError("Unexpected image format.")
    if suffix:
        path = path[: -(len(suffix) + 1)]
    return urlunsplit(("https", "pbs.twimg.com", path, urlencode({"format": fmt, "name": "orig"}), ""))


def read_cookies(path: Path) -> http.cookiejar.MozillaCookieJar:
    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception:
        raise ValueError("Cookies must be a valid Netscape cookies.txt export.") from None
    for cookie in list(jar):
        if cookie.domain.lstrip(".") not in {"x.com", "twitter.com", "www.x.com", "www.twitter.com", "api.x.com", "api.twitter.com"}:
            jar.clear(cookie.domain, cookie.path, cookie.name)
    return jar


def cookie_status(path: Path) -> dict:
    if not path.exists():
        return {"available": False, "authenticated": False, "message": "Public access · no cookies"}
    try:
        jar = read_cookies(path)
        names = {c.name for c in jar if not c.is_expired()}
        authenticated = {"auth_token", "ct0"} <= names
        return {"available": True, "authenticated": authenticated, "message": "X session loaded" if authenticated else "Cookies expired or missing auth_token / ct0"}
    except ValueError as exc:
        return {"available": True, "authenticated": False, "message": str(exc)}


class QuietLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


@contextmanager
def youtube_dl(use_cookies=True, cookie_path: Path | None = None, **extra):
    # yt-dlp saves its cookie jar on close; use a filtered temporary copy to
    # preserve the supplied file and prevent unrelated cookies leaving the app.
    with tempfile.TemporaryDirectory(prefix="lora1-x-") as folder:
        options = {"quiet": True, "no_warnings": True, "logger": QuietLogger(), "socket_timeout": 25,
                   "retries": 3, "fragment_retries": 3, "ffmpeg_location": FFMPEG,
                   "format_sort": ["res", "fps", "br"], "format_sort_force": True}
        source = cookie_path
        if use_cookies and source and source.exists():
            jar = read_cookies(source)
            jar.filename = str(Path(folder) / "cookies.txt")
            jar.save(ignore_discard=True, ignore_expires=True)
            options["cookiefile"] = jar.filename
        options.update(extra)
        with yt_dlp.YoutubeDL(options) as ydl:
            yield ydl


class MediaTwitterIE(TwitterIE):
    """Keep upstream's current login/API implementation, including photo data.

    _extract_status and _extract_variant_formats are upstream private APIs.
    This small adapter is covered by fixtures and should be checked on upgrades.
    """
    def _real_extract(self, url):
        return self._extract_status(self._match_id(url))


def format_summary(fmt: dict) -> dict:
    return {key: fmt.get(key) for key in ("format_id", "ext", "width", "height", "fps", "tbr", "vcodec", "acodec", "protocol", "filesize", "filesize_approx")}


def public_media(media: dict) -> dict:
    return {key: value for key, value in media.items() if not key.startswith("_")}


def build_media(status: dict, post_id: str, extractor, ydl) -> list[dict]:
    entities = status.get("extended_entities", {}).get("media", [])
    result = []
    for index, entry in enumerate(entities, 1):
        kind = entry.get("type")
        preview = entry.get("media_url_https") or entry.get("media_url")
        original = entry.get("original_info") or {}
        item = {"id": str(index), "kind": "image" if kind == "photo" else "video", "label": f"{'Image' if kind == 'photo' else 'Video'} {index}",
                "preview": preview if preview and urlsplit(preview).hostname == "pbs.twimg.com" else None,
                "width": original.get("width"), "height": original.get("height"), "fps": None,
                "recorded_width": original.get("width"), "recorded_height": original.get("height"),
                "original_verified": False, "quality_note": QUALITY_NOTE}
        if kind == "photo" and preview:
            url = original_image_url(preview)
            item.update({"quality": "X original image size", "formats": [], "_url": url,
                         "_ext": parse_qs(urlsplit(url).query)["format"][0]})
        elif kind in {"video", "animated_gif"}:
            formats = []
            for variant in entry.get("video_info", {}).get("variants", []):
                address = variant.get("url", "")
                if urlsplit(address).hostname != "video.twimg.com":
                    continue
                fmts, _ = extractor._extract_variant_formats(variant, post_id)
                formats.extend(fmts)
            if not formats:
                continue
            info = {"id": f"{post_id}_{index}", "title": f"X {post_id} video {index}", "formats": formats,
                    "extractor": "twitter", "webpage_url": f"https://x.com/i/web/status/{post_id}"}
            processed = ydl.process_ie_result(info, download=False)
            selected = processed.get("requested_downloads", [processed])[0]
            components = selected.get("requested_formats") or [selected]
            video = next((f for f in components if f.get("vcodec") != "none"), selected)
            item.update({"width": video.get("width"), "height": video.get("height"), "fps": video.get("fps"),
                         "quality": "Best available video", "formats": [format_summary(f) for f in processed["formats"]],
                         "selected_format": selected.get("format_id"), "duration": entry.get("video_info", {}).get("duration_millis", 0) / 1000,
                         "_info": processed, "_ext": "mp4"})
            item["matches_recorded_dimensions"] = dimensions_match(item)
        else:
            continue
        result.append(item)
    return result


def dimensions_match(item: dict):
    values = [item.get(key) for key in ("width", "height", "recorded_width", "recorded_height")]
    if not all(isinstance(value, (int, float)) and value > 0 for value in values):
        return None
    return values[:2] == values[2:]


def inspect_post(value: str, use_cookies=True, cookie_path: Path | None = None) -> dict:
    url, post_id = normalize_url(value)
    with youtube_dl(use_cookies, cookie_path, format="bestvideo*+bestaudio/best", merge_output_format="mp4") as ydl:
        extractor = MediaTwitterIE(ydl)
        status = extractor.extract(url)
        media = build_media(status, post_id, extractor, ydl)
    return _post_result(status, url, post_id, media, use_cookies, cookie_path)

def inspect_status(status: dict, post_id: str, cookie_path: Path | None = None) -> dict:
    """Use already authenticated discovery data with Void's format selection."""
    url, post_id = normalize_url(f"https://x.com/i/web/status/{post_id}")
    if str(status.get("id_str") or "") != post_id:
        raise ValueError("X returned a different post.")
    with youtube_dl(True, cookie_path, format="bestvideo*+bestaudio/best", merge_output_format="mp4") as ydl:
        media = build_media(status, post_id, MediaTwitterIE(ydl), ydl)
    return _post_result(status, url, post_id, media, True, cookie_path)

def _post_result(status, url, post_id, media, use_cookies, cookie_path):
    if not media:
        raise ValueError("No downloadable photos or videos were found in this post.")
    user = status.get("user") or {}
    return {"url": url, "post_id": post_id, "author": user.get("screen_name") or "X post",
            "text": status.get("full_text") or status.get("text") or "", "media": media,
            "quality_note": QUALITY_NOTE, "_use_cookies": use_cookies, "_cookie_path": cookie_path,
            "profile": {"username": user.get("screen_name"), "fullname": user.get("name"), "owner_id": user.get("id_str"), "avatar": user.get("profile_image_url_https")}}


def probe_video(path: Path) -> dict:
    result = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                            timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    video_lines = [line for line in result.stderr.splitlines() if "Video:" in line]
    if not video_lines:
        raise ValueError("The downloaded file did not contain a readable video stream.")
    line = video_lines[0]
    dimensions = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line)
    fps = re.search(r"([\d.]+) fps", line)
    return {"width": int(dimensions[1]) if dimensions else None, "height": int(dimensions[2]) if dimensions else None,
            "fps": float(fps[1]) if fps else None, "verified_stream": True}


def download_media(post: dict, item: dict, folder: Path, progress=lambda value: None) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"x_{post['post_id']}_{item['id']}"
    target = folder / f"{stem}.{item['_ext']}"
    details = {}
    def hook(update):
        total = update.get("total_bytes") or update.get("total_bytes_estimate")
        if update.get("status") == "downloading":
            progress(min(99, update.get("downloaded_bytes", 0) * 100 / total) if total else None)
    with youtube_dl(post.get("_use_cookies", True), post.get("_cookie_path"),
                    outtmpl=str(folder / f"{stem}.%(ext)s"), progress_hooks=[hook],
                    overwrites=True, merge_output_format="mp4") as ydl:
        if item["kind"] == "image":
            part = target.with_suffix(target.suffix + ".part")
            try:
                with ydl.urlopen(item["_url"]) as response, part.open("wb") as output:
                    if not response.headers.get("Content-Type", "").lower().startswith("image/"):
                        raise ValueError("X returned an unexpected response instead of an image.")
                    total = int(response.headers.get("Content-Length", 0))
                    done = 0
                    while chunk := response.read(256 * 1024):
                        output.write(chunk)
                        done += len(chunk)
                        progress(min(99, done * 100 / total) if total else None)
                part.replace(target)
            finally:
                part.unlink(missing_ok=True)
        else:
            info = ydl.process_ie_result(dict(item["_info"]), download=True)
            actual = Path(info.get("filepath") or ydl.prepare_filename(info))
            if actual.exists():
                target = actual
            elif not target.exists():
                raise ValueError("The download completed without an output file.")
            details = probe_video(target)
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    report = {"filename": target.name, "bytes": target.stat().st_size, "sha256": digest.hexdigest(),
              "kind": item["kind"], "source_post": post["url"], "original_verified": False,
              "quality_note": QUALITY_NOTE, **details}
    if item["kind"] == "video":
        report.update({"recorded_width": item.get("recorded_width"), "recorded_height": item.get("recorded_height")})
        report["matches_recorded_dimensions"] = dimensions_match(report)
    target.with_suffix(target.suffix + ".json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    progress(100)
    return report
