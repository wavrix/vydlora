"""Local library previews must be cached without fetching or altering media."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.api.routers import downloads
from app.core import media_convert as media
from app.services import storage_service as storage


class VideoThumbnailTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "build"
        base.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix="video-thumbnails-", dir=base)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.video = self.root / "123.mp4"
        self.video.write_bytes(b"original video")
        self.poster = storage.poster_for(self.video)
        self.record = SimpleNamespace(id=1, file_path=str(self.video), post_type="video", remote_path=None)
        self.db = Mock()
        self.db.get.return_value = self.record
        app = FastAPI()
        app.include_router(downloads.router)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.addCleanup(patch.stopall)
        patch.object(downloads, "_download_roots", return_value=[self.root]).start()
        patch.object(storage, "release_temporary_media").start()
        patch.object(media, "ffmpeg_path", return_value="ffmpeg").start()
        self.decoder = patch.object(media, "_run", side_effect=self.decode).start()
        self.fetch = patch.object(storage, "materialize", side_effect=AssertionError("must stay local")).start()

    @staticmethod
    def decode(args, timeout):
        assert timeout == 30
        Path(args[-1]).write_bytes(b"\xff\xd8complete JPEG\xff\xd9")
        return SimpleNamespace(returncode=0)

    def test_endpoint_generates_jpeg_once_and_preserves_original(self):
        for _ in range(2):
            response = self.client.get("/api/downloads/1/thumbnail")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "image/jpeg")
            self.assertEqual(response.content, self.poster.read_bytes())
        self.decoder.assert_called_once()
        self.fetch.assert_not_called()
        self.assertEqual(self.video.read_bytes(), b"original video")
        self.assertEqual(set(self.root.iterdir()), {self.video, self.poster})

    def test_concurrent_requests_share_one_completed_poster(self):
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(lambda _: media.video_poster(self.video, self.poster), range(8)))
        self.assertEqual(results, [self.poster] * 8)
        self.decoder.assert_called_once()

    def test_failed_or_timed_out_decode_leaves_no_partial_poster(self):
        def fail(args, timeout):
            self.assertFalse(self.poster.exists())
            Path(args[-1]).write_bytes(b"unfinished")
            return SimpleNamespace(returncode=1)
        for decoder in (fail, lambda *args, **kwargs: None):
            with self.subTest(decoder=decoder):
                self.decoder.side_effect = decoder
                self.assertIsNone(media.video_poster(self.video, self.poster))
                self.assertEqual(list(self.root.iterdir()), [self.video])
                self.assertEqual(self.video.read_bytes(), b"original video")

    def test_empty_sidecar_is_repaired(self):
        self.poster.touch()
        self.assertEqual(self.client.get("/api/downloads/1/thumbnail").status_code, 200)
        self.assertGreater(self.poster.stat().st_size, 0)

    def test_missing_converter_or_media_falls_back_without_an_error(self):
        with patch.object(media, "ffmpeg_path", return_value=None):
            self.assertEqual(self.client.get("/api/downloads/1/thumbnail").status_code, 404)
        self.video.unlink()
        self.record.remote_path = "/cloud/123.mp4"
        self.assertEqual(self.client.get("/api/downloads/1/thumbnail").status_code, 404)
        self.decoder.assert_not_called()
        self.fetch.assert_not_called()

    def test_existing_poster_serves_even_after_video_is_offloaded(self):
        self.poster.write_bytes(b"existing preview")
        self.video.unlink()
        response = self.client.get("/api/downloads/1/thumbnail")
        self.assertEqual((response.status_code, response.content), (200, b"existing preview"))
        self.decoder.assert_not_called()

    def test_media_outside_download_roots_cannot_be_decoded(self):
        self.record.file_path = str(self.root.parent / "outside.mp4")
        self.assertEqual(self.client.get("/api/downloads/1/thumbnail").status_code, 403)
        self.decoder.assert_not_called()

    def test_photo_albums_continue_to_serve_the_first_image(self):
        album = self.root / "album"
        album.mkdir()
        (album / "01.jpg").write_bytes(b"first photo")
        self.record.file_path, self.record.post_type = str(album), "image"
        response = self.client.get("/api/downloads/1/thumbnail")
        self.assertEqual((response.status_code, response.content), (200, b"first photo"))
        self.decoder.assert_not_called()

    def test_cloud_upload_can_generate_a_local_preview_when_cover_is_unavailable(self):
        self.assertEqual(storage.ensure_poster(self.db, self.video, "/cloud/123.mp4"), self.poster)
        self.decoder.assert_called_once()


class VideoPosterIntegrationTests(unittest.TestCase):
    def test_real_ffmpeg_output_is_a_small_readable_jpeg(self):
        executable = media.ffmpeg_path()
        if not executable:
            self.skipTest("ffmpeg is not installed")
        base = Path(__file__).resolve().parents[1] / "build"
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="poster-integration-", dir=base) as folder:
            video = Path(folder) / "short.mp4"
            result = media._run([executable, "-v", "error", "-y", "-f", "lavfi", "-i",
                                 "color=c=blue:s=640x360:r=10:d=0.2", "-c:v", "mpeg4", str(video)], timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            original = video.read_bytes()
            poster = media.video_poster(video, storage.poster_for(video))
            self.assertIsNotNone(poster)
            self.assertTrue(poster.read_bytes().startswith(b"\xff\xd8"))
            self.assertLess(poster.stat().st_size, 100_000)
            self.assertEqual(video.read_bytes(), original)
            decoded = media._run([executable, "-v", "error", "-i", str(poster), "-f", "null", "-"], timeout=30)
            self.assertEqual(decoded.returncode, 0, decoded.stderr)


if __name__ == "__main__":
    unittest.main()
