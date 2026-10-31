"""Vydlora's gallery-dl entry point with the current Instagram highlight query."""
from __future__ import annotations

import json
import re
from types import MethodType
from urllib.parse import urlsplit

HIGHLIGHTS_QUERY = "PolarisProfileStoryHighlightsTrayContentQuery"
HIGHLIGHTS_DOC_ID = "26970053832668570"
_original_request = None


class _BrowserSession:
    """Use browser-compatible TLS while retaining gallery-dl's cookie jar."""

    def __init__(self, source):
        from curl_cffi import requests

        self.source = source
        self.client = requests.Session(
            impersonate="chrome136", cookies=source.cookies,
            trust_env=source.trust_env)

    def request(self, method, url, **kwargs):
        from curl_cffi.requests import exceptions as curl_errors
        from requests import exceptions, structures

        headers = structures.CaseInsensitiveDict(self.source.headers)
        headers.update(kwargs.pop("headers", None) or {})
        kwargs["headers"] = {key: value for key, value in headers.items() if value is not None}
        try:
            return self.client.request(method, url, **kwargs)
        except curl_errors.Timeout as exc:
            raise exceptions.Timeout(str(exc)) from exc
        except curl_errors.RequestException as exc:
            raise exceptions.RequestException(str(exc)) from exc


def _current_query(data):
    """Supply fields required by Instagram's current browser queries."""
    operation = data.get("fb_api_req_friendly_name")
    if operation == "PolarisProfilePageContentQuery":
        fields = ("PolarisProfileInformTreatmentBannerEnabled",)
    elif operation == "PolarisProfilePostsTabContentQuery_connection":
        fields = ("PolarisSEOIsCrawler", "PolarisThreadsStyleCommentsEnabled")
    else:
        return data
    variables = json.loads(data["variables"])
    for field in fields:
        variables.setdefault("__relay_internal__pv__" + field + "relayprovider", False)
    return {**data, "variables": json.dumps(variables)}


def _checked_request(self, url, **kwargs):
    """Preserve access errors before gallery-dl masks them as a missing user."""
    from gallery_dl import exception

    kwargs.setdefault("retries", 0)
    host = urlsplit(url).hostname or ""
    if "session" not in kwargs and (host == "instagram.com" or host.endswith(".instagram.com")
                                    or host == "static.cdninstagram.com"):
        # Plain requests receives an incomplete page even with valid cookies.
        # Keep gallery-dl's pacing, redirects and HTTP error handling intact.
        session = getattr(self, "_lora1_browser_session", None)
        if session is None:
            session = self._lora1_browser_session = _BrowserSession(self.session)
        kwargs["session"] = session
    if host == "www.instagram.com" and urlsplit(url).path in ("/api/graphql", "/graphql/query"):
        data = kwargs.get("data")
        if isinstance(data, dict) and "variables" in data:
            kwargs["data"] = _current_query(data)
    try:
        return _original_request(self, url, **kwargs)
    except exception.HttpError as exc:
        if exc.status in (404, 405):
            # An obsolete search endpoint should fall back to the profile page.
            # Access failures (401/403/429) must still stop and remain visible.
            raise
        if exc.status == 429:
            message = "Instagram is rate limiting requests (429). Try again later."
        elif exc.status in (401, 403):
            message = "Instagram rejected the login or access to this profile. Check your Instagram cookies."
        else:
            message = f"Instagram couldn't retrieve the requested media: {exc}"
        error = exception.AbortExtraction(message)
        error.code = exc.code or 4
        raise error from exc
    except exception.AbortExtraction as exc:
        # Redirects/challenges are failures, although AbortExtraction defaults
        # to exit code zero. A clean exit would otherwise mean "nothing new".
        exc.code = 4
        raise


