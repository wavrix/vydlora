"""Isolated regression coverage for the sidebar and settings data contracts.

Run from backend: python -m unittest discover -s tests -v
No live database, accounts, workers or network requests are used.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import models, repositories as repo
from app.api.deps import get_db
from app.api.routers import users, system, downloads
from app.core.database import Base
from app.models.enums import DownloadStatus, UserStatus
from app.services import settings_service
from app.tiktok import downloader


class SidebarSettingsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        app = FastAPI()
        app.include_router(users.router)
        app.include_router(downloads.router)
        app.include_router(system.router)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def creator(self, name, platform="tiktok", downloaded=True, **kwargs):
        user = models.User(state_key=f"{platform}:{name}", username=name, nickname=f"Display {name}", platform=platform, **kwargs)
        self.db.add(user)
        self.db.flush()
        if downloaded:
            self.db.add(models.Download(user_id=user.id, video_id=name, status=DownloadStatus.completed))
        self.db.commit()
        return user

    def test_maximum_interval_persists_and_applies_without_resetting_penalty(self):
        before = (downloader._MIN_INTERVAL, downloader._MAX_INTERVAL, downloader._interval)
        try:
            downloader._interval = 18
            response = self.client.put("/api/settings", json={"tikdownloader_max_interval": 20})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(downloader._interval, 18)
            self.assertEqual(settings_service.get(self.db, "tikdownloader_max_interval"), 20)
            response = self.client.put("/api/settings", json={"tikdownloader_max_interval": 2, "tikdownloader_min_interval": 3})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(settings_service.get(self.db, "tikdownloader_max_interval"), 20)
        finally:
            downloader._MIN_INTERVAL, downloader._MAX_INTERVAL, downloader._interval = before

    def test_library_routes_are_removed_and_download_records_remain(self):
        user = self.creator("saved", monitored=False)
        self.db.query(models.Download).filter_by(user_id=user.id).update(
            {"remote_path": "/Vydlora/TikTok/saved/saved.mp4"}
        )
        self.db.commit()
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertNotIn("/api/users/library", paths)
        self.assertNotIn("/api/downloads/library", paths)
        self.assertIn("/api/downloads/url", paths)
        record = self.db.query(models.Download).filter_by(user_id=user.id).one()
        self.assertEqual(record.status, DownloadStatus.completed)
        self.assertEqual(record.remote_path, "/Vydlora/TikTok/saved/saved.mp4")

    def test_telegram_status_reports_daily_count_without_token(self):
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self.db.add_all([models.TelegramMessage(chat_id=1, text="today", created_at=today), models.TelegramMessage(chat_id=1, text="yesterday", created_at=today-timedelta(seconds=1))])
        self.db.commit()
        with patch.object(system.telegram_service, "is_running", return_value=False), patch.object(system.telegram_service, "bot_username", return_value=""):
            response = self.client.get("/api/telegram/status")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["requests_today"], 1)
        self.assertGreater(data["upload_limit_bytes"], 0)
        self.assertNotIn("bot_token", data)

    def test_telegram_disconnect_clears_token_and_preserves_allow_list(self):
        repo.Settings.set(self.db, "bot_token", "123456:" + "A"*35)
        repo.Settings.set(self.db, "telegram_allowed_ids", "123")
        self.db.commit()
        with patch.object(system.telegram_service, "restart"), patch.object(system.telegram_service, "is_running", return_value=False):
            response = self.client.put("/api/telegram/config", json={"bot_token": ""})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["token_set"])
        self.assertEqual(repo.Settings.get(self.db, "telegram_allowed_ids"), "123")


if __name__ == "__main__":
    unittest.main()
