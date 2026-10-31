"""Switch real SQLite profiles using mocked MEGA authentication, never live accounts."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import models, repositories as repo
from app.core.database import Base
from app.core.profiles import SELECTOR, account_key, activate, resolve_profile
from app.services import account_profile_service as profiles, cloud_sync_service as cloud, mega_service as mega, storage_service
from app.schemas import MegaConnect
from app.api.routers.system import connect_mega
from app.api.routers.system import _mega_connection_locks


class AccountProfileTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "build"
        base.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="account-profiles-", dir=base)
        self.root = Path(self.temporary.name).resolve()
        self.engine = self.store(self.root, "old@example.test", "original")
        profiles._pending.clear()
        profiles._account = ""
        self.environment = patch.dict(os.environ, {"LORA1_PROFILES_ROOT": str(self.root), "LORA1_MANAGED": "1"})
        self.environment.start()
        self.auth = patch.object(mega, "authenticate", return_value=("new@example.test", "new-session")).start()
        patch.object(mega, "whoami", return_value="old@example.test").start()
        patch.object(mega, "_session_token", return_value="old-session").start()
        self.restore = patch.object(mega, "restore_session").start()

    def tearDown(self):
        profiles._pending.clear()
        profiles._account = ""
        patch.stopall()
        self.engine.dispose()
        self.temporary.cleanup()

    def store(self, directory, account, username):
        directory.mkdir(parents=True, exist_ok=True)
        engine = create_engine("sqlite:///" + (directory / "lora1.db").as_posix())
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            repo.Settings.set(db, mega.BOUND_ACCOUNT_KEY, account)
            repo.Settings.set(db, mega.SESSION_KEY, "saved-session")
            repo.Settings.set(db, "bot_token", "private-bot-" + username)
            db.add(models.User(username=username, state_key="@" + username))
            db.commit()
        return engine

    def connect(self, email="new@example.test", switch=True):
        with Session(self.engine) as db:
            return connect_mega(MegaConnect(email=email, password="not-a-real-password", switch_account=switch), db)

    def test_new_account_is_empty_and_original_files_credentials_and_records_survive(self):
        cookies = self.root / "cookies.txt"
        cookies.write_text("private-original-cookies", encoding="utf-8")
        media = self.root / "cache" / "downloads" / "pending.mp4"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"not-yet-uploaded")
        before = hashlib.sha256((self.root / "lora1.db").read_bytes()).digest()
        result = self.connect(" NEW@example.test ")
        self.assertTrue(result["restart_required"])
        target = resolve_profile(self.root)
        self.assertEqual(target, self.root / "profiles" / account_key("new@example.test"))
        self.assertEqual(profiles.bound_account(target), "new@example.test")
        self.assertEqual(hashlib.sha256((self.root / "lora1.db").read_bytes()).digest(), before)
        self.assertEqual(cookies.read_text(encoding="utf-8"), "private-original-cookies")
        self.assertEqual(media.read_bytes(), b"not-yet-uploaded")
        engine = create_engine("sqlite:///" + (target / "lora1.db").as_posix())
        try:
            with Session(engine) as db:
                self.assertIsNone(db.scalar(select(models.User)))
                self.assertEqual(repo.Settings.get(db, mega.SESSION_KEY), "new-session")
                self.assertIsNone(repo.Settings.get(db, "bot_token"))
            self.assertFalse((target / "cookies.txt").exists())
        finally:
            engine.dispose()
        self.assertNotIn("not-a-real-password", (self.root / SELECTOR).read_text(encoding="utf-8"))
        self.restore.assert_not_called()

    def test_switch_back_reopens_original_store_in_place(self):
        self.connect()
        profiles._pending.clear()  # Simulate a completed desktop restart.
        self.auth.return_value = ("old@example.test", "fresh-old-session")
        with Session(self.engine) as db:
            # The current engine would be the new profile after restart.
            result = profiles.prepare_switch(db, "old@example.test", "password")
        self.assertTrue(result["restart_required"])
        self.assertEqual(resolve_profile(self.root), self.root)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(models.User)).username, "original")
            self.assertEqual(repo.Settings.get(db, "bot_token"), "private-bot-original")
            self.assertEqual(repo.Settings.get(db, mega.SESSION_KEY), "fresh-old-session")

    def test_existing_target_preserves_its_own_data_without_merging_original(self):
        target = self.root / "profiles" / account_key("new@example.test")
        engine = self.store(target, "new@example.test", "returning")
        engine.dispose()
        self.connect()
        engine = create_engine("sqlite:///" + (target / "lora1.db").as_posix())
        try:
            with Session(engine) as db:
                self.assertEqual([u.username for u in db.scalars(select(models.User))], ["returning"])
                self.assertEqual(repo.Settings.get(db, "bot_token"), "private-bot-returning")
        finally:
            engine.dispose()

    def test_bad_password_and_disk_failure_keep_previous_profile_active(self):
        self.auth.side_effect = mega.MegaError("Bad credentials")
        with self.assertRaises(HTTPException):
            self.connect()
        self.assertFalse(profiles.switching())
        self.assertEqual(resolve_profile(self.root), self.root)
        self.restore.assert_called_with("old-session")
        self.auth.side_effect = None
        with patch.object(profiles, "activate", side_effect=OSError("disk full")):
            with self.assertRaises(HTTPException):
                self.connect()
        self.assertFalse(profiles.switching())
        self.assertEqual(resolve_profile(self.root), self.root)
        self.restore.assert_called_with("old-session")
        self.assertEqual(profiles.bound_account(self.root), "old@example.test")

    def test_switch_requires_desktop_and_explicit_supported_client(self):
        with self.assertRaises(HTTPException) as caught:
            self.connect(switch=False)
        self.assertEqual(caught.exception.status_code, 409)
        with patch.dict(os.environ, {"LORA1_MANAGED": "0"}):
            with self.assertRaises(HTTPException):
                self.connect()
        self.auth.assert_not_called()
        self.assertEqual(resolve_profile(self.root), self.root)

    def test_busy_cloud_transfer_cannot_leave_a_delayed_login_queued(self):
        with patch.object(cloud.sync, "_lock") as lock:
            lock.acquire.return_value = False
            with self.assertRaises(HTTPException) as caught:
                with _mega_connection_locks():
                    raise AssertionError("A busy connection was accepted.")
            self.assertEqual(caught.exception.status_code, 409)
            lock.acquire.assert_called_once_with(timeout=10)
        self.auth.assert_not_called()

    def test_pending_switch_is_idempotent_and_blocks_automation_and_transfers(self):
        self.connect()
        self.assertTrue(self.connect()["restart_required"])
        self.auth.assert_called_once()
        with Session(self.engine) as db:
            self.assertFalse(mega.ensure_session(db))
            self.assertFalse(cloud.sync.can_automate(db))
        with self.assertRaises(HTTPException):
            self.connect("old@example.test")
        with patch.object(cloud.sync, "_db", side_effect=AssertionError("must not sync")):
            cloud.sync.sync_once()

    def test_selector_rejects_traversal_and_broken_data(self):
        for profile in ("../other", "profiles/not-a-hash", "D:/other"):
            (self.root / SELECTOR).write_text(json.dumps({"version": 1, "profile": profile, "account": "new@example.test"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                resolve_profile(self.root)
        with self.assertRaises(ValueError):
            activate(self.root, self.root.parent, "new@example.test")

    def test_bootstrap_resolves_database_cookies_cache_and_sync_into_target(self):
        self.connect()
        source = """
