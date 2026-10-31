"""Strict X/Twitter URL routing shared by the app and Telegram."""
import re
from urllib.parse import urlsplit

HOSTS = {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
RESERVED = {"home", "explore", "search", "notifications", "messages", "settings", "i", "intent", "share", "login", "logout", "signup", "tos", "privacy"}

def _path(value: str) -> str:
    try:
        parsed = urlsplit(value.strip() if "://" in value else "https://" + value.strip())
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in HOSTS or parsed.username or parsed.password or parsed.port:
            return ""
        return parsed.path
    except (ValueError, AttributeError):
        return ""

def normalize_handle(value: str) -> str:
    handle = (value or "").strip().lstrip("@").rstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle) or handle.lower() in RESERVED:
        raise ValueError("Enter an X username or profile link.")
    return handle

def extract_username(value: str) -> str | None:
    match = re.fullmatch(r"/([A-Za-z0-9_]{1,15})(?:/media)?/?", _path(value))
    if not match:
        return None
    try:
        return normalize_handle(match[1])
    except ValueError:
        return None

def post_id(value: str) -> str | None:
    match = re.fullmatch(r"/(?:[A-Za-z0-9_]{1,15}|i/web)/status/(\d+)(?:/(?:photo|video)/\d+)?/?", _path(value))
    return match[1] if match else None

def is_profile_url(value: str) -> bool:
    return extract_username(value) is not None

def is_x_url(value: str) -> bool:
    return bool(post_id(value) or is_profile_url(value))

def profile_url(handle: str) -> str:
    return "https://x.com/" + normalize_handle(handle)

def status_url(identifier: str) -> str:
    if not str(identifier).isdigit():
        raise ValueError("Invalid X post ID.")
    return "https://x.com/i/web/status/" + str(identifier)
