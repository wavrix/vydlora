"""Application configuration - loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from .compat import apply_environment_aliases

apply_environment_aliases()

# Project root = engine/.. (relative .env paths stay inside this checkout).
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
PROJECT_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── App ──────────────────────────────────────────────
    app_name: str = "Lora 1"
    environment: str = "development"
    log_level: str = "INFO"

    # ── Database ─────────────────────────────────────────
    database_url: str = "sqlite:///./lora1.db"

    # ── Storage ──────────────────────────────────────────
    downloads_dir: str = "./downloads"
    cookies_file: str = "./cookies.txt"

    # ── Monitoring defaults (override live via Settings page) ──
    scan_interval: int = 60
    request_timeout: int = 15
    max_posts_per_scan: int = 10
    # How many creators are checked for new posts at once per scan tick. 1 keeps
    # the gentle sequential sweep; raising it checks a large roster far faster at
    # the cost of heavier request bursts (TikTok rate-limits by IP).
    scan_concurrency: int = 1
    # Stories: opt-in (needs a logged-in cookie session + the headless browser).
    # The story sweep is separate and runs less often - stories last ~24h.
    monitor_stories: bool = False
    story_scan_interval: int = 1800  # seconds between story sweeps (30 min)
    # Gentle per-creator pacing: sleep a random delay in [min, max] seconds
    # between consecutive per-creator checks (posts, stories and catch-up). A
    # jittered gap spaces requests so a large roster doesn't burst TikTok's
    # per-IP limits. Defaults keep the historical 2s spacing; raise both to
    # e.g. 15–30 for a very cautious sweep. Only applies to the sequential
    # (scan_concurrency=1) path - the concurrency limit is the throttle above it.
    scan_spacing_min: float = 2.0
    scan_spacing_max: float = 2.0
    # Catch-up: on app launch and whenever the internet reconnects after a drop,
    # backfill every not-yet-downloaded post per creator (plus any still-active
    # stories) so nothing posted while Lora 1 was offline is missed.
    catch_up_enabled: bool = True
    # ── Rate-limit-safe download defaults ───────────────────────────────
    # tikdownloader.io is fronted by Cloudflare and rate-limits per client IP.
    # These defaults are deliberately conservative so a bulk "download all" of
    # hundreds of posts completes reliably instead of tripping the limit:
    #   • download_workers = 3 - parallel CDN media transfers. API calls are
    #     globally paced regardless of this, so it trades a little speed for
    #     fewer concurrent connections / gentler bursts.
    #   • tikdownloader_min_interval = 3.0s - base spacing between API calls,
    #     i.e. ~20 calls/min steady-state. The adaptive governor widens this
    #     automatically on a 429 and relaxes it again on success.
    #   • tikdownloader_max_retries = 4 - ajaxSearch attempts (shared exponential
    #     backoff) before falling back to yt-dlp, so a brief throttle is ridden
    #     out rather than failing the job.
    download_workers: int = 3
    tikdownloader_min_interval: float = 3.0
    # Seconds gallery-dl waits between Instagram requests. The TikTok side has
    # had a tunable interval since it was first throttled; this is the same
    # lever for the other platform, and at 2.0 it is already gentler than
    # TikTok's 3.0.
    instagram_sleep: float = 2.0
    # Instagram monitoring. Off by default: it needs Instagram cookies, and
    # turning it on for someone who has not set those up would only produce
    # failures. Checked less often than TikTok because gallery-dl has no cheap
    # listing step - every check is a real request.
    instagram_monitor_enabled: bool = False
    instagram_scan_interval: int = 900
    instagram_scan_concurrency: int = 2
    instagram_max_posts_per_scan: int = 5
    tikdownloader_max_retries: int = 4

    # ── API ──────────────────────────────────────────────
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ── Telegram (optional) ──────────────────────────────
    bot_token: str = ""
    allowed_ids: str = ""

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_id_set(self) -> set[int]:
        out: set[int] = set()
        for chunk in self.allowed_ids.split(","):
            chunk = chunk.strip()
            if chunk:
                try:
                    out.add(int(chunk))
                except ValueError:
                    continue
        return out

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.bot_token.strip())

    def _resolve(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    @property
    def downloads_path(self) -> Path:
        return self._resolve(self.downloads_dir).resolve()

    @property
    def cookies_path(self) -> Path:
        return self._resolve(self.cookies_file).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
