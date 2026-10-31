"""Private staging and migration must preserve media and leave Downloads clean."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models, repositories as repo
from app.core.database import Base
from app.services import download_service, instagram_service, managed_storage_service as managed
from app.services import mega_service, settings_service as settings
import bootstrap


class ManagedStorageTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "build"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="managed-storage-", dir=base)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.appdata = self.root / "roaming"
        self.profile = self.appdata / "Vydlora"
        self.custom = self.root / "D-Downloads" / "Vydlora"
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"LORA1_DATA_DIR": str(self.profile), "APPDATA": str(self.appdata)}).start()
        patch.object(Path, "home", return_value=self.home).start()
        patch.object(settings, "cfg", SimpleNamespace(cookies_path=self.profile / "cookies.txt")).start()
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        repo.Settings.set(self.db, "downloads_dir", str(self.custom))
        self.db.commit()

    def creator(self, platform):
        user = models.User(state_key=platform+":@creator", username="creator", platform=platform,
                           folder_name="Creator")
        self.db.add(user)
        self.db.commit()
        return user

    def test_bootstrap_does_not_create_downloads_folder(self):
        self.assertEqual(bootstrap._default_downloads_dir(), settings.managed_downloads_path())
        self.assertFalse((self.home / "Downloads" / "Vydlora").exists())
        self.assertFalse(self.custom.exists())

    def test_platform_working_folders_are_private_and_cloud_layout_is_preserved(self):
        tiktok = self.creator("tiktok")
        instagram = self.creator("instagram")
        tt = download_service.account_dir(self.db, tiktok, tiktok.username)
        ig = instagram_service._account_dir(self.db, instagram)
        root = settings.managed_downloads_path()
        self.assertEqual(tt, root / "TikTok" / "Creator")
        self.assertEqual(ig, root / "Instagram" / "Creator")
        self.assertEqual(mega_service.remote_dir_for(self.db, tt / "Stories"), "/Vydlora/TikTok/Creator/Stories")
        self.assertFalse(self.custom.exists())

    def test_migration_preserves_local_media_history_metadata_and_cookie_inputs(self):
        user = self.creator("instagram")
        old = self.custom / "Instagram" / "Creator" / "123.jpg"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"only local copy awaiting upload")
        (old.parent / "metadata.json").write_text("{}")
        cookie = self.custom / "instagram_esoh.txt"
        cookie.write_text("input remains here")
        row = models.Download(user_id=user.id, video_id="123", file_path=str(old))
        self.db.add(row)
        self.db.commit()
        managed.migrate(self.db)
        new = settings.managed_downloads_path() / "Instagram" / "Creator" / old.name
        self.assertEqual(new.read_bytes(), b"only local copy awaiting upload")
        self.assertEqual(Path(row.file_path), new)
        self.assertTrue((new.parent / "metadata.json").is_file())
        self.assertEqual(cookie.read_text(), "input remains here")
        self.assertFalse((self.custom / "Instagram").exists())
        self.assertEqual(managed.migrate(self.db), {"moved": 0, "repathed": 0})

    def test_old_default_image_cache_moves_and_empty_default_folder_is_removed(self):
        old_root = self.home / "Downloads" / "Vydlora"
        image = old_root / ".cache" / "img" / "cover.jpg"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"cached image")
        managed.migrate(self.db)
        self.assertFalse(old_root.exists())
        self.assertEqual((settings.managed_downloads_path() / ".cache" / "img" / image.name).read_bytes(), b"cached image")

    def test_conflicting_files_are_preserved_without_repointing_the_old_download(self):
        user = self.creator("tiktok")
        old = self.custom / "TikTok" / "Creator" / "123.mp4"
        new = settings.managed_downloads_path() / "TikTok" / "Creator" / old.name
        old.parent.mkdir(parents=True)
        new.parent.mkdir(parents=True)
        old.write_bytes(b"old media")
        new.write_bytes(b"different media")
        row = models.Download(user_id=user.id, video_id="123", file_path=str(old))
        self.db.add(row)
        self.db.commit()
        managed.migrate(self.db)
        self.assertEqual(old.read_bytes(), b"old media")
        self.assertEqual(new.read_bytes(), b"different media")
        self.assertEqual(row.file_path, str(old))

    def test_isolated_profile_does_not_move_the_installed_default_cache(self):
        cache = self.home / "Downloads" / "Vydlora" / ".cache" / "img.jpg"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"installed profile")
        with patch.dict(os.environ, {"LORA1_DATA_DIR": str(self.root / "isolated-profile")}):
            managed.migrate(self.db)
        self.assertEqual(cache.read_bytes(), b"installed profile")

    def test_legacy_folder_changes_are_rejected_without_creating_the_folder(self):
        with self.assertRaisesRegex(ValueError, "managed automatically"):
            settings.set_downloads_dir(self.db, str(self.custom))
        self.assertFalse(self.custom.exists())

    def test_unavailable_source_keeps_the_only_local_download_recovery_path(self):
        user = self.creator("tiktok")
        old = self.custom / "TikTok" / "Creator" / "123.mp4"
        row = models.Download(user_id=user.id, video_id="123", file_path=str(old))
        self.db.add(row)
        self.db.commit()
        managed.migrate(self.db)
        self.assertEqual(row.file_path, str(old))


if __name__ == "__main__":
    unittest.main()
