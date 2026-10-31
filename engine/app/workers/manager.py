"""Worker manager - a resizable download thread pool + an APScheduler scan loop."""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from ..core.database import SessionLocal, session_scope
from ..core.events import publish
from ..core.logging_config import get_logger
from ..core import worker_wake
from .. import repositories as repo
from ..models.enums import ActivityType, JobStatus, LogSource, WorkerState
from ..services import events_service, monitor_service, settings_service

log = get_logger("workers")

# Job sources that monitoring itself creates. Pausing monitoring holds exactly
# these; `telegram`, `manual` and `bulk` were asked for by a person and keep
# running, so the bot still answers and a "download all" already in flight still
# finishes.
MONITORING_SOURCES = ("monitor", "catchup", "story")


def _paused_sources(db) -> tuple[str, ...]:
    try:
        from ..services import settings_service

        from ..services.cloud_sync_service import sync
        if settings_service.get(db, "monitoring_paused") or not sync.can_automate(db):
            return MONITORING_SOURCES
    except Exception:
        # Never let a settings read stop the queue draining.
        pass
    return ()



# When idle, workers poll the queue every second but only persist a heartbeat
# this often - one write per worker per second kept SQLite needlessly busy.
IDLE_HEARTBEAT_SECS = 10.0
# Backstop only - a queued job wakes an idle worker immediately via worker_wake.
# Also bounds how long a worker can take to notice a stop request.
IDLE_POLL_SECS = 1.0

# How often the connectivity watcher probes the internet. On a down→up transition
# it fires an offline catch-up so posts/stories missed during the outage are
# backfilled. Short delay after launch before the first startup catch-up so the
# UI and initial scan settle first.
CONNECTIVITY_POLL_SECS = 30.0
STARTUP_CATCH_UP_DELAY_SECS = 15.0


def _probe_online() -> bool:
    """Cheap internet reachability check - a TCP connect to a public DNS resolver
    (port 53). Avoids hitting TikTok just to test connectivity."""
    for host in (("1.1.1.1", 53), ("8.8.8.8", 53)):
        try:
            with socket.create_connection(host, timeout=3):
                return True
        except OSError:
            continue
    return False


