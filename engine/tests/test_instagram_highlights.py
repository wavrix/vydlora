"""Highlight pagination, real GraphQL errors and partial Telegram completion."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from gallery_dl.extractor.instagram import InstagramAPI
from gallery_dl.exception import AbortExtraction

from app.instagram import gallery
from app.services import instagram_service, telegram_service as bot


class HighlightQueryTests(unittest.TestCase):
    def api(self, responses):
        calls = []
        def request(url, **kwargs):
            calls.append(json.loads(kwargs["data"]["variables"]))
            payload = responses.pop(0)
            return SimpleNamespace(json=lambda: payload, text=json.dumps(payload))
        extr = SimpleNamespace(config=lambda *args: None, _user=None, root="https://www.instagram.com",
                               log=Mock(),
                               csrf_token="test-only", exc=SimpleNamespace(AbortExtraction=AbortExtraction),
                               request=request, cache=lambda fn, *args, **kwargs: fn(*args),
                               _init_cursor=lambda: None, _update_cursor=lambda cursor: cursor)
        api = InstagramAPI(extr)
        api.user = Mock(return_value={"id": "123"})
        api._extract_fb_tokens = Mock(return_value=("test-lsd", "test-dtsg"))
        return api, calls

    def test_query_id_matches_own_export_instead_of_another_operation(self):
        script = ('__d("Other",["' + gallery.HIGHLIGHTS_QUERY + '_instagramRelayOperation"],'
                  '(function(a){a.exports="28647748031575520"}),null);\n'
                  '__d("' + gallery.HIGHLIGHTS_QUERY + '_instagramRelayOperation",[], '
                  '(function(a){a.exports="26970053832668570"}),null);')
        self.assertEqual(gallery._operation_id(script, gallery.HIGHLIGHTS_QUERY), gallery.HIGHLIGHTS_DOC_ID)

    def test_dependency_reference_alone_does_not_return_another_query_id(self):
        script = '__d("Other",["' + gallery.HIGHLIGHTS_QUERY + '_instagramRelayOperation"],(function(a){a.exports="11111111111111111"}),null);'
        self.assertEqual(gallery._operation_id(script, gallery.HIGHLIGHTS_QUERY), "")

    def test_all_highlight_pages_are_read_and_query_methods_are_restored(self):
        responses = [{"data": {"highlights": {"edges": [{"node": {"id": "highlight:1"}}],
                      "page_info": {"has_next_page": True, "end_cursor": "next-page"}}}},
                     {"data": {"highlights": {"edges": [{"node": {"id": "highlight:2"}}],
                      "page_info": {"has_next_page": False}}}}]
        api, calls = self.api(responses)
        original_docid, original_request = api._extract_docid, api.extractor.request
        with patch.object(gallery, "_highlight_docid", lambda *args: gallery.HIGHLIGHTS_DOC_ID):
            tray = list(gallery._highlights_tray(api, "creator"))
        self.assertEqual([item["id"] for item in tray], ["highlight:1", "highlight:2"])
        self.assertEqual(calls[0]["max_highlights_to_fetch_on_pagination"], 12)
        self.assertEqual(calls[0]["first"], 12)
        self.assertIsNone(calls[0]["after"])
        self.assertEqual(calls[1]["after"], "next-page")
        self.assertEqual(api._extract_docid, original_docid)
        self.assertIs(api.extractor.request, original_request)

    def test_graphql_error_is_not_treated_as_an_account_without_highlights(self):
        api, _ = self.api([{"data": None, "errors": [{"code": 1675012}]}])
        original = api.extractor.request
        with patch.object(gallery, "_highlight_docid", lambda *args: gallery.HIGHLIGHTS_DOC_ID), self.assertRaises(AbortExtraction):
            list(gallery._highlights_tray(api, "creator"))
        self.assertIs(api.extractor.request, original)

    def test_unexpected_response_is_not_reported_as_empty(self):
        api, _ = self.api([{"data": {"different_query": {}}}])
        with patch.object(gallery, "_highlight_docid", lambda *args: gallery.HIGHLIGHTS_DOC_ID), self.assertRaises(AbortExtraction):
            list(gallery._highlights_tray(api, "creator"))

    def test_clean_empty_highlight_tray_is_valid(self):
        api, _ = self.api([{"data": {"highlights": {"edges": [], "page_info": {"has_next_page": False}}}}])
        with patch.object(gallery, "_highlight_docid", lambda *args: gallery.HIGHLIGHTS_DOC_ID):
            self.assertEqual(list(gallery._highlights_tray(api, "creator")), [])


class ProfileHighlightResultsTests(unittest.TestCase):
    def run_profile(self, results):
        with patch.object(instagram_service.cookie_service, "has_instagram_cookies", return_value=True), patch.object(instagram_service, "download_url", side_effect=results) as download:
            result = instagram_service.download_profile(Mock(), "creator", source="telegram")
            urls = [call.args[1] for call in download.call_args_list]
        self.assertEqual(urls, ["https://www.instagram.com/stories/creator/", "https://www.instagram.com/creator/highlights/", "https://www.instagram.com/creator/"])
        return result

    def success(self, count):
        return {"ok": True, "count": count, "files": [], "creators": ["creator"]}

    def test_telegram_profile_runs_highlights_and_counts_them(self):
        result = self.run_profile([self.success(1), self.success(39), self.success(5)])
        self.assertEqual(result["count"], 45)
        self.assertEqual(result["parts"]["highlights"], 39)
        self.assertEqual(result["errors"], {})

    def test_highlight_failure_is_kept_when_stories_and_posts_succeed(self):
        result = self.run_profile([self.success(1), {"ok": False, "empty": False, "error": "Rejected"}, self.success(5)])
        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], {"highlights": "Rejected"})

    def test_some_highlights_saved_before_failure_are_marked_partial(self):
        partial = self.success(3)
        partial["warning"] = "Request timed out"
        result = self.run_profile([self.success(1), partial, self.success(5)])
        self.assertEqual(result["parts"]["highlights"], 3)
        self.assertEqual(result["errors"], {"highlights": "Request timed out"})

    def test_account_without_highlights_has_no_failure_notice(self):
        result = self.run_profile([self.success(1), {"ok": False, "empty": True}, self.success(5)])
        self.assertEqual(result["errors"], {})


class TelegramHighlightCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_new_files_with_failed_highlights_does_not_claim_all_are_saved(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        result = {"ok": True, "count": 0, "errors": {"highlights": "Rejected"}}
        with patch.object(bot, "_app", app), patch.object(bot, "_ig_work", return_value=result):
            await bot._run_ig_profile(123, "creator", message)
        text = app.bot.send_message.call_args.args[1]
        self.assertIn("Couldn't finish highlights", text)
        self.assertNotIn("everything I can reach", text)

    async def test_partial_download_does_not_claim_everything_was_saved(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        result = {"ok": True, "count": 6, "detail": "1 story, 5 posts", "errors": {"highlights": "Rejected"}}
        with patch.object(bot, "_app", app), patch.object(bot, "_ig_work", return_value=result):
            await bot._run_ig_profile(123, "creator", message)
        text = app.bot.send_message.call_args.args[1]
        self.assertIn("Couldn't finish highlights", text)
        self.assertNotIn("All done", text)

    async def test_complete_download_includes_highlights_in_completion(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        result = {"ok": True, "count": 45, "detail": "1 story, 39 highlights, 5 posts", "errors": {}}
        with patch.object(bot, "_app", app), patch.object(bot, "_ig_work", return_value=result):
            await bot._run_ig_profile(123, "creator", message)
        text = app.bot.send_message.call_args.args[1]
        self.assertIn("39 highlights", text)
        self.assertIn("All done", text)


if __name__ == "__main__":
    unittest.main()
