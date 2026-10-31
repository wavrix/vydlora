"""X discovery, storage, monitoring and Telegram platform isolation."""
import asyncio
import json
import tempfile
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app import models, platforms, repositories as repo
from app.core.database import Base
from app.models.enums import DownloadStatus
from app.x import profiles, urls
from app.services import cookie_service, settings_service, x_service as service, x_monitor_service as monitor, telegram_service as bot

class RoutingTests(unittest.TestCase):
    def test_profile_aliases_and_media_tab(self):
        for value in ("https://x.com/Creator", "https://twitter.com/Creator?lang=en", "mobile.twitter.com/Creator/media"):
            self.assertEqual(urls.extract_username(value), "Creator")
            self.assertEqual(platforms.detect(value), "x")

    def test_status_does_not_become_a_profile(self):
        for value in ("https://x.com/name/status/123/photo/2?s=20", "https://twitter.com/i/web/status/123"):
            self.assertEqual(urls.post_id(value), "123")
            self.assertIsNone(urls.extract_username(value))
            self.assertEqual(platforms.detect(value), "x")

    def test_rejects_reserved_paths_lookalikes_and_credentials(self):
        for value in ("https://x.com/home", "https://x.com/intent", "https://x.com.evil.test/a/status/1",
                      "https://x.com@evil.test/a/status/1", "https://x.com:8080/a/status/1",
                      "https://x.com/name/likes", "https://x.com/abcdefghijklmnop", "ftp://x.com/name"):
            self.assertFalse(urls.is_x_url(value), value)

    def test_discovery_deduplicates_carousels_and_excludes_other_authors(self):
        rows = [[3, "url", {"tweet_id": 123, "user": {"name": "creator"}}],
                [3, "url", {"tweet_id": 123, "user": {"name": "creator"}}],
                [3, "url", {"tweet_id": 999, "user": {"name": "someoneelse"}}],
                [3, "url", {"tweet_id": 124, "user": {"name": "CREATOR"}}]]
        with patch.object(profiles, "_query", return_value=rows):
            self.assertEqual(profiles.posts("creator", None, 10), ["123", "124"])

    def test_profile_uses_x_identity(self):
        rows = [[2, "", {"id": 222, "name": "creator", "nick": "Creator", "followers_count": 5, "profile_image": "https://pbs.twimg.com/a.jpg"}]]
        with patch.object(profiles, "_query", return_value=rows):
            self.assertEqual(profiles.probe("creator", None)["owner_id"], "222")

    def test_full_listing_has_no_post_cap_or_overall_timeout(self):
        rows = [[3, "url", {"tweet_id": n, "user": {"name": "creator"}}] for n in range(1, 602)]
        proc = SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
        with patch.object(profiles.subprocess, "run", return_value=proc) as run:
            self.assertEqual(len(profiles.posts("creator", None)), 601)
            self.assertNotIn("--post-range", run.call_args.args[0])
            self.assertIsNone(run.call_args.kwargs["timeout"])
            self.assertIn("timeout=25", run.call_args.args[0])

    def test_partial_listing_retains_posts_and_reports_error_record(self):
        rows = [[3, "url", {"tweet_id": 123, "user": {"name": "creator"}}],
                [-1, {"error": "RateLimitError", "message": "429"}]]
        proc = SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
        with patch.object(profiles.subprocess, "run", return_value=proc):
            listing = profiles.posts("creator", None)
        self.assertEqual(listing, ["123"])
        self.assertIn("limiting", listing.error)

    def test_streaming_listing_is_uncapped_and_deduplicates_media(self):
        rows = [[2, "", {"tweet_id": n, "user": {"name": "creator"}, "_lora1_status": {"id_str": str(n)}}] for n in range(1, 602)]
        rows += [rows[0], [2, "", {"tweet_id": 999, "user": {"name": "another"}}]]
        with patch.object(profiles, "_iter_rows", return_value=iter(rows)) as listing:
            posts = list(profiles.iter_posts("creator", None))
        self.assertEqual(len(posts), 601)
        self.assertIsNone(listing.call_args.args[2])
        self.assertEqual(posts[0].status["id_str"], "1")

    def test_listing_is_bounded_and_errors_are_not_empty_success(self):
        proc = SimpleNamespace(returncode=0, stdout="[]", stderr="")
        with patch.object(profiles.subprocess, "run", return_value=proc) as run:
            self.assertEqual(profiles._run_query("https://x.com/creator/media", None, 10), [])
            args = run.call_args.args[0]
            self.assertEqual(args[args.index("--post-range") + 1], "1-10")
            self.assertEqual(run.call_args.kwargs["timeout"], 180)
        proc.returncode = 1
        proc.stderr = "429 rate limit"
        with patch.object(profiles.subprocess, "run", return_value=proc):
            with self.assertRaisesRegex(ValueError, "limiting"):
                profiles._run_query("https://x.com/creator/media", None, 10)

class XTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "build")
        self.root = Path(self.temp.name)
        self.engine = create_engine("sqlite:///" + str(self.root / "db.sqlite"), connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.sessions()
        settings_service.ensure_defaults(self.db)
        self.db.commit()
        self.profile = {"username": "creator", "fullname": "Creator & Friends", "owner_id": "222"}
        self.post = {"post_id": "123", "url": "https://x.com/i/web/status/123", "author": "creator", "text": "Caption",
                     "profile": self.profile, "media": [{"id": "1", "kind": "image"}, {"id": "2", "kind": "video"}]}
        self.patches = [patch.object(service, "probe_creator", return_value=(self.profile, "")),
                        patch.object(service.settings_service, "downloads_path", return_value=self.root / "media"),
                        patch.object(service.events_service, "emit"),
                        patch.object(service.storage_service, "store_download"),
                        patch.object(service.void_media, "inspect_post", return_value=self.post),
                        patch.object(service.void_media, "download_media", side_effect=self.download),
                        patch.object(bot, "session_scope", self.scope),
                        patch.object(monitor, "session_scope", self.scope)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    @contextmanager
    def scope(self):
        with self.sessions() as db:
            yield db
            db.commit()

    def download(self, post, item, folder):
        name = f"x_{post['post_id']}_{item['id']}.{'jpg' if item['kind'] == 'image' else 'mp4'}"
        (folder / name).write_bytes(b"fixture-media")
        return {"filename": name}

    def test_monitor_add_and_stop_preserve_other_platforms_and_history(self):
        tt = models.User(state_key="@creator", platform="tiktok", username="creator", monitored=True)
        self.db.add(tt)
        self.db.commit()
        user = service.set_monitoring(self.db, "Creator", True)
        self.assertEqual((user.platform, user.platform_uid), ("x", "222"))
        service.download_url(self.db, self.post["url"])
        service.set_monitoring(self.db, "creator", False)
        self.assertTrue(tt.monitored)
        self.assertFalse(user.monitored)
        self.assertEqual(len(list(self.db.scalars(select(models.Download)))), 2)

    def test_download_records_every_media_and_uploads_to_mega(self):
        result = service.download_url(self.db, self.post["url"])
        self.assertEqual(result["count"], 2)
        rows = list(self.db.scalars(select(models.Download)))
        self.assertEqual({r.video_id for r in rows}, {"123_1", "123_2"})
        self.assertTrue(all(r.strategy == "void-x" and r.status == DownloadStatus.completed for r in rows))
        self.assertTrue(all("X" in Path(r.file_path).parts for r in rows))
        self.assertEqual(service.storage_service.store_download.call_count, 2)

    def test_duplicate_post_does_not_download_again_but_manual_can_send_saved_media(self):
        service.download_url(self.db, self.post["url"])
        result = service.download_url(self.db, self.post["url"], use_archive=True)
        self.assertEqual(result["count"], 0)
        self.assertTrue(result["empty"])
        self.assertEqual(service.void_media.download_media.call_count, 2)
        result = service.download_url(self.db, self.post["url"])
        self.assertEqual(len(result["files"]), 2)

    def test_cloud_history_is_used_for_duplicate_prevention(self):
        user = service.get_or_create_creator(self.db, "creator")
        self.db.add(models.Download(user_id=user.id, video_id="123_1", remote_path="/Vydlora/X/old.jpg", status="completed"))
        self.db.commit()
        result = service.download_url(self.db, self.post["url"], use_archive=True)
        self.assertEqual(result["count"], 1)
        service.void_media.download_media.assert_called_once()

    def test_manual_profile_download_processes_posts_beyond_old_cap(self):
        identifiers = [str(n) for n in range(1, 66)]
        saved = {"ok": True, "count": 1, "files": [{"path": "saved"}]}
        with patch.object(profiles, "iter_posts", return_value=identifiers) as listing, patch.object(service, "download_url", return_value=saved) as download:
            result = service.download_profile(self.db, "creator")
        self.assertIsNone(listing.call_args.args[2])
        self.assertEqual(download.call_count, 65)
        self.assertEqual(download.call_args.args[1], urls.status_url("65"))
        self.assertEqual(result["count"], 65)
        self.assertEqual(result["detail"], "All available media posts")
        self.assertIsNone(result["limit"])

    def test_incomplete_history_is_not_reported_as_all_saved(self):
        def listing():
            yield "123"
            raise ValueError("X is limiting requests. Try again later.")
        with patch.object(profiles, "iter_posts", side_effect=lambda *args: listing()):
            result = service.download_profile(self.db, "creator")
        self.assertEqual(result["count"], 2)
        self.assertIn("listing", result["errors"])
        self.assertTrue(result["ok"])

    def test_telegram_profile_download_has_no_cap(self):
        saved = {"ok": True, "count": 1, "files": [{"path": "saved"}]}
        with patch.object(bot.settings_service, "get", return_value=False), patch.object(profiles, "iter_posts", return_value=[str(n) for n in range(1, 41)]) as listing, patch.object(service, "download_url", return_value=saved) as download:
            result = bot._ig_work(123, "https://x.com/creator", profile=True, platform="x")
        self.assertIsNone(listing.call_args.args[2])
        self.assertEqual(download.call_count, 40)
        self.assertEqual(result["count"], 40)

    def test_first_download_starts_before_discovery_process_finishes(self):
        flag = self.root / "first-saved"
        row = [2, "", {"tweet_id": 123, "user": {"name": "creator"}}]
        script = ("import json,time,pathlib,sys\n"
                  f"print({json.dumps(json.dumps(row))},flush=True)\n"
                  f"flag=pathlib.Path({str(flag)!r})\n"
                  "deadline=time.monotonic()+5\n"
                  "while not flag.exists() and time.monotonic()<deadline: time.sleep(.02)\n"
                  "sys.exit(0 if flag.exists() else 1)\n")
        popen = subprocess.Popen
        def spawn(cmd, **kwargs):
            self.assertNotIn("--post-range", cmd)
            self.assertIn("output.jsonl=true", cmd)
            self.assertIn("extractor.twitter.ratelimit=abort", cmd)
            return popen([sys.executable, "-u", "-c", script], **kwargs)
        def saved(*args, **kwargs):
            flag.write_text("saved", encoding="utf-8")
            return {"ok": True, "count": 1, "files": [{"path": "saved"}]}
        with patch.object(profiles.subprocess, "Popen", side_effect=spawn), patch.object(service, "download_url", side_effect=saved):
            result = service.download_profile(self.db, "creator")
        self.assertTrue(flag.exists())
        self.assertEqual(result["count"], 1)
        self.assertFalse(result["errors"])

    def test_profile_uses_discovered_details_without_refetching_the_post(self):
        discovered = profiles.DiscoveredPost("123", {"id_str": "123"})
        with patch.object(profiles, "iter_posts", return_value=[discovered]), patch.object(service.void_media, "inspect_status", return_value=self.post) as inspected:
            result = service.download_profile(self.db, "creator")
        service.void_media.inspect_post.assert_not_called()
        inspected.assert_called_once()
        self.assertEqual(result["count"], 2)

    def test_streamed_rate_limit_keeps_saved_media_and_releases_child(self):
        row = [2, "", {"tweet_id": 123, "user": {"name": "creator"}}]
        error = [-1, {"error": "AbortExtraction", "message": "Rate limit exceeded"}]
        script = f"import sys\nprint({json.dumps(json.dumps(row))},flush=True)\nprint({json.dumps(json.dumps(error))},flush=True)\nsys.exit(1)\n"
        popen = subprocess.Popen
        children = []
        def spawn(cmd, **kwargs):
            child = popen([sys.executable, "-u", "-c", script], **kwargs)
            children.append(child)
            return child
        with patch.object(profiles.subprocess, "Popen", side_effect=spawn):
            result = service.download_profile(self.db, "creator")
        self.assertEqual(result["count"], 2)
        self.assertTrue(result["ok"])
        self.assertIn("limiting", result["errors"]["listing"])
        self.assertTrue(all(child.poll() is not None for child in children))

    async def test_x_download_survives_expired_button_acknowledgement(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        query = SimpleNamespace(data="xdl:creator", message=message, answer=AsyncMock(side_effect=RuntimeError("expired")))
        update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=123))
        with patch.object(bot, "_approved", return_value=True), patch.object(bot, "_run_ig_profile", new_callable=AsyncMock) as download:
            await bot._on_button(update, SimpleNamespace())
            await asyncio.sleep(0)
            download.assert_awaited_once_with(123, "creator", message, "x")

    async def test_repeated_x_download_button_does_not_start_another_profile_job(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        query = SimpleNamespace(data="xdl:creator", message=message, answer=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=123))
        waiting = asyncio.Event()
        async def job(*args):
            await waiting.wait()
        with patch.object(bot, "_approved", return_value=True), patch.object(bot, "_run_ig_profile", side_effect=job) as download:
            await bot._on_button(update, SimpleNamespace())
            await bot._on_button(update, SimpleNamespace())
            await asyncio.sleep(0)
            self.assertEqual(download.call_count, 1)
            query.answer.assert_awaited_with("Already downloading this creator.")
            waiting.set()
            await asyncio.sleep(0)

    async def test_failed_opening_message_does_not_stop_x_saving(self):
        message = SimpleNamespace(reply_text=AsyncMock(side_effect=RuntimeError("offline")))
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        result = {"ok": True, "count": 2, "errors": {}, "creators": ["creator"], "detail": ""}
        with patch.object(bot, "_app", app), patch.object(bot, "_ig_work", return_value=result) as work:
            await bot._run_ig_profile(123, "creator", message, "x")
        work.assert_called_once()
        self.assertIn("on_progress", work.call_args.kwargs)
        app.bot.send_message.assert_awaited_once()

    async def test_worker_timeout_finishes_with_an_error_instead_of_retrying_forever(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(bot, "_app", app), patch.object(bot, "_ig_work", side_effect=TimeoutError("X timed out")):
            await asyncio.wait_for(bot._run_ig_profile(123, "creator", message, "x"), timeout=2)
        app.bot.send_message.assert_awaited_once()
        self.assertIn("went wrong", app.bot.send_message.call_args.args[1])

    def test_partial_failure_is_reported_and_success_is_preserved(self):
        def partial(post, item, folder):
            if item["id"] == "2":
                raise ValueError("X unavailable")
            return self.download(post, item, folder)
        service.void_media.download_media.side_effect = partial
        result = service.download_url(self.db, self.post["url"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        self.assertIn("Media 2", result["warning"])

    def test_telegram_partial_download_summary_does_not_claim_everything_saved(self):
        result = {"ok": True, "count": 1, "files": [], "creators": ["creator"], "warning": "Media 2 failed"}
        with patch.object(bot.settings_service, "get", return_value=False), patch.object(service, "download_url", return_value=result):
            summary = bot._ig_work(123, self.post["url"], platform="x")
        self.assertIn("could not be saved", summary["text"])
        self.assertTrue(summary["errors"])

    async def test_telegram_keeps_partial_failure_notice_after_one_successful_attachment(self):
        result = {"ok": True, "count": 1, "text": "Saved one; another failed", "errors": {"media": "failed"},
                  "items": [{"path": "fixture.jpg", "post_type": "image", "caption": "Caption", "creator": {"username": "creator", "platform": "x"}}]}
        status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
        async def progress(status, task, **kwargs):
            await task
        with patch.object(bot, "_ig_work", return_value=result), patch.object(bot, "_send_file", new_callable=AsyncMock), patch.object(bot, "_keep_posted", side_effect=progress):
            await bot._run_ig_download(123, self.post["url"], status, "x")
        status.delete.assert_not_awaited()
        status.edit_text.assert_awaited_once_with(result["text"])

    def test_identity_change_does_not_download_or_replace_metadata(self):
        user = service.set_monitoring(self.db, "creator", True)
        user.platform_uid = "old"
        self.db.commit()
        result = service.download_url(self.db, self.post["url"], expected_user=user)
        self.assertFalse(result["ok"])
        self.assertEqual(user.platform_uid, "old")
        service.void_media.download_media.assert_not_called()

    def test_empty_scan_is_healthy_and_failures_require_three_attempts(self):
        user = service.set_monitoring(self.db, "creator", True)
        with patch.object(service, "download_profile", return_value={"ok": True, "empty": True, "count": 0}):
            self.assertEqual(monitor.scan_user(self.db, user), 0)
        self.assertEqual(user.scan_failures, 0)
        with patch.object(service, "download_profile", return_value={"ok": False, "error": "X unavailable"}):
            monitor.scan_user(self.db, user)
            monitor.scan_user(self.db, user)
            self.assertEqual(user.status, "monitoring")
            monitor.scan_user(self.db, user)
            self.assertEqual(user.status, "error")

    def test_sweep_requires_cookies_and_respects_pause_and_device_ownership(self):
        repo.Settings.set(self.db, "x_monitor_enabled", True)
        self.db.commit()
        with patch.object(cookie_service, "has_x_cookies", return_value=False):
            self.assertIn("cookies", monitor.deferral_reason(self.db))
        repo.Settings.set(self.db, "monitoring_paused", True)
        self.db.commit()
        self.assertIn("paused", monitor.deferral_reason(self.db))
        repo.Settings.set(self.db, "monitoring_paused", False)
        self.db.commit()
        from app.services.cloud_sync_service import sync
        with patch.object(sync, "can_automate", return_value=False):
            self.assertIn("another device", monitor.deferral_reason(self.db))

    def test_cookie_validation_filters_other_sites_and_bad_upload_preserves_session(self):
        data = b"# Netscape HTTP Cookie File\n.x.com\tTRUE\t/\tTRUE\t2147483647\tauth_token\tfixture\n.x.com\tTRUE\t/\tTRUE\t2147483647\tct0\tfixture\n.example.com\tTRUE\t/\tTRUE\t2147483647\tother\tprivate\n"
        with patch.object(cookie_service, "COOKIES_DIR", self.root):
            self.assertEqual(cookie_service.save_x_cookies(data), 2)
            before = cookie_service.x_cookies_path().read_bytes()
            self.assertNotIn(b"example.com", before)
            with self.assertRaises(ValueError):
                cookie_service.save_x_cookies(b"not cookies")
            self.assertEqual(cookie_service.x_cookies_path().read_bytes(), before)

    def test_telegram_buttons_and_caption_match_other_platforms(self):
        info = {"username": "creator", "nickname": "Creator & Friends", "uid": "222"}
        caption = bot._creator_caption(info, "x")
        self.assertEqual(caption.replace("https://x.com/creator", "https://www.tiktok.com/@creator"), bot._creator_caption(info))
        for monitored, prefix in ((False, "xmon"), (True, "xunmon")):
            buttons = bot._creator_buttons("creator", monitored, "x").inline_keyboard[0]
            self.assertEqual(buttons[0].callback_data, prefix + ":creator")
            self.assertEqual(buttons[1].callback_data, "xdl:creator")

    async def test_telegram_profile_and_post_route_to_x(self):
        status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
        message = SimpleNamespace(text="https://twitter.com/creator", reply_text=AsyncMock(return_value=status))
        update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=123))
        with patch.object(bot, "_approved", return_value=True), patch.object(bot, "_send_creator_card", new_callable=AsyncMock) as card, patch.object(bot, "_run_ig_download", new_callable=AsyncMock) as download:
            await bot._on_message(update, SimpleNamespace())
            card.assert_awaited_once_with(message, "creator", "x")
            download.assert_not_awaited()
            message.text = self.post["url"]
            await bot._on_message(update, SimpleNamespace())
            download.assert_awaited_once_with(123, self.post["url"], status, "x")

    async def test_telegram_toggle_button_changes_only_x_roster(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        query = SimpleNamespace(data="xmon:creator", message=message, answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=123))
        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(bot, "_approved", return_value=True):
            await bot._on_button(update, ctx)
            self.assertEqual(query.edit_message_reply_markup.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data, "xunmon:creator")
            query.data = "xunmon:creator"
            await bot._on_button(update, ctx)
        self.db.expire_all()
        self.assertFalse(service.find_creator(self.db, "creator").monitored)

    async def test_telegram_download_button_starts_x_profile_download(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        query = SimpleNamespace(data="xdl:creator", message=message, answer=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=123))
        with patch.object(bot, "_approved", return_value=True), patch.object(bot, "_run_ig_profile", new_callable=AsyncMock) as download:
            await bot._on_button(update, SimpleNamespace())
            await asyncio.sleep(0)
            download.assert_awaited_once_with(123, "creator", message, "x")

    def test_telegram_materializes_x_media_from_mega_and_uses_x_caption(self):
        result = service.download_url(self.db, self.post["url"])
        materialized = self.root / "cloud.jpg"
        materialized.write_bytes(b"cloud-media")
        with patch.object(bot.storage_service, "materialize", return_value=materialized) as fetch, patch.object(bot, "oversize", return_value=False), patch.object(bot, "build_post_caption", return_value="Caption") as caption:
            ready, skipped = bot._ig_items(self.db, result["files"], 1, "x")
            self.assertEqual(ready[0]["creator"]["platform"], "x")
            self.assertEqual(caption.call_args.kwargs["platform"], "x")
            self.assertEqual(ready[0]["path"], str(materialized))
            self.assertEqual(skipped, 0)
            fetch.assert_called_once()

    def test_scheduled_sweep_only_checks_due_x_creators(self):
        user = service.set_monitoring(self.db, "creator", True)
        self.db.add(models.User(state_key="@creator", platform="tiktok", username="creator", monitored=True))
        repo.Settings.set(self.db, "x_monitor_enabled", True)
        self.db.commit()
        with patch.object(cookie_service, "has_x_cookies", return_value=True), patch.object(service, "download_profile", return_value={"ok": True, "count": 0}) as download:
            self.assertEqual(monitor.scan_all(self.db), 0)
            download.assert_called_once()
            self.assertEqual(download.call_args.kwargs["expected_user"].platform, "x")
            self.assertEqual(download.call_args.kwargs["limit"], 10)
        self.db.refresh(user)
        self.assertIsNotNone(user.last_checked)

    def test_x_api_preview_monitor_download_and_settings_shapes(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routers import x as api
        from app.api.deps import get_db
        from app.schemas import SettingsOut, SettingsUpdate
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[get_db] = self.scope
        # Dependency overrides must be generators, not context manager objects.
        def database():
            with self.scope() as db:
                yield db
        app.dependency_overrides[get_db] = database
        client = TestClient(app)
        self.assertTrue(client.get("/api/x/preview", params={"username": "creator"}).json()["exists"])
        self.assertEqual(client.post("/api/x/monitor", json={"username": "creator"}).status_code, 200)
        self.assertEqual(client.post("/api/x/download", json={"url": self.post["url"]}).json()["count"], 2)
        with patch.object(service, "download_profile", return_value={"ok": True, "count": 0}) as download:
            self.assertEqual(client.post("/api/x/download", json={"url": "https://x.com/creator"}).status_code, 200)
            self.assertIsNone(download.call_args.kwargs["limit"])
            self.assertEqual(client.post("/api/x/download", json={"url": "https://x.com/creator", "limit": 501}).status_code, 200)
            self.assertEqual(download.call_args.kwargs["limit"], 501)
        self.assertFalse(client.get("/api/x/preview", params={"username": "bad handle"}).json()["addable"])
        values = settings_service.get_all(self.db)
        self.assertEqual(SettingsOut(**values).x_scan_interval, 900)
        self.assertEqual(SettingsUpdate(x_scan_interval=1200).x_scan_interval, 1200)
        with self.assertRaises(ValueError):
            SettingsUpdate(x_scan_interval=1)

    def test_shared_telegram_download_uses_void_and_x_creator_identity(self):
        with patch.object(bot.settings_service, "get", return_value=False):
            result = bot._ig_work(123, self.post["url"], platform="x")
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(service.void_media.inspect_post.call_args.args[0], self.post["url"])

if __name__ == "__main__":
    unittest.main()
