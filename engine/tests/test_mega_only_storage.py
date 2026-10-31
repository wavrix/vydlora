"""Cloud-only storage must preserve uploads and release temporary viewing files."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.services import storage_service as storage
from app.services import telegram_service
from app.api.routers import downloads


class MegaOnlyStorageTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "build"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="cloud-only-test-", dir=base)
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(storage, "cache_dir", return_value=self.root / "cache").start()
        self.db = Mock()
        self.media = self.root / "123.mp4"
        self.media.write_bytes(b"video")
        self.remote = "/Vydlora/TikTok/creator/123.mp4"
        patch.object(storage.mega_service, "is_enabled", return_value=True).start()
        patch.object(storage.mega_service, "remote_dir_for", return_value="/Vydlora/TikTok/creator").start()
        patch.object(storage, "keep_local_copies", return_value=False).start()

    def cleanup(self):
        allowed = Path(__file__).resolve().parents[1] / "build"
        assert self.root.parent == allowed.resolve()
        assert self.root.name.startswith("cloud-only-test-")
        self.temp.cleanup()

    def test_successful_upload_removes_media_and_existing_poster_without_caching(self):
        poster = storage.poster_for(self.media)
        poster.write_bytes(b"preview")
        with patch.object(storage.mega_service, "upload", return_value=self.remote), \
                patch.object(storage, "ensure_poster") as generate:
            self.assertEqual(storage.offload(self.db, self.media, self.root, cache_slot=self.root / "retained"), self.remote)
        self.assertFalse(self.media.exists())
        self.assertFalse(poster.exists())
        self.assertFalse((self.root / "retained").exists())
        generate.assert_not_called()

    def test_failed_upload_preserves_the_only_copy_and_preview(self):
        poster = storage.poster_for(self.media)
        poster.write_bytes(b"preview")
        with patch.object(storage.mega_service, "upload", side_effect=storage.mega_service.MegaError("offline")):
            self.assertIsNone(storage.offload(self.db, self.media, self.root))
        self.assertEqual(self.media.read_bytes(), b"video")
        self.assertTrue(poster.exists())

    def test_store_download_records_remote_without_allocating_retained_cache(self):
        download = SimpleNamespace(id=1, remote_path=None)
        with patch.object(storage.mega_service, "upload", return_value=self.remote), \
                patch.object(storage, "cache_slot") as cache:
            self.assertEqual(storage.store_download(self.db, download, self.media), self.remote)
        self.assertEqual(download.remote_path, self.remote)
        self.db.commit.assert_called_once()
        cache.assert_not_called()

    def test_legacy_cache_option_preserves_existing_behavior(self):
        slot = self.root / "retained"
        with patch.object(storage, "keep_local_copies", return_value=True), \
                patch.object(storage.mega_service, "upload", return_value=self.remote), \
                patch.object(storage, "ensure_poster") as generate:
            storage.offload(self.db, self.media, self.root, cache_slot=slot)
        self.assertTrue((slot / self.media.name).exists())
        generate.assert_called_once()

    def fetch(self, db, remote, slot):
        slot.mkdir(parents=True, exist_ok=True)
        result = slot / "123.mp4"
        result.write_bytes(b"remote video")
        return result

    def test_overlapping_views_use_independent_files_and_cleanup_after_response(self):
        self.media.unlink()
        download = SimpleNamespace(id=1, file_path=str(self.media), remote_path=self.remote)
        with patch.object(storage.mega_service, "fetch", side_effect=self.fetch), \
                patch.object(downloads, "_safe", side_effect=lambda path: path):
            first = storage.materialize(self.db, download)
            second = storage.materialize(self.db, download)
            self.assertNotEqual(first, second)
            response = downloads._serve(first)
            self.assertTrue(first.exists())
            asyncio.run(response.background())
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())
            storage.clear_temporary_media()
            self.assertFalse(second.exists())

    def test_failed_view_fetch_leaves_no_temporary_files(self):
        self.media.unlink()
        download = SimpleNamespace(id=1, file_path=str(self.media), remote_path=self.remote)
        with patch.object(storage.mega_service, "fetch", return_value=None):
            self.assertIsNone(storage.materialize(self.db, download))
        self.assertEqual(list(storage.temporary_dir().iterdir()), [])

    def test_cleanup_cannot_remove_library_or_cache_root(self):
        storage.release_temporary_media(self.media)
        storage.release_temporary_media(storage.temporary_dir())
        self.assertEqual(self.media.read_bytes(), b"video")

    def test_telegram_releases_temporary_media_when_sending_fails(self):
        self.media.unlink()
        download = SimpleNamespace(id=1, file_path=str(self.media), remote_path=self.remote)
        with patch.object(storage.mega_service, "fetch", side_effect=self.fetch):
            fetched = storage.materialize(self.db, download)
        with patch.object(telegram_service, "_send_file_contents", new=AsyncMock(side_effect=RuntimeError("send failed"))):
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                asyncio.run(telegram_service._send_file({1}, fetched, "video", ""))
        self.assertFalse(fetched.exists())