def _user_by_screen_name(self, screen_name):
    from gallery_dl import exception

    for strategy in self._strategy_uid:
        try:
            if strategy in {"search", "topsearch"}:
                return self.user_by_search(screen_name)
            if strategy in {"info", "web_profile_info", "api"}:
                return self.user_by_name(screen_name)
            if strategy in {"web", "webpage"}:
                return self.user_by_web(screen_name)
        except exception.ControlException:
            raise
        except Exception:
            continue
    error = exception.AbortExtraction("Instagram couldn't retrieve this profile. Check your Instagram sign-in or try again later.")
    error.code = 4
    raise error


def _operation_id(script: str, operation: str) -> str:
    # Match the operation's own export, never an earlier dependency reference.
    # The latter can pick another query's ID and cache it for an entire day.
    module = re.escape(operation + "_instagramRelayOperation")
    match = re.search(r'__d\(["\']' + module + r'["\'],[^\n]{0,500}?\bexports\s*=\s*["\']?(\d{10,20})', script)
    return match.group(1) if match else ""


def _highlight_docid(self, path: str, operation: str) -> str:
    from gallery_dl import text

    extr = self.extractor
    page = extr.cache(self._webpage, path)
    assets = dict.fromkeys(text.extract_iter(page, 'href="https://static.cdninstagram.com/rsrc.php/', '"'))
    for asset in assets:
        if not asset.endswith(".js"):
            continue
        script = extr.request("https://static.cdninstagram.com/rsrc.php/" + asset, interval=False).text
        identifier = _operation_id(script, operation)
        if identifier:
            return identifier
    return HIGHLIGHTS_DOC_ID


def _highlights_tray(self, handle: str):
    user = self.user(handle)
    variables = {
        "user_id": str(user["id"]), "first": 12, "last": None, "before": None,
        "max_highlights_to_fetch_on_pagination": 12,
    }
    extr = self.extractor
    original_docid, original_request = self._extract_docid, extr.request

    def checked_request(url, **kwargs):
        response = original_request(url, **kwargs)
        request = kwargs.get("data") or {}
        if request.get("fb_api_req_friendly_name") == HIGHLIGHTS_QUERY:
            try:
                payload = response.json()
                highlights = (payload.get("data") or {}).get("highlights")
                if payload.get("errors") or not isinstance(highlights, dict) or not isinstance(highlights.get("edges"), list):
                    raise ValueError("Instagram rejected the highlight list or returned an unexpected response.")
            except (ValueError, TypeError, AttributeError) as exc:
                error = self.exc.AbortExtraction("Couldn't retrieve Instagram highlights: " + str(exc))
                error.code = 4
                raise error from exc
        return response

    # A distinct cache function also bypasses gallery-dl's stale query-ID entry.
    # Restore both methods before fetching reel media, which uses another query.
    self._extract_docid = MethodType(_highlight_docid, self)
    extr.request = checked_request
    try:
        if hasattr(self, "_extract_docid_map"):
            yield from self._pagination_graphql(HIGHLIGHTS_QUERY, variables, "highlights")
        else:
            yield from self._pagination_graphql(
                HIGHLIGHTS_QUERY, "highlights", "", HIGHLIGHTS_DOC_ID, variables)
    finally:
        self._extract_docid = original_docid
        extr.request = original_request


def install():
    global _original_request
    from gallery_dl.extractor.instagram import InstagramAPI, InstagramExtractor

    # Current gallery-dl includes these fixes and uses a newer GraphQL API.
    # Keep its canonical queries; retain the compatibility hooks for old builds.
    if not hasattr(InstagramAPI, "_extract_docid_map"):
        InstagramAPI.highlights_tray = _highlights_tray
        InstagramAPI.user_by_screen_name = _user_by_screen_name
    if _original_request is None:
        _original_request = InstagramExtractor.request
        InstagramExtractor.request = _checked_request


def main():
    install()
    from gallery_dl import main as gallery_main

    raise SystemExit(gallery_main())


if __name__ == "__main__":
    main()
