"""Verify additive upgrades preserve an existing local data profile."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app import models, repositories as repo
from app.core import database


class ProfileUpgradeTests(unittest.TestCase):
    def test_legacy_profile_upgrade_is_repeatable_and_preserves_records(self):
        root = Path(__file__).resolve().parents[1] / "build"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="upgrade-test-", dir=root) as directory:
            engine = create_engine("sqlite:///" + str(Path(directory) / "legacy.db"))
            try:
                tables = [table for table in database.Base.metadata.sorted_tables if table.name != "cloud_records"]
                database.Base.metadata.create_all(engine, tables=tables)
                with Session(engine) as db:
                    user = models.User(state_key="@legacy", username="legacy", nickname="Saved creator")
                    db.add(user)
                    db.flush()
                    db.add(models.Post(user_id=user.id, video_id="saved", url="https://example.test/saved", downloaded=True))
                    db.add(models.Download(user_id=user.id, video_id="saved", file_path="D:/saved/media.mp4"))
                    repo.Settings.set(db, "scan_interval", 456)
                    repo.Settings.set(db, "mega_session", "local-only-test-session")
                    db.commit()
                missing = {
                    "users": {"platform", "platform_uid", "sec_uid", "monitored", "scan_failures"},
                    "posts": {"is_story"}, "downloads": {"remote_path", "telegram_chat_id"},
                    "queue_jobs": {"telegram_chat_id"},
                }
                for name, columns in missing.items():
                    for index in database.Base.metadata.tables[name].indexes:
                        if any(column.name in columns for column in index.columns):
                            index.drop(engine)
                    with engine.begin() as connection:
                        for column in columns:
                            connection.exec_driver_sql(f"ALTER TABLE {name} DROP COLUMN {column}")
                with patch.object(database, "engine", engine), patch.object(database, "_is_sqlite", True):
                    database.init_db()
                    database.init_db()
                self.assertIn("cloud_records", inspect(engine).get_table_names())
                with Session(engine) as db:
                    user = db.scalar(select(models.User))
                    self.assertEqual(user.nickname, "Saved creator")
                    self.assertEqual(user.platform, "tiktok")
                    self.assertTrue(user.monitored)
                    self.assertEqual(user.scan_failures, 0)
                    self.assertTrue(db.scalar(select(models.Post)).downloaded)
                    download = db.scalar(select(models.Download))
                    self.assertEqual(download.file_path, "D:/saved/media.mp4")
                    self.assertIsNone(download.remote_path)
                    self.assertEqual(repo.Settings.get(db, "scan_interval"), 456)
                    self.assertEqual(repo.Settings.get(db, "mega_session"), "local-only-test-session")
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
