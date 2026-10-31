"""Lora 1 FastAPI application.

Lifespan boots: DB init → default seeding → worker pool + scheduler → Telegram bot.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .core.config import settings
from .core.database import init_db, session_scope
from .core.events import set_loop
from .core.logging_config import get_logger, setup_logging
from .services import cookie_service, settings_service

setup_logging()
log = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Capture the running loop so worker threads can broadcast over WS.
    set_loop(asyncio.get_running_loop())

    # Mirror the backend's own logs to the app's Terminal page. Started first so
    # the startup sequence below is itself visible there.
    from .core import logstream

    logstream.start()

    init_db()
    from .services import storage_service

    storage_service.clear_temporary_media()
    with session_scope() as db:
        settings_service.ensure_defaults(db)
        cookie_service.sync_default(db)
        # Apply persisted rate-limit settings to the downloader governor so a
        # saved (or default) throttle is in effect from the first download.
        from .tiktok import downloader

        downloader.set_min_interval(float(settings_service.get(db, "tikdownloader_min_interval")))
        downloader.set_max_interval(float(settings_service.get(db, "tikdownloader_max_interval")))
        downloader.set_max_retries(int(settings_service.get(db, "tikdownloader_max_retries")))
    log.info("Database ready (%s)", settings.database_url)

    # Relocate working files before any migration, reconciliation or worker can
    # create more platform folders in the former user-selected Downloads root.
    from .services import managed_storage_service
    with session_scope() as db:
        managed_storage_service.migrate(db)

    # One-time layout migration: every platform now has its own download folder
    # (<downloads>/TikTok/<creator>/). Existing creator folders are moved and their
    # stored paths rewritten. MUST run before reconciliation, which scans those
    # folders - otherwise previously-downloaded posts look missing and get fetched
    # again. Guarded to run a single time.
    try:
        from .services import layout_service

        with session_scope() as db:
            layout_service.migrate_once(db)
            # Fold together the split folders left behind when a scan renamed a
            # creator's folder out from under its already-downloaded files. Must
            # also precede reconciliation, for the same reason as the move above.
            layout_service.reconcile_once(db)
    except Exception as exc:  # never block startup on the migration
        log.error("platform-folder migration failed: %s", exc)

    # One-time reconciliation: teach the DB about media already on disk (files
    # saved outside the app, or before a DB reset) so the catch-up / bulk logic
    # doesn't re-download them. Guarded to run a single time; runs BEFORE the
    # workers/catch-up so the first sweep sees an accurate "already downloaded" set.
    try:
        from .services import reconcile_service

        with session_scope() as db:
            reconcile_service.reconcile_once(db)
    except Exception as exc:  # never block startup on reconciliation
        log.error("disk reconciliation failed: %s", exc)

    # Restore the account's state before any automatic monitoring begins.
    from .services.cloud_sync_service import sync as cloud_sync
    from .workers.manager import manager_instance
    from .services import telegram_service

    closing = threading.Event()
    lifecycle_lock = threading.Lock()

    def start_workers():
        # Shutdown cannot race the completion of a slow account restore.
        with lifecycle_lock:
            if closing.is_set():
                return
            manager_instance.start()
            telegram_service.start()

    cloud_sync.start_background(start_workers)

    log.info("Lora 1 %s started", __version__)
    try:
        yield
    finally:
        log.info("Shutting down…")
        with lifecycle_lock:
            closing.set()
        cloud_sync.stop()
        logstream.stop()
        manager_instance.stop()
        telegram_service.stop()
        storage_service.clear_temporary_media()
        from .tiktok import browser

        browser.shutdown()


app = FastAPI(
    title="Lora 1 API",
    version=__version__,
    description="Enterprise TikTok Monitoring & Download Management Platform",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def account_switch_guard(request, call_next):
    from .services.account_profile_service import switching
    if switching() and request.url.path not in ("/api/ping", "/api/mega/connect"):
        return JSONResponse(status_code=503, content={"detail": "Restarting the Lora 1 engine to open the connected account."})
    return await call_next(request)


# ── Routers ──
from .api.routers import dashboard, downloads, instagram, x, queue, system, users, ws  # noqa: E402

app.include_router(dashboard.router)
app.include_router(users.router)
app.include_router(downloads.router)
app.include_router(queue.router)
app.include_router(system.router)
app.include_router(instagram.router)
app.include_router(x.router)
app.include_router(ws.router)

@app.get("/api/ping")
def ping():
    return {"ok": True, "app": settings.app_name, "version": __version__}


@app.exception_handler(Exception)
async def _unhandled(request, exc):  # pragma: no cover
    log.error("Unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
