"""Profile cards and buttons must keep Instagram and TikTok identities separate."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models, platforms, repositories as repo
from app.core.database import Base
from app.services import telegram_service as bot, instagram_service, settings_service, user_service


class CreatorCardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "build")
        self.engine = create_engine("sqlite:///" + str(Path(self.temp.name) / "test.db"),
                                    connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.sessions() as db:
            settings_service.ensure_defaults(db)
            db.commit()
        self.profile = {"username": "creator", "fullname": "Creator & Friends",
                        "owner_id": "222", "avatar": "https://example.test/avatar.jpg"}
        self.patches = [patch.object(bot, "session_scope", self.scope),
                        patch.object(instagram_service, "probe_creator", return_value=(self.profile, "")),
                        patch.object(bot.events_service, "emit")]
        for item in self.patches:
            item.start()
        self.status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
        self.message = SimpleNamespace(text="https://www.instagram.com/creator/?igsh=abc",
                                       reply_text=AsyncMock(return_value=self.status), reply_photo=AsyncMock())
        self.query = SimpleNamespace(data="igmon:creator", message=self.message,
                                     answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
        self.update = SimpleNamespace(message=self.message, callback_query=self.query,
                                      effective_chat=SimpleNamespace(id=123))
        self.ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.engine.dispose()
        self.temp.cleanup()

    @contextmanager
    def scope(self):
        with self.sessions() as db:
            yield db
            db.commit()

    def add(self, platform, monitored=False):
        with self.scope() as db:
            user = models.User(state_key=platforms.state_key(platform, "creator"), platform=platform,
                               username="creator", nickname=platform.title(), monitored=monitored,
                               platform_uid="222" if platform == "instagram" else None,
                               tiktok_uid="111" if platform == "tiktok" else None)
            db.add(user)
            db.flush()
            return user.id

    def test_caption_uses_identical_format_and_escaped_name(self):
        info = {"username": "creator", "nickname": "Creator & Friends", "uid": "222"}
        instagram = bot._ig_caption(info)
        tiktok = bot._creator_caption(info)
        self.assertEqual(instagram.replace("https://www.instagram.com/creator/", "https://www.tiktok.com/@creator"), tiktok)
        self.assertIn("Creator &amp; Friends", instagram)
        self.assertTrue(instagram.endswith("@creator\nUID 222"))

    def test_buttons_toggle_and_stay_within_telegram_payload_limit(self):
        handle = "x" * 30
        for monitored, label, action in [(False, "Add to Monitoring", "igmon"), (True, "Stop Monitoring", "igunmon")]:
            buttons = bot._ig_buttons(handle, monitored).inline_keyboard[0]
            self.assertEqual([button.text for button in buttons], [label, "Download"])
            self.assertEqual(buttons[0].callback_data, action + ":" + handle)
            self.assertEqual(buttons[1].callback_data, "igdl:" + handle)
            self.assertTrue(all(len(button.callback_data.encode()) <= 64 for button in buttons))

    def test_live_profile_lookup_does_not_create_or_monitor_a_creator(self):
        self.add("tiktok", True)
        info = bot._ig_profile("creator")
        self.assertEqual((info["nickname"], info["uid"], info["avatar"]),
                         (self.profile["fullname"], "222", self.profile["avatar"]))
        self.assertFalse(info["monitored"])
        with self.scope() as db:
            self.assertIsNone(instagram_service.find_creator(db, "creator"))

    def test_unavailable_lookup_uses_saved_instagram_identity(self):
        self.add("tiktok", True)
        self.add("instagram", True)
        instagram_service.probe_creator.return_value = (None, "signed-out")
        info = bot._ig_profile("creator")
        self.assertEqual((info["nickname"], info["uid"]), ("Instagram", "222"))
        self.assertTrue(info["stale"])
        self.assertTrue(info["monitored"])

    def test_missing_profile_returns_error_instead_of_actions(self):
        instagram_service.probe_creator.return_value = (None, "not-found")
        self.assertIn("error", bot._ig_profile("creator"))

    async def test_instagram_card_uses_photo_and_shared_caption(self):
        await bot._send_ig_card(self.message, "creator")
        sent = self.message.reply_photo.call_args.kwargs
        self.assertEqual(sent["photo"], self.profile["avatar"])
        self.assertIn("UID 222", sent["caption"])
        self.assertEqual(sent["reply_markup"].inline_keyboard[0][0].text, "Add to Monitoring")
        self.status.delete.assert_awaited_once()

    async def test_failed_avatar_upload_falls_back_to_the_same_text_card(self):
        self.message.reply_photo.side_effect = RuntimeError("Photo unavailable")
        await bot._send_ig_card(self.message, "creator")
        self.assertIn("UID 222", self.status.edit_text.call_args.args[0])
        self.assertEqual(self.status.edit_text.call_args.kwargs["reply_markup"].inline_keyboard[0][1].callback_data, "igdl:creator")

    async def test_profile_link_routes_to_card_without_starting_a_download(self):
        with patch.object(bot, "_approved", return_value=True), patch.object(bot, "_send_ig_card", new_callable=AsyncMock) as card, patch.object(bot, "_run_ig_download", new_callable=AsyncMock) as download:
            await bot._on_message(self.update, self.ctx)
            card.assert_awaited_once_with(self.message, "creator")
            download.assert_not_awaited()

    async def test_post_link_keeps_its_direct_download_behavior(self):
        self.message.text = "https://www.instagram.com/p/ABC123/"
        with patch.object(bot, "_approved", return_value=True), patch.object(bot, "_send_ig_card", new_callable=AsyncMock) as card, patch.object(bot, "_run_ig_download", new_callable=AsyncMock) as download:
            await bot._on_message(self.update, self.ctx)
            card.assert_not_awaited()
            download.assert_awaited_once()

    async def test_monitor_and_stop_buttons_preserve_both_platforms_and_history(self):
        tiktok_id = self.add("tiktok", True)
        instagram_id = self.add("instagram")
        with self.scope() as db:
            db.add(models.Download(user_id=instagram_id, video_id="saved", remote_path="/saved.jpg"))
        with patch.object(bot, "_approved", return_value=True):
            await bot._on_button(self.update, self.ctx)
            self.assertEqual(self.query.edit_message_reply_markup.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data, "igunmon:creator")
            self.query.data = "igunmon:creator"
            await bot._on_button(self.update, self.ctx)
        with self.scope() as db:
            self.assertTrue(db.get(models.User, tiktok_id).monitored)
            user = db.get(models.User, instagram_id)
            self.assertFalse(user.monitored)
            self.assertEqual((user.platform_uid, user.nickname), ("222", self.profile["fullname"]))
            self.assertIsNotNone(db.scalar(select(models.Download).where(models.Download.user_id == instagram_id)))
        self.assertEqual(self.query.edit_message_reply_markup.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data, "igmon:creator")

    async def test_instagram_download_callback_never_uses_tiktok_downloader(self):
        self.query.data = "igdl:creator"
        with patch.object(bot, "_approved", return_value=True), patch.object(bot, "_run_ig_button", new_callable=AsyncMock) as download, patch.object(bot, "_button_download") as tiktok:
            await bot._on_button(self.update, self.ctx)
            download.assert_awaited_once_with(self.query, 123, "creator")
            tiktok.assert_not_called()

    async def test_unapproved_chat_cannot_use_instagram_monitoring_buttons(self):
        with patch.object(bot, "_approved", return_value=False), patch.object(bot, "_button_ig_monitor") as monitor:
            await bot._on_button(self.update, self.ctx)
            monitor.assert_not_called()
            self.ctx.bot.send_message.assert_not_awaited()

    def test_tiktok_stop_button_does_not_remove_same_named_instagram_creator(self):
        instagram_id = self.add("instagram", True)
        tiktok_id = self.add("tiktok", True)
        self.assertFalse(bot._button_unmonitor("creator")["monitored"])
        with self.scope() as db:
            self.assertIsNone(db.get(models.User, tiktok_id))
            self.assertTrue(db.get(models.User, instagram_id).monitored)

    def test_adding_tiktok_does_not_promote_same_named_instagram_row(self):
        instagram_id = self.add("instagram")
        meta = SimpleNamespace(username="creator", nickname="TikTok Creator", account_id="111",
                               sec_uid="tt-sec", avatar_url="", total_posts=0, followers=0)
        with self.scope() as db, patch.object(user_service, "resolve_account", return_value=SimpleNamespace(metadata=meta, videos=[])):
            user = user_service.add(db, "creator", baseline=False)
            self.assertEqual(user.platform, "tiktok")
            self.assertNotEqual(user.id, instagram_id)
            self.assertFalse(db.get(models.User, instagram_id).monitored)


if __name__ == "__main__":
    unittest.main()
