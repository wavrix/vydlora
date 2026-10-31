"""Exercise real SQLite stores on two devices, with a simulated MEGA transport."""
import copy
import gzip
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app import models, repositories as repo
from app.core.database import Base
from app.services import cloud_sync_service as cloud, mega_service as mega, settings_service


class CloudSyncTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "build"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="sync-tests-", dir=base)
        self.root = Path(self.temp.name)
        self.remote = {}
        self.commands = []
        self.account = "person@example.test"
        self.online = True
        self.fail_put = False
        self.engines = []
        self.addCleanup(self.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(mega, "ensure_session", side_effect=lambda db: self.online).start()
        patch.object(mega, "whoami", side_effect=lambda **kw: self.account).start()
        patch.object(mega, "_run", side_effect=self.command).start()
        patch.object(cloud.CloudSync, "_apply_runtime").start()
        patch.object(cloud, "publish").start()
        patch.dict("os.environ", {"LORA1_CLOUD_SYNC_DISABLED": "0"}).start()
        self.a = self.device("a")
        self.b = self.device("b")

    def cleanup(self):
        for engine in self.engines:
            engine.dispose()
        assert self.root.parent.resolve() == (Path(__file__).resolve().parents[1] / "build").resolve()
        self.temp.cleanup()

    def device(self, name):
        directory = self.root / name
        directory.mkdir()
        engine = create_engine("sqlite:///" + str(directory / "app.db"))
        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(engine)
        self.engines.append(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        with sessions() as db:
            settings_service.ensure_defaults(db)
        return cloud.CloudSync(sessions, data_dir=directory, cookies_dir=directory / "cookies")

    def command(self, args, **kwargs):
        self.commands.append(args)
        if args[0] == "mkdir":
            return mega.Result(0, "", "")
        if args[0] == "ls":
            return mega.Result(0, "\n".join(key.rsplit("/", 1)[1] for key in self.remote if key.rsplit("/", 1)[0] == args[1]), "")
        if args[0] == "put":
            if self.fail_put:
                return mega.Result(1, "", "offline")
            path = Path(args[2])
            self.remote[args[3].rstrip("/") + "/" + path.name] = path.read_bytes()
            return mega.Result(0, "", "")
        if args[0] == "get":
            if args[1] not in self.remote:
                return mega.Result(1, "", "not found")
            (Path(args[2]) / args[1].rsplit("/", 1)[1]).write_bytes(self.remote[args[1]])
            return mega.Result(0, "", "")
        if args[0] == "rm":
            self.remote.pop(args[2], None)
            return mega.Result(0, "", "")
        raise AssertionError(args)

    def user(self, device, name="creator"):
        with device.sessions() as db:
            row = models.User(state_key="@" + name, username=name, nickname="Original", platform="tiktok")
            db.add(row)
            db.commit()
            return row.id

    def sync_ok(self, device):
        self.assertIn(device.sync_once()["state"], ("synced", "uploading", "waiting"))

    def converge(self):
        for device in (self.a, self.b, self.a, self.b):
            self.sync_ok(device)

    def test_restore_all_user_data_without_machine_paths_or_mega_credentials(self):
        uid = self.user(self.a)
        cookie = self.a.cookies_dir / "test.txt"
        cookie.parent.mkdir()
        cookie.write_text(".tiktok.com\tTRUE\t/\tTRUE\t0\tsessionid\tprivate-session\n")
        self.a._file_path("instagram-cookies").write_text(".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tprivate-ig\n")
        self.a._file_path("shortcuts").write_text('[{"Id":"add","Enabled":false,"Chord":{"Key":78,"Modifiers":1}}]')
        with self.a.sessions() as db:
            for item in (
                models.Post(user_id=uid, video_id="123", url="https://example.test/123", downloaded=True),
                models.Download(user_id=uid, video_id="123", remote_path="/Vydlora/123.mp4", file_path="Z:/old-device/123.mp4"),
                models.QueueJob(user_id=uid, username="creator", video_id="456", url="https://example.test/456", status="processing", worker="worker-1"),
                models.ActivityEvent(user_id=uid, type="download_completed", message="Saved a post"),
                models.TelegramMessage(chat_id=123, text="hello"),
                models.LogEntry(level="INFO", source="system", message="Started"),
                models.Notification(title="Saved", body="A post", read=True),
                models.CookieProfile(name="test.txt", file_path=str(cookie), active=True),
            ):
                db.add(item)
            repo.Settings.set(db, "scan_interval", 123)
            repo.Settings.set(db, "bot_token", "123456:fake-token")
            repo.Settings.set(db, "telegram_allowed_ids", "123,456")
            repo.Settings.set(db, "mega_session", "DO-NOT-UPLOAD")
            repo.Settings.set(db, "downloads_dir", "Z:/old-device")
            db.commit()
        self.user(self.b, "other")  # ensures foreign keys aren't just copied ids
        self.converge()
        with self.b.sessions() as db:
            creator = db.scalar(select(models.User).where(models.User.username == "creator"))
            self.assertNotEqual(uid, creator.id)
            post = db.scalar(select(models.Post))
            self.assertEqual(post.user_id, creator.id)
            download = db.scalar(select(models.Download))
            self.assertIsNone(download.file_path)
            self.assertEqual(download.remote_path, "/Vydlora/123.mp4")
            job = db.scalar(select(models.QueueJob))
            self.assertEqual(job.status, "paused")
            self.assertIsNone(job.worker)
            self.assertEqual(repo.Settings.get(db, "scan_interval"), 123)
            self.assertEqual(repo.Settings.get(db, "bot_token"), "123456:fake-token")
            from app.services.telegram_service import live_allowed_ids
            self.assertEqual(live_allowed_ids(db), {123, 456})
            self.assertIsNone(repo.Settings.get(db, "mega_session"))
            for kind in ("activity", "telegram", "logs", "notifications"):
                self.assertEqual(len(db.scalars(select(cloud.KINDS[kind])).all()), 1)
            restored_cookie = db.scalar(select(models.CookieProfile))
            self.assertEqual(Path(restored_cookie.file_path).read_text(), cookie.read_text())
            self.assertFalse(self.b.can_automate(db))
        self.assertEqual(json.loads(self.b._file_path("shortcuts").read_text())[0]["Enabled"], False)
        self.assertIn("private-ig", self.b._file_path("instagram-cookies").read_text())
        encoded = b" ".join(gzip.decompress(value) for key, value in self.remote.items() if key.endswith(".json.gz"))
        self.assertNotIn(b"DO-NOT-UPLOAD", encoded)
        self.assertNotIn(b"Z:/old-device", encoded)
        self.assertNotIn(str(self.root).encode(), encoded)

    def test_x_cookies_creators_settings_and_history_restore_to_second_device(self):
        self.a.cookies_dir.mkdir(parents=True, exist_ok=True)
        self.a._file_path("x-cookies").write_text("# Netscape HTTP Cookie File\n.x.com\tTRUE\t/\tTRUE\t2147483647\tauth_token\tfixture\n.x.com\tTRUE\t/\tTRUE\t2147483647\tct0\tfixture\n")
        with self.a.sessions() as db:
            user = models.User(state_key="x:@creator", platform="x", username="creator", platform_uid="222", monitored=True)
            db.add(user)
            db.flush()
            db.add(models.Download(user_id=user.id, video_id="123_1", remote_path="/Vydlora/X/creator/x_123_1.jpg", status="completed"))
            repo.Settings.set(db, "x_monitor_enabled", True)
            repo.Settings.set(db, "x_scan_interval", 1200)
            db.commit()
        self.converge()
        self.assertEqual(self.a._file_path("x-cookies").read_bytes(), self.b._file_path("x-cookies").read_bytes())
        self.assertNotEqual(self.b._file_path("x-cookies"), self.b._file_path("instagram-cookies"))
        with self.b.sessions() as db:
            user = db.scalar(select(models.User).where(models.User.platform == "x"))
            self.assertEqual(user.platform_uid, "222")
            self.assertTrue(user.monitored)
            download = db.scalar(select(models.Download).where(models.Download.user_id == user.id))
            self.assertEqual(download.video_id, "123_1")
            self.assertIsNone(download.file_path)
            self.assertEqual(repo.Settings.get(db, "x_scan_interval"), 1200)
            self.assertFalse(self.b.can_automate(db))
        self.a._file_path("x-cookies").unlink()
        self.converge()
        self.assertFalse(self.b._file_path("x-cookies").exists())

    def test_slow_startup_restore_does_not_block_caller_or_start_workers_early(self):
        entered, release, ready = threading.Event(), threading.Event(), threading.Event()

        def slow_restore(**kwargs):
            self.assertFalse(kwargs["include_media"])
            entered.set()
            self.assertTrue(release.wait(3))

        with patch.object(self.a, "sync_once", side_effect=slow_restore), patch.object(cloud.event, "contains", return_value=True):
            try:
                self.a.start_background(ready.set)
                self.assertTrue(entered.wait(1))
                self.assertFalse(ready.is_set())
                release.set()
                self.assertTrue(ready.wait(2))
            finally:
                release.set()
                self.a.stop()

    def test_repeated_sync_does_not_recreate_an_existing_mega_folder(self):
        original = self.command

        def existing_folder(args, **kwargs):
            if args[0] == "mkdir":
                return mega.Result(1, "", "Folder already exists")
            return original(args, **kwargs)

        with patch.object(mega, "_run", side_effect=existing_folder):
            self.user(self.a)
            self.converge()
        self.assertFalse(any(args[0] == "mkdir" for args in self.commands))

    def test_concurrent_mega_folder_creation_is_confirmed_by_listing(self):
        original = self.command
        first_listing = True

        def race(args, **kwargs):
            nonlocal first_listing
            if args[0] == "ls" and first_listing:
                first_listing = False
                return mega.Result(1, "", "Folder not found")
            if args[0] == "mkdir":
                return mega.Result(1, "", "Folder already exists")
            return original(args, **kwargs)

        with patch.object(mega, "_run", side_effect=race):
            self.sync_ok(self.a)

    def test_shutdown_during_restore_cannot_restart_workers_or_sync_loop(self):
        entered, release, ready = threading.Event(), threading.Event(), threading.Event()

        def slow_restore(**kwargs):
            entered.set()
            release.wait(3)

        with patch.object(self.a, "sync_once", side_effect=slow_restore):
            try:
                self.a.start_background(ready.set)
                self.assertTrue(entered.wait(1))
                self.a._stop.set()
                release.set()
                self.a._startup_thread.join(2)
                self.assertFalse(self.a._startup_thread.is_alive())
                self.assertFalse(ready.is_set())
                self.assertIsNone(self.a._thread)
            finally:
                release.set()
                self.a.stop()

    def test_startup_backs_up_metadata_and_defers_large_media_transfers(self):
        uid = self.user(self.a)
        path = self.a.data_dir / "large.mp4"
        path.write_bytes(b"media")
        with self.a.sessions() as db:
            db.add(models.Download(user_id=uid, video_id="large", file_path=str(path)))
            db.commit()
        with patch.object(mega, "upload", return_value="/Vydlora/large.mp4") as upload:
            status = self.a.sync_once(include_media=False)
            self.assertEqual(status["state"], "uploading")
            self.assertEqual(status["pending_uploads"], 1)
            upload.assert_not_called()
            self.sync_ok(self.b)
            with self.b.sessions() as db:
                self.assertEqual(db.scalar(select(models.User)).username, "creator")
            self.sync_ok(self.a)
            upload.assert_called_once()

    def test_telegram_approval_edits_restore_and_restart_the_running_bot(self):
        self.converge()
        with self.a.sessions() as db:
            repo.Settings.set(db, "telegram_allowed_ids", "987")
            db.commit()
        self.sync_ok(self.a)
        cloud.CloudSync._apply_runtime.reset_mock()
        self.sync_ok(self.b)
        cloud.CloudSync._apply_runtime.assert_called_once_with(telegram_changed=True)
        with self.b.sessions() as db:
            from app.services.telegram_service import live_allowed_ids
            self.assertEqual(live_allowed_ids(db), {987})

    def test_concurrent_field_edits_merge_and_do_not_keep_echoing(self):
        self.user(self.a)
        self.converge()
        with self.a.sessions() as db:
            db.scalar(select(models.User)).nickname = "New name"
            db.commit()
        with self.b.sessions() as db:
            db.scalar(select(models.User)).enabled = False
            db.commit()
        self.converge()
        for device in (self.a, self.b):
            with device.sessions() as db:
                user = db.scalar(select(models.User))
                self.assertEqual(user.nickname, "New name")
                self.assertFalse(user.enabled)
        uploads = sum(args[0] == "put" for args in self.commands)
        self.converge()
        self.assertEqual(sum(args[0] == "put" for args in self.commands), uploads)

    def test_deletion_survives_offline_device_and_restart(self):
        self.user(self.a)
        self.converge()
        with self.a.sessions() as db:
            db.delete(db.scalar(select(models.User)))
            db.commit()
        self.sync_ok(self.a)
        self.online = False
        with self.b.sessions() as db:
            db.scalar(select(models.User)).nickname = "Offline edit"
            db.commit()
        self.assertEqual(self.b.sync_once()["state"], "disconnected")
        self.b = cloud.CloudSync(self.b.sessions, data_dir=self.b.data_dir, cookies_dir=self.b.cookies_dir)
        self.online = True
        self.converge()
        for device in (self.a, self.b):
            with device.sessions() as db:
                self.assertEqual(len(db.scalars(select(models.User)).all()), 0)

    def test_failed_upload_retries_and_keeps_previous_cloud_generation(self):
        self.user(self.a)
        self.sync_ok(self.a)
        before = dict(self.remote)
        with self.a.sessions() as db:
            db.scalar(select(models.User)).nickname = "Pending edit"
            db.commit()
        self.fail_put = True
        self.assertEqual(self.a.sync_once()["state"], "error")
        self.assertEqual(self.remote, before)
        with self.a.sessions() as db:
            self.assertEqual(db.scalar(select(models.User)).nickname, "Pending edit")
        self.fail_put = False
        self.converge()
        with self.b.sessions() as db:
            self.assertEqual(db.scalar(select(models.User)).nickname, "Pending edit")

    def test_corrupt_snapshot_does_not_modify_database_or_upload(self):
        self.user(self.a)
        self.sync_ok(self.a)
        name = "f" * 32 + "-" + "9" * 20 + "-12345678.json.gz"
        self.remote[cloud.REMOTE_FOLDER + "/" + name] = b'{"invalid":true}'
        before = dict(self.remote)
        self.assertEqual(self.b.sync_once()["state"], "error")
        self.assertEqual(self.remote, before)
        with self.b.sessions() as db:
            self.assertEqual(len(db.scalars(select(models.User)).all()), 0)

    def test_connected_account_switch_never_exports_previous_account_data(self):
        self.user(self.a)
        self.sync_ok(self.a)
        before = dict(self.remote)
        self.account = "different@example.test"
        self.assertEqual(self.a.sync_once()["state"], "error")
        self.assertEqual(self.remote, before)

    def test_existing_local_media_is_backed_up_even_when_offload_is_off(self):
        uid = self.user(self.a)
        path = self.a.data_dir / "123.mp4"
        path.write_bytes(b"media")
        with self.a.sessions() as db:
            db.add(models.Download(user_id=uid, video_id="123", file_path=str(path)))
            db.commit()
        self.converge()
        self.assertTrue(path.exists())
        with self.b.sessions() as db:
            self.assertTrue(db.scalar(select(models.Download)).remote_path.endswith("/123.mp4"))

    def test_failed_media_upload_does_not_block_metadata_backup(self):
        uid = self.user(self.a)
        path = self.a.data_dir / "123.mp4"
        path.write_bytes(b"media")
        with self.a.sessions() as db:
            db.add(models.Download(user_id=uid, video_id="123", file_path=str(path)))
            db.commit()
        with patch.object(mega, "upload", side_effect=mega.MegaError("offline")):
            self.assertEqual(self.a.sync_once()["state"], "error")
        self.assertTrue(path.exists())
        self.sync_ok(self.b)
        with self.b.sessions() as db:
            self.assertEqual(db.scalar(select(models.User)).username, "creator")

    def test_snapshot_pruning_preserves_other_devices_and_three_generations(self):
        self.user(self.a)
        self.converge()
        for index in range(5):
            with self.a.sessions() as db:
                db.scalar(select(models.User)).nickname = str(index)
                db.commit()
            self.sync_ok(self.a)
        own = [key for key in self.remote if key.rsplit("/", 1)[-1].startswith(self.a.device + "-")]
        self.assertEqual(len(own), 3)
        self.assertTrue(any(key.rsplit("/", 1)[-1].startswith(self.b.device + "-") for key in self.remote))

    def test_explicit_automation_takeover_converges_to_one_device(self):
        self.converge()
        self.b.take_over()
        self.converge()
        with self.a.sessions() as db:
            self.assertFalse(self.a.can_automate(db))
        with self.b.sessions() as db:
            self.assertTrue(self.b.can_automate(db))

    def test_validation_rejects_machine_paths_and_unsafe_preference_values(self):
        self.sync_ok(self.a)
        document = json.loads(gzip.decompress(next(iter(self.remote.values()))))
        copy_doc = copy.deepcopy(document)
        copy_doc["records"]["settings:scan_interval"]["payload"]["fields"]["value"]["v"] = "0"
        with self.assertRaises(ValueError):
            cloud.validate_document(copy_doc, self.account)
        copy_doc = copy.deepcopy(document)
        copy_doc["records"]["settings:scan_interval"]["payload"]["fields"]["file_path"] = {"v": "C:/other", "r": [1, "a" * 32]}
        with self.assertRaises(ValueError):
            cloud.validate_document(copy_doc, self.account)

    def test_import_failure_restores_files_and_rolls_back_database(self):
        self.user(self.a)
        self.a._file_path("shortcuts").write_text('[{"Id":"add","Enabled":true,"Chord":{"Key":78,"Modifiers":1}}]')
        self.converge()
        before = self.b._file_path("shortcuts").read_bytes()
        self.a._file_path("shortcuts").write_text('[{"Id":"add","Enabled":false,"Chord":{"Key":78,"Modifiers":1}}]')
        with self.a.sessions() as db:
            db.scalar(select(models.User)).nickname = "Should roll back"
            db.commit()
        self.sync_ok(self.a)
        apply = self.b.apply
        def fail(db, entries, changed):
            apply(db, entries, changed)
            raise RuntimeError("commit failed")
        remote_before = dict(self.remote)
        with patch.object(self.b, "apply", side_effect=fail):
            self.assertEqual(self.b.sync_once()["state"], "error")
        self.assertEqual(self.b._file_path("shortcuts").read_bytes(), before)
        self.assertEqual(self.remote, remote_before)
        with self.b.sessions() as db:
            self.assertEqual(db.scalar(select(models.User)).nickname, "Original")
        self.sync_ok(self.b)
        self.assertFalse(json.loads(self.b._file_path("shortcuts").read_text())[0]["Enabled"])

    def test_reused_sqlite_row_ids_do_not_resurrect_deleted_history(self):
        uid = self.user(self.a)
        with self.a.sessions() as db:
            db.add(models.Download(user_id=uid, video_id="old", remote_path="/old.mp4"))
            db.commit()
        self.converge()
        with self.a.sessions() as db:
            old_id = db.scalar(select(models.Download)).id
            db.delete(db.scalar(select(models.Download)))
            db.commit()
            new = models.Download(user_id=uid, video_id="new", remote_path="/new.mp4")
            db.add(new)
            db.commit()
            self.assertEqual(new.id, old_id)
        self.converge()
        with self.b.sessions() as db:
            self.assertEqual([row.video_id for row in db.scalars(select(models.Download))], ["new"])

    def test_creator_identity_upgrade_preserves_children_across_devices(self):
        uid = self.user(self.a)
        with self.a.sessions() as db:
            db.add(models.Post(user_id=uid, video_id="123", url="https://example.test/123", downloaded=True))
            db.add(models.Download(user_id=uid, video_id="123", remote_path="/123.mp4"))
            db.commit()
        self.converge()
        with self.a.sessions() as db:
            user = db.scalar(select(models.User))
            user.state_key, user.tiktok_uid = "uid:123456", "123456"
            db.commit()
        self.converge()
        with self.b.sessions() as db:
            user = db.scalar(select(models.User))
            self.assertEqual(user.state_key, "uid:123456")
            self.assertEqual(db.scalar(select(models.Post)).user_id, user.id)
            self.assertEqual(db.scalar(select(models.Download)).user_id, user.id)

    def test_cookie_deletion_is_not_undone_by_startup_discovery(self):
        default = self.a.data_dir / "cookies.txt"
        default.write_text(".tiktok.com\tTRUE\t/\tTRUE\t0\tsessionid\tprivate-session\n")
        with self.a.sessions() as db:
            db.add(models.CookieProfile(name="cookies.txt", file_path=str(default), active=True))
            db.commit()
        self.converge()
        with self.a.sessions() as db:
            db.delete(db.scalar(select(models.CookieProfile)))
            db.commit()
        self.converge()
        with self.a.sessions() as db, patch.object(cloud.cookie_service, "_discover_cookie_file", return_value=default):
            cloud.cookie_service.sync_default(db)
            self.assertEqual(len(db.scalars(select(models.CookieProfile)).all()), 0)

    def test_committed_edits_request_sync_and_internal_journal_writes_do_not(self):
        self.sync_ok(self.a)
        with patch.object(cloud.sync, "request") as request:
            with self.a.sessions() as db:
                db.add(models.Notification(title="New", body="local"))
                cloud._mark_changes(db, None)
                db.commit()
                cloud._request_sync(db)
                request.assert_called_once()
            request.reset_mock()
            with self.a._db() as db:
                repo.Settings.set(db, "scan_interval", 30)
                cloud._mark_changes(db, None)
                db.commit()
                cloud._request_sync(db)
                request.assert_not_called()

    def test_successful_restore_updates_ui_even_if_following_upload_fails(self):
        self.user(self.a)
        self.sync_ok(self.a)
        self.fail_put = True
        cloud.publish.reset_mock()
        self.assertEqual(self.b.sync_once()["state"], "error")
        cloud.publish.assert_any_call("cloud_data", {})
        with self.b.sessions() as db:
            self.assertEqual(db.scalar(select(models.User)).username, "creator")

    def test_secondary_device_can_run_manual_checks_but_not_automatic_scans(self):
        from app.workers import manager
        worker = manager.WorkerManager()
        with patch.object(manager, "session_scope", self.a.sessions), \
                patch.object(cloud.sync, "can_automate", return_value=False), \
                patch.object(manager.monitor_service, "scan_all", return_value=0) as scan:
            worker._scheduled_scan()
            scan.assert_not_called()
            worker._scheduled_scan(manual=True)
            scan.assert_called_once()