class WorkerManager:
    def __init__(self) -> None:
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._scheduler = BackgroundScheduler(timezone="UTC")
        self._lock = threading.Lock()
        # Serialises resize/stop so two pool transitions can't interleave.
        self._pool_lock = threading.Lock()
        self._running = False
        # Connectivity watcher + catch-up state. `_watch_stop` is separate from
        # `_stop` (which the pool resize toggles) so a resize never cancels the
        # watcher. `_catch_up_lock` guarantees only one catch-up runs at a time.
        self._watch_stop = threading.Event()
        self._catch_up_lock = threading.Lock()
        self._online = True

    # ── lifecycle ──
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop.clear()

        with session_scope() as db:
            orphans = repo.Queue.requeue_orphans(db)
        if orphans:
            log.info("Requeued %d job(s) left mid-download by the last shutdown", orphans)

        with session_scope() as db:
            settings_service.ensure_defaults(db)
            n = int(settings_service.get(db, "download_workers"))
            interval = int(settings_service.get(db, "scan_interval"))
            story_interval = int(settings_service.get(db, "story_scan_interval"))
            min_interval = float(settings_service.get(db, "tikdownloader_min_interval"))
            max_interval = float(settings_service.get(db, "tikdownloader_max_interval"))
            max_retries = int(settings_service.get(db, "tikdownloader_max_retries"))
            ig_interval = int(settings_service.get(db, "instagram_scan_interval"))

        from ..tiktok import downloader
        downloader.set_min_interval(min_interval)
        downloader.set_max_interval(max_interval)
        downloader.set_max_retries(max_retries)

        self._spawn(n)

        self._scheduler.add_job(
            self._scheduled_scan, "interval", seconds=interval, id="scan",
            max_instances=1, coalesce=True,
            next_run_time=datetime.now(timezone.utc),
        )
        # Separate, lower-frequency sweep for ephemeral stories (opt-in; the job
        # itself no-ops when disabled). Delayed first run so it doesn't collide
        # with the initial post scan on startup.
        self._scheduler.add_job(
            self._scheduled_story_sweep, "interval", seconds=story_interval, id="story_sweep",
            max_instances=1, coalesce=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=90),
        )
        # Instagram creators, on their own slower schedule. The job no-ops when
        # the feature is off or no Instagram cookies are set, so it costs a
        # function call on a timer for anyone not using it. Offset from the
        # TikTok sweep so the two are not competing for the network on the same
        # tick.
        self._scheduler.add_job(
            self._scheduled_instagram_scan, "interval", seconds=ig_interval,
            id="instagram_scan", max_instances=1, coalesce=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=45),
        )
        self._scheduler.add_job(self._scheduled_x_scan, "interval", seconds=900,
            id="x_scan", max_instances=1, coalesce=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=75))
        with session_scope() as db:
            self._scheduler.reschedule_job("x_scan", trigger="interval",
                                          seconds=int(settings_service.get(db, "x_scan_interval")))
        self._scheduler.start()

        # Connectivity watcher (reconnect catch-up) + one catch-up on launch.
        self._watch_stop.clear()
        self._online = True
        threading.Thread(target=self._connectivity_loop, name="connectivity", daemon=True).start()
        threading.Thread(target=self._startup_catch_up, name="startup-catchup", daemon=True).start()

        log.info("Worker manager started: %d download worker(s), scan every %ds, "
                 "story sweep every %ds, instagram every %ds",
                 n, interval, story_interval, ig_interval)

    def stop(self) -> None:
        self._running = False
        self._stop.set()
        self._watch_stop.set()
        worker_wake.notify()  # break idle workers out of their wait immediately
        try:
            self._scheduler.shutdown(wait=False)
        except Exception:
            pass
        for t in self._threads:
            t.join(timeout=5)
        with session_scope() as db:
            for w in repo.Workers.all(db):
                repo.Workers.set_state(db, w.name, WorkerState.offline)
        log.info("Worker manager stopped")

    # ── pool management ──
    def _spawn(self, n: int) -> None:
        with self._lock:
            for i in range(n):
                name = f"download-{i + 1}"
                t = threading.Thread(target=self._worker_loop, args=(name,), name=name, daemon=True)
                t.start()
                self._threads.append(t)

    def resize(self, n: int) -> None:
        """Restart the pool with `n` workers.

        Runs in the background: workers finish their in-flight download before
        exiting, which can take minutes. The old code joined with a 5s timeout
        and then cleared the stop flag - any worker still downloading survived
        alongside its freshly-spawned namesake, duplicating the pool.
        """
        threading.Thread(target=self._do_resize, args=(n,), name="pool-resize", daemon=True).start()

    def _do_resize(self, n: int) -> None:
        with self._pool_lock:
            if not self._running:
                return
            log.info("Resizing download pool -> %d (draining current workers)", n)
            self._stop.set()
            worker_wake.notify()  # idle workers exit now rather than after a timeout
            for t in self._threads:
                t.join(timeout=900)  # generous: lets an in-flight download finish
            self._threads.clear()
            if not self._running:
                return
            self._stop.clear()
            self._spawn(n)
            # Drop status rows beyond the new pool size so the dashboard doesn't
            # keep phantom offline workers around after shrinking.
            with session_scope() as db:
                for w in repo.Workers.all(db):
                    if w.kind == "download" and w.name.startswith("download-"):
                        try:
                            idx = int(w.name.rsplit("-", 1)[1])
                        except (ValueError, IndexError):
                            continue
                        if idx > n:
                            db.delete(w)
            log.info("Download pool resized to %d", n)

    def reschedule(self, interval: int) -> None:
        try:
            self._scheduler.reschedule_job("scan", trigger="interval", seconds=interval)
            log.info("Scan interval -> %ds", interval)
        except Exception as exc:
            log.warning("reschedule failed: %s", exc)

    def reschedule_stories(self, interval: int) -> None:
        try:
            self._scheduler.reschedule_job("story_sweep", trigger="interval", seconds=interval)
            log.info("Story sweep interval -> %ds", interval)
        except Exception as exc:
            log.warning("story reschedule failed: %s", exc)

    def trigger_scan(self) -> None:
        threading.Thread(target=self._scheduled_scan, kwargs={"manual": True}, daemon=True).start()

    def next_instagram_scan_at(self) -> datetime | None:
        """The scheduler's next fire time for the Instagram sweep (aware UTC).

        Separate from `next_scan_at` because the two sweeps are separate jobs on
        separate intervals; showing the TikTok countdown on the Instagram tab
        made a running monitor look idle and an idle one look busy.
        """
        if not self._running:
            return None
        try:
            job = self._scheduler.get_job("instagram_scan")
            return job.next_run_time if job else None
        except Exception:
            return None

    def next_scan_at(self) -> datetime | None:
        """The scheduler's next fire time for the post scan (aware UTC), or None
        when the manager isn't running. Powers the UI 'Next scan' countdown."""
        if not self._running:
            return None
        try:
            job = self._scheduler.get_job("scan")
            return job.next_run_time if job else None
        except Exception:
            return None

    def trigger_story_sweep(self) -> None:
        threading.Thread(target=self._scheduled_story_sweep, kwargs={"manual": True}, daemon=True).start()

    def catch_up_running(self) -> bool:
        """True while an offline catch-up is in progress.

        Read by the Instagram sweep, which stands aside for it: a catch-up walks
        the whole TikTok roster and is by far the heaviest thing the app does.
        """
        if self._catch_up_lock.acquire(blocking=False):
            self._catch_up_lock.release()
            return False
        return True

    def trigger_instagram_sweep(self) -> None:
        threading.Thread(target=self._scheduled_instagram_scan, kwargs={"manual": True}, daemon=True).start()

    # ── offline catch-up ──
    def run_catch_up(self, reason: str, respect_setting: bool = True) -> bool:
        """Kick off an offline catch-up in the background. Returns False if one is
        already running (so the trigger is coalesced) or catch-up is disabled.

        `respect_setting=False` forces a run regardless of `catch_up_enabled`
        (used by the manual 'catch up now' endpoint)."""
        if not self._running:
            return False
        if respect_setting:
            with session_scope() as db:
                from ..services.cloud_sync_service import sync
                if not settings_service.get(db, "catch_up_enabled") or not sync.can_automate(db):
                    return False
        if not self._catch_up_lock.acquire(blocking=False):
            log.info("catch-up already running, coalescing %s trigger", reason)
            return False

        def _run() -> None:
            try:
                from ..services import catchup_service

                with session_scope() as db:
                    catchup_service.catch_up_all(db, reason=reason)
            except Exception as exc:  # pragma: no cover
                log.error("catch-up (%s) failed: %s", reason, exc)
            finally:
                self._catch_up_lock.release()

        threading.Thread(target=_run, name="catch-up", daemon=True).start()
        return True

    def _startup_catch_up(self) -> None:
        # Let the initial scan + UI settle, then catch up on the offline gap.
        if self._watch_stop.wait(STARTUP_CATCH_UP_DELAY_SECS):
            return  # shutting down
        self.run_catch_up("startup")

    def _connectivity_loop(self) -> None:
        """Poll internet reachability; on a down→up transition, run a catch-up so
        anything posted during the outage is backfilled."""
        while not self._watch_stop.is_set():
            online = _probe_online()
            if online and not self._online:
                log.info("internet reconnected, running catch-up")
                self.run_catch_up("reconnect")
            self._online = online
            self._watch_stop.wait(CONNECTIVITY_POLL_SECS)

    # ── worker loop ──
    def _worker_loop(self, name: str) -> None:
        with session_scope() as db:
            repo.Workers.upsert(db, name, kind="download")
            repo.Workers.set_state(db, name, WorkerState.online)
        publish("worker", {"name": name, "state": "online"})

        from ..services import download_service

        last_beat = 0.0
        while not self._stop.is_set():
            db = SessionLocal()
            try:
                # Cleared *before* claiming: an enqueue landing between the claim
                # and the wait then leaves the flag set, so the wait returns at
                # once instead of losing the nudge and napping the full timeout.
                worker_wake.clear()
                # While monitoring is paused, workers ignore the jobs monitoring
                # produced but keep serving the ones a person asked for.
                job = repo.Queue.claim_next(db, name, exclude_sources=_paused_sources(db))
                if not job:
                    now = time.monotonic()
                    if now - last_beat >= IDLE_HEARTBEAT_SECS:
                        last_beat = now
                        repo.Workers.heartbeat(db, name)
                        db.commit()
                    db.close()
                    # Woken the moment something is queued. The timeout is only a
                    # backstop for work that becomes claimable without an enqueue
                    # (a resumed or retried job) and bounds shutdown latency.
                    worker_wake.wait(IDLE_POLL_SECS)
                    continue

                repo.Workers.set_state(db, name, WorkerState.busy, current_job=job.video_id)
                db.commit()
                publish("worker", {"name": name, "state": "busy", "job": job.video_id})

                ok = False
                try:
                    ok = download_service.process_job(db, job, name)
                except Exception as exc:
                    log.error("[%s] job %s crashed: %s", name, job.id, exc)
                    db.rollback()

                w = repo.Workers.upsert(db, name)
                if ok:
                    w.processed += 1
                elif job.status not in (JobStatus.paused, JobStatus.cancelled):
                    # A cooperative pause or cancellation isn't a failure.
                    w.failed += 1
                repo.Workers.set_state(db, name, WorkerState.online)
                db.commit()
                publish("worker", {"name": name, "state": "online"})
            except Exception as exc:  # pragma: no cover
                log.error("[%s] loop error: %s", name, exc)
                try:
                    db.rollback()
                except Exception:
                    pass
                time.sleep(1.0)
            finally:
                try:
                    db.close()
                except Exception:
                    pass

        with session_scope() as db:
            repo.Workers.set_state(db, name, WorkerState.offline)
        publish("worker", {"name": name, "state": "offline"})

    # ── scheduled scan ──
    def _scheduled_scan(self, manual: bool = False) -> None:
        try:
            with session_scope() as db:
                from ..services.cloud_sync_service import sync
                if not manual and not sync.can_automate(db):
                    return
                new = monitor_service.scan_all(db)
            if new:
                log.info("Scheduled scan queued %d new post(s)", new)
        except Exception as exc:  # pragma: no cover
            log.error("scheduled scan error: %s", exc)

    # ── scheduled Instagram sweep (opt-in; no-ops when disabled) ──
    def _scheduled_instagram_scan(self, manual: bool = False) -> None:
        try:
            from ..services import instagram_monitor_service
            with session_scope() as db:
                from ..services.cloud_sync_service import sync
                if not manual and not sync.can_automate(db):
                    return
                new = instagram_monitor_service.scan_all(db)
            if new:
                log.info("Instagram sweep saved %d new item(s)", new)
        except Exception as exc:  # pragma: no cover
            log.error("scheduled instagram scan error: %s", exc)

    def next_x_scan_at(self):
        if not self._running:
            return None
        job = self._scheduler.get_job("x_scan")
        return job.next_run_time if job else None

    def trigger_x_sweep(self):
        threading.Thread(target=self._scheduled_x_scan, daemon=True).start()

    def _scheduled_x_scan(self):
        try:
            from ..services import x_monitor_service
            with session_scope() as db:
                x_monitor_service.scan_all(db)
        except Exception as exc:
            log.error("X sweep failed: %s", exc)

    def reschedule_x(self, interval: int):
        if self._running:
            self._scheduler.reschedule_job("x_scan", trigger="interval", seconds=interval)

    def reschedule_instagram(self, interval: int) -> None:
        try:
            self._scheduler.reschedule_job("instagram_scan", trigger="interval", seconds=interval)
            log.info("Instagram scan interval -> %ds", interval)
        except Exception as exc:
            log.debug("could not reschedule the instagram sweep: %s", exc)

    # ── scheduled story sweep (opt-in; no-ops when disabled) ──
    def _scheduled_story_sweep(self, manual: bool = False) -> None:
        try:
            from ..services import story_service

            with session_scope() as db:
                from ..services.cloud_sync_service import sync
                if not manual and not sync.can_automate(db):
                    return
                new = story_service.scan_all_stories(db)
            if new:
                log.info("Story sweep archived %d new story item(s)", new)
        except Exception as exc:  # pragma: no cover
            log.error("story sweep error: %s", exc)


manager_instance = WorkerManager()
