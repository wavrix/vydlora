"""Browser recovery tests without real downloads or browser processes."""

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from app.tiktok import browser


class BrowserLaunchTests(unittest.TestCase):
    def setUp(self):
        self.chromium = Mock()
        self.pw = SimpleNamespace(chromium=self.chromium)
        self.full_launch = call(headless=True, channel="chromium", args=browser._LAUNCH_ARGS)
        self.fallback_launch = call(headless=True, args=browser._LAUNCH_ARGS)
        self.missing = RuntimeError("BrowserType.launch: Executable doesn't exist at chrome.exe\nInstall browsers")

    def test_installed_full_browser_does_not_install(self):
        with patch.object(browser, "_install_browser") as install:
            result = browser._launch(self.pw)
        self.assertIs(result, self.chromium.launch.return_value)
        self.assertEqual(self.chromium.launch.call_args_list, [self.full_launch])
        install.assert_not_called()

    def test_missing_full_browser_is_installed_before_fallback(self):
        full = object()
        self.chromium.launch.side_effect = [self.missing, full]
        with patch.object(browser, "_install_browser", return_value=True) as install:
            self.assertIs(browser._launch(self.pw), full)
        install.assert_called_once_with()
        self.assertEqual(self.chromium.launch.call_args_list, [self.full_launch, self.full_launch])

    def test_failed_install_uses_existing_shell(self):
        shell = object()
        self.chromium.launch.side_effect = [self.missing, shell]
        with patch.object(browser, "_install_browser", return_value=False), patch.object(browser.log, "warning") as warning:
            self.assertIs(browser._launch(self.pw), shell)
        self.assertEqual(self.chromium.launch.call_args_list, [self.full_launch, self.fallback_launch])
        self.assertNotIn("\n", warning.call_args.args[1])

    def test_failed_retry_falls_back(self):
        shell = object()
        self.chromium.launch.side_effect = [self.missing, RuntimeError("Launch failed"), shell]
        with patch.object(browser, "_install_browser", return_value=True):
            self.assertIs(browser._launch(self.pw), shell)
        self.assertEqual(self.chromium.launch.call_args_list, [self.full_launch, self.full_launch, self.fallback_launch])

    def test_other_launch_errors_do_not_download_browsers(self):
        shell = object()
        self.chromium.launch.side_effect = [RuntimeError("Browser crashed"), shell]
        with patch.object(browser, "_install_browser") as install:
            self.assertIs(browser._launch(self.pw), shell)
        install.assert_not_called()
        self.assertEqual(self.chromium.launch.call_args_list, [self.full_launch, self.fallback_launch])

    def test_no_usable_browser_propagates_failure(self):
        self.chromium.launch.side_effect = [self.missing, self.missing]
        with patch.object(browser, "_install_browser", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "Executable doesn't exist"):
                browser._launch(self.pw)

    def test_failed_download_is_attempted_only_once_per_process(self):
        failed = SimpleNamespace(returncode=1, stderr="Download failed")
        with patch.object(browser, "_install_tried", False), patch.object(browser.subprocess, "run", return_value=failed) as run:
            self.assertFalse(browser._install_browser())
            self.assertFalse(browser._install_browser())
        run.assert_called_once()

    def test_download_timeout_is_attempted_only_once_per_process(self):
        with patch.object(browser, "_install_tried", False), patch.object(browser.subprocess, "run", side_effect=subprocess.TimeoutExpired("playwright", 900)) as run:
            self.assertFalse(browser._install_browser())
            self.assertFalse(browser._install_browser())
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
