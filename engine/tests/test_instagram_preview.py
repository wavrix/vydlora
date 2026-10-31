"""An unavailable Instagram lookup must not be reported as a missing account."""
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from app.api.routers import instagram
from app.instagram import downloader
from app.services import instagram_service


def result(*, code=0, error=None, message=None, stderr="", stdout=None):
    if stdout is None:
        stdout = json.dumps([[-1, {"error": error, "message": message}]]) if error else "[]"
    return subprocess.CompletedProcess([], code, stdout, stderr)


class InstagramProbeTests(unittest.TestCase):
    def probe(self, process):
        with patch.object(downloader.subprocess, "run", return_value=process):
            return downloader.probe_profile("https://www.instagram.com/protheme/", None)

    def test_json_notfound_with_success_exit_is_an_unverified_lookup(self):
        profile, reason = self.probe(result(error="NotFoundError", message="Requested user could not be found"))
        self.assertIsNone(profile)
        self.assertEqual(reason, "lookup-failed")

    def test_stderr_notfound_is_not_authoritative_either(self):
        self.assertEqual(self.probe(result(code=1, stderr="Requested user could not be found")), (None, "lookup-failed"))

    def test_json_errors_preserve_the_actionable_cause(self):
        for error, message, expected in [
            ("AuthRequired", "authenticated cookies", "signed-out"),
            ("AbortExtraction", "HTTP redirect to home page", "signed-out"),
            ("HttpError", "429 Too many requests", "rate-limited"),
            ("AbortExtraction", "checkpoint required", "signed-out"),
            ("AuthorizationError", "Account is private", "private"),
        ]:
            with self.subTest(error=error, message=message):
                self.assertEqual(self.probe(result(error=error, message=message)), (None, expected))

    def test_sign_in_and_throttling_take_priority_over_wrapped_notfound(self):
        for message, expected in [("Login required", "signed-out"), ("429 Too many requests", "rate-limited")]:
            with self.subTest(message=message):
                self.assertEqual(self.probe(result(error="NotFoundError", message="Requested user could not be found", stderr=message)), (None, expected))

    def test_empty_and_malformed_success_responses_are_unverified(self):
        for stdout in ["[]", "not JSON", "null", "{}"]:
            with self.subTest(stdout=stdout):
                self.assertEqual(self.probe(result(stdout=stdout)), (None, "no-profile"))

    def test_valid_profile_keeps_real_identity(self):
        process = result(stdout=json.dumps([[2, {"username":"protheme", "fullname":"Fixture profile", "owner_id":"123"}],
                                          [3, "https://example.test/avatar.jpg", {"display_url":"https://example.test/avatar.jpg"}]]))
        profile, reason = self.probe(process)
        self.assertEqual(reason, "")
        self.assertEqual((profile["username"], profile["owner_id"]), ("protheme", "123"))

    def test_timeout_remains_bounded_and_unverified(self):
        with patch.object(downloader.subprocess, "run", side_effect=subprocess.TimeoutExpired("gallery_dl", 30)):
            profile, reason = downloader.probe_profile("https://www.instagram.com/protheme/", None)
        self.assertIsNone(profile)
        self.assertIn("30s", reason)


class InstagramPreviewTests(unittest.TestCase):
    def setUp(self):
        self.probe = patch.object(instagram_service, "probe_creator", return_value=(None, "no-profile")).start()
        self.find = patch.object(instagram_service, "find_creator", return_value=None).start()
        self.addCleanup(patch.stopall)

    def preview(self):
        return instagram.preview_creator("protheme", Mock()).model_dump()

    def test_unavailable_lookups_leave_the_add_button_enabled(self):
        for reason in ["no-profile", "not-found", "lookup-failed", "signed-out", "rate-limited", "private", "no-cookies"]:
            with self.subTest(reason=reason):
                self.probe.return_value = (None, reason)
                preview = self.preview()
                self.assertFalse(preview["exists"])
                self.assertTrue(preview["addable"])
                self.assertNotIn("doesn't exist", preview["error"])
                self.assertEqual(preview["user_id"], "")

    def test_resolved_different_account_still_blocks_adding(self):
        self.probe.return_value = (None, "mismatch:someone_else")
        self.assertFalse(self.preview()["addable"])

    def test_empty_handle_is_still_rejected(self):
        preview = instagram.preview_creator(" ", Mock()).model_dump()
        self.assertFalse(preview["addable"])
        self.probe.assert_not_called()

    def test_valid_profile_shows_verified_metadata(self):
        self.probe.return_value = ({"username":"protheme", "fullname":"Fixture profile", "owner_id":"123"}, "")
        preview = self.preview()
        self.assertTrue(preview["exists"])
        self.assertTrue(preview["addable"])
        self.assertEqual(preview["user_id"], "123")

    def test_known_creator_is_preserved_when_lookup_fails(self):
        self.find.return_value = Mock()
        self.assertTrue(self.preview()["known"])

    def test_monitoring_can_start_without_inventing_a_uid(self):
        creator = Mock(platform_uid=None, nickname="protheme", avatar_url="", monitored=False)
        self.find.return_value = creator
        instagram_service.set_monitoring(Mock(), "protheme", True)
        self.assertTrue(creator.monitored)
        self.assertTrue(creator.enabled)
        self.assertIsNone(creator.platform_uid)
