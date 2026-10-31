"""Instagram sweep, duplicate prevention and durable archive regression checks."""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import io
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app import models, repositories as repo
from app.core.database import Base
from app.instagram import downloader
from app.services import instagram_monitor_service as monitor, instagram_service, settings_service


class InstagramMonitoringTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "build"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="ig-check-", dir=root)
        self.root = Path(self.temp.name)
        self.engine = create_engine("sqlite:///" + str(self.root / "app.db"), connect_args={"check_same_thread": False})
        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=10000")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)
        self.db = self.sessions()
        settings_service.ensure_defaults(self.db)
        for key, value in {"instagram_monitor_enabled": True, "instagram_scan_interval": 60,
                           "instagram_scan_concurrency": 1, "monitoring_paused": False,
                           "scan_spacing_min": 0.0, "scan_spacing_max": 0.0}.items():
            repo.Settings.set(self.db, key, value)
        self.db.commit()
        self.addCleanup(self.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(monitor, "_posts_blocked_until", 0.0).start()
        patch.object(monitor, "session_scope", self.scope).start()
        patch.object(monitor.cookie_service, "has_instagram_cookies", return_value=True).start()
        patch.object(monitor.events_service, "emit").start()
        patch.object(monitor, "publish").start()
        patch.object(monitor.instagram_service, "probe_creator", return_value=(None, "no-profile")).start()
        self.download = patch.object(monitor.instagram_service, "download_url", return_value={"ok": False, "empty": True}).start()
        from app.workers.manager import manager_instance
        patch.object(manager_instance, "catch_up_running", return_value=False).start()

    def cleanup(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    @contextmanager
    def scope(self):
        with self.sessions() as db:
            yield db
            db.commit()

    def user(self, name="creator", **kwargs):
        user = models.User(state_key="ig:@" + name, platform="instagram", username=name,
                           nickname=name, monitored=True, **kwargs)
        self.db.add(user)
        self.db.commit()
        return user

    def test_posts_highlights_and_stories_are_scanned_and_results_accumulate(self):
        user = self.user(scan_failures=2)
        self.download.side_effect = [{"ok": True, "count": 2}, {"ok": False, "empty": True}, {"ok": True, "count": 1}]
        self.assertEqual(monitor.scan_user(self.db, user), 3)
        self.assertEqual(user.status, "monitoring")
        self.assertEqual(user.scan_failures, 0)
        self.assertIsNotNone(user.last_download)
        self.assertEqual(len(self.download.call_args_list), 3)
        self.assertTrue(all(call.kwargs["use_archive"] for call in self.download.call_args_list))
        self.assertEqual(self.download.call_args_list[0].kwargs["limit"], 5)

    def test_posts_redirect_does_not_disable_working_stories_or_highlights(self):
        user = self.user()
        self.download.side_effect = [{"ok": False, "error": "HTTP redirect to home page"},
                                     {"ok": True, "count": 1}, {"ok": False, "empty": True}]
        self.assertEqual(monitor.scan_user(self.db, user), 1)
        self.assertTrue(user.enabled)
        self.assertEqual(user.scan_failures, 0)
        self.download.reset_mock()
        self.download.side_effect = None
        self.download.return_value = {"ok": False, "empty": True}
        self.assertEqual(monitor.scan_user(self.db, user), 0)
        self.assertEqual(self.download.call_count, 2)

    def test_failed_sign_in_is_reported_and_a_later_success_recovers(self):
        user = self.user()
        self.download.return_value = {"ok": False, "error": "Login required"}
        for _ in range(3):
            self.assertEqual(monitor.scan_user(self.db, user), 0)
        self.assertEqual(user.status, "error")
        self.assertTrue(user.enabled)
        self.assertEqual(user.scan_failures, 3)
        self.download.return_value = {"ok": False, "empty": True}
        monitor.scan_user(self.db, user)
        self.assertEqual(user.status, "monitoring")
        self.assertEqual(user.scan_failures, 0)
        self.assertIsNone(user.last_error)

    def test_reused_instagram_handle_never_downloads_a_different_account(self):
        user = self.user(platform_uid="123")
        monitor.instagram_service.probe_creator.return_value = ({"owner_id": "456"}, "")
        self.assertEqual(monitor.scan_user(self.db, user), 0)
        self.download.assert_not_called()
        self.assertEqual(user.status, "error")
        self.assertIn("different Instagram account", user.last_error)

    def test_sweep_only_checks_due_enabled_monitored_instagram_creators(self):
        due = self.user("due", last_checked=datetime.now(timezone.utc) - timedelta(minutes=10))
        self.user("recent", last_checked=datetime.now(timezone.utc))
        self.user("disabled", enabled=False)
        removed = self.user("removed")
        removed.monitored = False
        tiktok = models.User(state_key="@tiktok", username="tiktok", platform="tiktok")
        self.db.add(tiktok)
        self.db.commit()
        with patch.object(monitor, "_scan_one_isolated", return_value=1) as scan:
            self.assertEqual(monitor.scan_all(self.db), 1)
            scan.assert_called_once_with(due.id)
        self.assertIsNotNone(monitor.last_run_at())

    def test_missing_cookies_pause_and_secondary_device_defer_scans(self):
        self.user()
        with patch.object(monitor.cookie_service, "has_instagram_cookies", return_value=False):
            self.assertIn("No Instagram cookies", monitor.deferral_reason(self.db))
            self.assertEqual(monitor.scan_all(self.db), 0)
        repo.Settings.set(self.db, "monitoring_paused", True)
        self.db.commit()
        self.assertEqual(monitor.deferral_reason(self.db), "Monitoring is paused.")
        repo.Settings.set(self.db, "monitoring_paused", False)
        repo.Settings.set(self.db, "automation_device", "other-device")
        repo.Settings.set(self.db, "_cloud_device", "this-device")
        self.db.commit()
        self.assertIn("another device", monitor.deferral_reason(self.db))
        self.assertEqual(monitor.scan_all(self.db), 0)
        self.download.assert_not_called()

    def test_pause_after_first_creator_stops_the_rest_of_the_sweep(self):
        self.user("first")
        self.user("second")
        def pause(_):
            with self.scope() as db:
                repo.Settings.set(db, "monitoring_paused", True)
            return 1
        with patch.object(monitor, "_scan_one_isolated", side_effect=pause) as scan:
            self.assertEqual(monitor.scan_all(self.db), 1)
            self.assertEqual(scan.call_count, 1)

    def test_waiting_parallel_checks_recheck_pause_and_creator_removal(self):
        user = self.user()
        repo.Settings.set(self.db, "monitoring_paused", True)
        self.db.commit()
        self.assertEqual(monitor._scan_one_isolated(user.id), 0)
        repo.Settings.set(self.db, "monitoring_paused", False)
        user.monitored = False
        self.db.commit()
        self.assertEqual(monitor._scan_one_isolated(user.id), 0)
        self.download.assert_not_called()

    def test_manual_and_scheduled_checks_cannot_overlap_for_one_creator(self):
        user = self.user()
        entered, release = threading.Event(), threading.Event()
        def slow_download(*args, **kwargs):
            entered.set()
            release.wait(3)
            return {"ok": False, "empty": True}
        self.download.side_effect = slow_download
        def check():
            with self.scope() as db:
                monitor.scan_user(db, db.get(models.User, user.id))
        thread = threading.Thread(target=check)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(monitor.scan_user(self.db, user), 0)
            self.assertEqual(self.download.call_count, 1)
        finally:
            release.set()
            thread.join(4)
        self.assertFalse(thread.is_alive())

    def test_restored_download_history_seeds_gallery_duplicate_prevention(self):
        user = self.user()
        self.db.add_all([
            models.Download(user_id=user.id, video_id="111_222", remote_path="/Vydlora/222.jpg"),
            models.Download(user_id=user.id, video_id="333", file_path="missing.jpg"),
            models.Download(user_id=user.id, video_id="444", status="failed", remote_path="/Vydlora/444.jpg"),
        ])
        self.db.commit()
        archive = self.root / "archive.db"
        instagram_service._seed_archive(self.db, archive, "https://www.instagram.com/creator/")
        with closing(sqlite3.connect(archive)) as connection:
            self.assertEqual(connection.execute("SELECT entry FROM archive").fetchall(), [("instagram222",)])
        from gallery_dl import archive as gallery_archive
        reader = gallery_archive.connect(str(archive), "instagram", "{media_id}")
        try:
            self.assertTrue(reader.check({"media_id": "222"}))
        finally:
            reader.close()


class InstagramDeliveryTests(unittest.TestCase):
    def test_failed_storage_does_not_archive_media_or_claim_a_clean_empty_scan(self):
        self._delivery(fail=True)

    def test_successful_storage_is_archived_for_the_next_scan(self):
        self._delivery(fail=False)

    def _delivery(self, *, fail):
        root = Path(__file__).resolve().parents[1] / "build"
        with tempfile.TemporaryDirectory(prefix="ig-delivery-", dir=root) as directory:
            folder = Path(directory)
            media = folder / "123.jpg"
            media.write_bytes(b"media")
            archive = folder / "archive.db"
            item = downloader.IGItem("creator", "123", media, False, {"media_id": "123"})
            process = Mock(returncode=0)
            process.stderr = io.StringIO("")
            process.poll.return_value = 0
            process.communicate.return_value = ("", "")
            handler = Mock(side_effect=OSError("Disk full") if fail else None)
            with patch.object(downloader.subprocess, "Popen", return_value=process), patch.object(downloader, "_collect", return_value=[item]), patch.object(downloader, "_ready", return_value=True):
                items, error = downloader._run_streaming([], folder, timeout=5, on_item=handler, archive=archive)
            if fail:
                self.assertEqual(items, [])
                self.assertIn("Disk full", error)
                self.assertFalse(archive.exists())
            else:
                self.assertEqual(items, [item])
                self.assertFalse(error)
                with closing(sqlite3.connect(archive)) as connection:
                    self.assertEqual(connection.execute("SELECT entry FROM archive").fetchall(), [("instagram123",)])

    def test_gallery_archive_writes_are_disabled_until_application_acknowledges(self):
        root = Path(__file__).resolve().parents[1] / "build"
        with tempfile.TemporaryDirectory(prefix="ig-command-", dir=root) as directory:
            folder = Path(directory)
            with patch.object(downloader, "_run_streaming", return_value=([], "")) as run:
                downloader.run("https://www.instagram.com/creator/", None, folder, on_item=Mock(), archive=folder / "archive.db")
            command = run.call_args.args[0]
            self.assertIn("extractor.instagram.archive-event=[]", command)
            from gallery_dl.option import _parse_option
            self.assertEqual(_parse_option("extractor.instagram.archive-event=[]")[1], [])


if __name__ == "__main__":
    unittest.main()