import bootstrap as boot
data = boot._data_dir()
boot._configure_env(data)
from app.core.config import settings
from app.services import settings_service, cookie_service, cloud_sync_service
import json
print(json.dumps(dict(data=str(data), db=settings.database_url, cookies=str(settings.cookies_path), managed=str(settings_service.managed_downloads_path()), sync=str(cloud_sync_service.sync.data_dir), jars=str(cookie_service.COOKIES_DIR))))
"""
        environment = os.environ.copy()
        environment["LORA1_DATA_DIR"] = str(self.root)
        # Even inherited legacy overrides cannot attach the previous database
        # to the new profile's cookies/session after an account switch.
        environment.update(DATABASE_URL="sqlite:///" + (self.root / "lora1.db").as_posix(),
                           DOWNLOADS_DIR=str(self.root / "cache"), COOKIES_FILE=str(self.root / "cookies.txt"))
        result = subprocess.run([sys.executable, "-c", source], cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout.strip().splitlines()[-1])
        target = resolve_profile(self.root)
        self.assertEqual(Path(values["data"]), target)
        self.assertEqual(values["db"], "sqlite:///" + (target / "lora1.db").as_posix())
        for key in ("cookies", "managed", "sync", "jars"):
            self.assertTrue(Path(values[key]).is_relative_to(target))

    def test_identical_remote_paths_cannot_reuse_another_accounts_cached_file(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root), "LORA1_DATA_DIR": str(self.root / "a")}):
            first = storage_service.cache_dir()
            (first / "saved.mp4").write_bytes(b"account-a-private-media")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root), "LORA1_DATA_DIR": str(self.root / "b")}):
            second = storage_service.cache_dir()
            self.assertNotEqual(first, second)
            self.assertFalse((second / "saved.mp4").exists())
        self.assertEqual((first / "saved.mp4").read_bytes(), b"account-a-private-media")


class MegaAuthenticationTests(unittest.TestCase):
    def test_failed_login_restores_session_and_does_not_echo_password(self):
        with patch.object(mega, "is_installed", return_value=True), patch.object(mega, "whoami", return_value="old@example.test"), \
                patch.object(mega, "_session_token", return_value="preserved"), patch.object(mega, "restore_session") as restore, \
                patch.object(mega, "_run", side_effect=[mega.Result(0, "", ""), mega.Result(1, "password-should-not-appear", "bad login")]) as command:
            with self.assertRaises(mega.MegaError) as caught:
                mega.authenticate("new@example.test", "password-should-not-appear")
            self.assertNotIn("password-should-not-appear", str(caught.exception))
            self.assertEqual(command.call_args_list[0].args[0], ["logout", "--keep-session"])
            restore.assert_called_once_with("preserved")


if __name__ == "__main__":
    unittest.main()
