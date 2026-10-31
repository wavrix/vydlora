"""Saving Telegram settings must never leave two polling threads alive."""
import asyncio
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, Mock, patch

from app.services import telegram_service as bot
from app.services.cloud_sync_service import sync


class TelegramLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(bot, "session_scope", return_value=MagicMock()),
                        patch.object(bot, "live_token", return_value="test-only-token"),
                        patch.object(bot, "live_allowed_ids", return_value={123}),
                        patch.object(sync, "can_automate", return_value=True),
                        patch.object(bot, "_run", side_effect=self.run_fake)]
        for item in self.patches:
            item.start()
        self.ready = threading.Event()
        self.guard = threading.Lock()
        self.active = self.maximum = self.starts = 0
        self.stop_thread_ids = []
        bot._thread = bot._app = bot._loop = None
        bot._stopping.set()

    def tearDown(self):
        bot.stop()
        for item in reversed(self.patches):
            item.stop()

    def run_fake(self, token):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        owner = self
        class Poller:
            def stop_running(self):
                # This reproduces PTB's requirement: calling from the API
                # thread raises, and stopping just its processor is insufficient.
                if asyncio.get_running_loop() is not loop:
                    raise RuntimeError("Wrong event loop")
                owner.stop_thread_ids.append(threading.get_ident())
                loop.stop()
        bot._app = Poller()
        bot._loop = loop
        with self.guard:
            self.active += 1
            self.starts += 1
            self.maximum = max(self.maximum, self.active)
        def initialized():
            self.ready.set()
            # Real PTB checks this in post_init too: settings may be saved
            # before startup has published the application and its loop.
            if bot._stopping.is_set():
                bot._app.stop_running()
        loop.call_soon(initialized)
        try:
            loop.run_forever()
        finally:
            with self.guard:
                self.active -= 1

    def start(self):
        self.ready.clear()
        bot.start()
        self.assertTrue(self.ready.wait(2), "Polling loop did not start")

    def test_shutdown_runs_on_the_bot_loop_and_joins_the_old_thread(self):
        self.start()
        previous = bot._thread
        bot.stop()
        self.assertFalse(previous.is_alive())
        self.assertEqual(self.stop_thread_ids, [previous.ident])
        self.assertEqual(self.active, 0)
        self.assertIsNone(bot._thread)
        self.assertIsNone(bot._app)
        self.assertIsNone(bot._loop)

    def test_start_is_idempotent_even_when_called_concurrently(self):
        self.start()
        previous = bot._thread
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: bot.start(), range(30)))
        self.assertIs(bot._thread, previous)
        self.assertEqual(self.starts, 1)
        self.assertEqual(self.maximum, 1)

    def test_repeated_settings_saves_leave_one_live_poller(self):
        self.start()
        for _ in range(8):
            previous = bot._thread
            self.ready.clear()
            bot.restart()
            self.assertTrue(self.ready.wait(2))
            self.assertFalse(previous.is_alive())
            self.assertTrue(bot.is_running())
        self.assertEqual(self.active, 1)
        self.assertEqual(self.maximum, 1)

    def test_concurrent_restarts_never_overlap_pollers(self):
        self.start()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: bot.restart(), range(12)))
        self.assertTrue(self.ready.wait(2))
        self.assertEqual(self.maximum, 1)
        self.assertTrue(bot._thread.is_alive())

    def test_slow_shutdown_keeps_references_and_refuses_a_second_poller(self):
        thread, app, loop = Mock(), Mock(), Mock()
        thread.is_alive.return_value = True
        bot._thread, bot._app, bot._loop = thread, app, loop
        try:
            with patch.object(bot.threading, "Thread") as new_thread:
                bot.restart()
                new_thread.assert_not_called()
            loop.call_soon_threadsafe.assert_called_once_with(app.stop_running)
            self.assertIs(bot._thread, thread)
            self.assertIs(bot._app, app)
            self.assertIs(bot._loop, loop)
            self.assertTrue(bot._stopping.is_set())
            self.assertFalse(bot.is_running())
        finally:
            bot._thread = bot._app = bot._loop = None

    def test_other_device_does_not_start_telegram(self):
        with patch.object(sync, "can_automate", return_value=False):
            bot.start()
        self.assertIsNone(bot._thread)
        self.assertEqual(self.starts, 0)


if __name__ == "__main__":
    unittest.main()
