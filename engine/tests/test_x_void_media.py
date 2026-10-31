import hashlib
import io
import http.client
import json
import tempfile
import threading
import unittest
import subprocess
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from contextlib import contextmanager

from app.x import void_media as dl


class InputTests(unittest.TestCase):
    def test_accepts_share_and_media_links(self):
        for value in ["https://x.com/skewxz/status/2107043041764937822?s=20", "twitter.com/name/status/123/video/2", "https://x.com/i/web/status/123"]:
            url, post_id = dl.normalize_url(value)
            self.assertEqual(url, f"https://x.com/i/web/status/{post_id}")

    def test_rejects_other_hosts_and_non_posts(self):
        for value in ["https://x.com.evil.test/a/status/12", "https://x.com@localhost/a/status/1", "http://127.0.0.1/a/status/1", "https://x.com/a", "https://x.com:123/a/status/1"]:
            with self.assertRaises(ValueError):
                dl.normalize_url(value)

    def test_image_original_keeps_encoding(self):
        self.assertEqual(dl.original_image_url("https://pbs.twimg.com/media/abc.png:large"), "https://pbs.twimg.com/media/abc?format=png&name=orig")
        self.assertEqual(dl.original_image_url("https://pbs.twimg.com/media/abc?format=jpg&name=small"), "https://pbs.twimg.com/media/abc?format=jpg&name=orig")

    def test_cookie_filter_removes_other_sites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.txt"
            path.write_text("# Netscape HTTP Cookie File\n.x.com\tTRUE\t/\tTRUE\t2147483647\tauth_token\ttest-only\n.example.com\tTRUE\t/\tTRUE\t2147483647\tsecret\ttest-only\n")
            self.assertEqual([c.domain for c in dl.read_cookies(path)], [".x.com"])


class MediaTests(unittest.TestCase):
    def test_discovered_status_uses_void_selection_without_another_api_request(self):
        status = {"id_str": "123", "user": {"screen_name": "creator"}, "extended_entities": {"media": [
            {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/test.jpg", "original_info": {"width": 4000, "height": 3000}}]}}
        with patch.object(dl.MediaTwitterIE, "extract", side_effect=AssertionError("must not refetch")):
            post = dl.inspect_status(status, "123")
        self.assertEqual(post["author"], "creator")
        self.assertEqual(post["media"][0]["quality"], "X original image size")
        self.assertIn("name=orig", post["media"][0]["_url"])
        with self.assertRaisesRegex(ValueError, "different post"):
            dl.inspect_status(status, "456")
    def test_recorded_dimensions_are_not_download_dimensions(self):
        self.assertFalse(dl.dimensions_match({"width": 720, "height": 720, "recorded_width": 1080, "recorded_height": 1080}))
        self.assertTrue(dl.dimensions_match({"width": 1080, "height": 1080, "recorded_width": 1080, "recorded_height": 1080}))
        self.assertIsNone(dl.dimensions_match({"width": 720, "height": 720}))

    def test_image_download_preserves_bytes_and_writes_report(self):
        payload = b"\x89PNG\r\n\x1a\nfixture-image-bytes"
        class Response(io.BytesIO):
            headers = {"Content-Type": "image/png", "Content-Length": str(len(payload))}
        class YDL:
            def urlopen(self, url):
                self.url = url
                return Response(payload)
        client = YDL()
        @contextmanager
        def fake_ydl(*args, **kwargs):
            yield client
        with tempfile.TemporaryDirectory() as directory, patch.object(dl, "youtube_dl", fake_ydl):
            item = {"id": "1", "kind": "image", "_ext": "png", "_url": "https://pbs.twimg.com/media/test?format=png&name=orig"}
            report = dl.download_media({"post_id": "123", "url": "https://x.com/i/web/status/123"}, item, Path(directory))
            self.assertEqual((Path(directory) / report["filename"]).read_bytes(), payload)
            self.assertEqual(report["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertIn("name=orig", client.url)
            self.assertFalse(report["original_verified"])

    def test_real_video_download_preserves_bytes_and_60fps(self):
        class SilentFileHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            subprocess.run([dl.FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=192x108:r=60:d=0.2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True, capture_output=True)
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SilentFileHandler, directory=directory))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/source.mp4"
                item = {"id": "1", "kind": "video", "_ext": "mp4", "_info": {"id": "123_1", "title": "fixture", "url": url, "ext": "mp4", "vcodec": "h264", "acodec": "none"}}
                report = dl.download_media({"post_id": "123", "url": "https://x.com/i/web/status/123", "_use_cookies": False}, item, root / "output")
                self.assertEqual(report["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
                self.assertEqual(report["fps"], 60)
                self.assertFalse(report["original_verified"])
                self.assertTrue((root / "output" / (report["filename"] + ".json")).is_file())
            finally:
                server.shutdown()
                server.server_close()

    def test_mixed_post_selects_1080p60_without_cap(self):
        class Extractor:
            def _extract_variant_formats(self, variant, post_id):
                return variant["fixture"], {}
        formats = [
            {"format_id": "720", "url": "https://video.twimg.com/720.mp4", "ext": "mp4", "width": 1280, "height": 720, "fps": 60, "tbr": 1500, "vcodec": "h264", "acodec": "aac"},
            {"format_id": "1080-30", "url": "https://video.twimg.com/1080.mp4", "ext": "mp4", "width": 1920, "height": 1080, "fps": 30, "tbr": 8000, "vcodec": "h264", "acodec": "aac"},
            {"format_id": "1080-60", "url": "https://video.twimg.com/60.mp4", "ext": "mp4", "width": 1920, "height": 1080, "fps": 60, "tbr": 6000, "vcodec": "h264", "acodec": "aac"}]
        status = {"extended_entities": {"media": [
            {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/test.jpg", "original_info": {"width": 4000, "height": 3000}},
            {"type": "video", "original_info": {"width": 1920, "height": 1080}, "video_info": {"variants": [{"url": "https://video.twimg.com/test.mp4", "fixture": formats}]}}
        ]}}
        with dl.youtube_dl(False, format="bestvideo*+bestaudio/best") as ydl:
            media = dl.build_media(status, "123", Extractor(), ydl)
        self.assertEqual([m["kind"] for m in media], ["image", "video"])
        self.assertEqual(media[1]["selected_format"], "1080-60")
        self.assertEqual(media[1]["fps"], 60)
        self.assertFalse(media[1]["original_verified"])
        self.assertNotIn("_info", dl.public_media(media[1]))

    def test_preserves_separate_audio_selection(self):
        class Extractor:
            def _extract_variant_formats(self, variant, post_id):
                return [
                    {"format_id": "v", "url": "https://video.twimg.com/v.mp4", "ext": "mp4", "height": 2160, "width": 3840, "fps": 60, "vcodec": "h264", "acodec": "none"},
                    {"format_id": "a", "url": "https://video.twimg.com/a.m4a", "ext": "m4a", "vcodec": "none", "acodec": "aac", "tbr": 128}], {}
        status = {"extended_entities": {"media": [{"type": "video", "video_info": {"variants": [{"url": "https://video.twimg.com/master.m3u8"}]}}]}}
        with dl.youtube_dl(False, format="bestvideo*+bestaudio/best", merge_output_format="mp4") as ydl:
            item = dl.build_media(status, "123", Extractor(), ydl)[0]
        self.assertEqual(item["height"], 2160)
        self.assertEqual(item["selected_format"], "v+a")

    def test_video_probe_reads_actual_frame_rate(self):
        fake = type("Result", (), {"stderr": "Stream #0:0: Video: h264 (High), yuv420p, 1920x1080, 60 fps, 60 tbr\n"})()
        with patch.object(dl.subprocess, "run", return_value=fake):
            result = dl.probe_video(Path("video.mp4"))
        self.assertEqual((result["width"], result["height"], result["fps"]), (1920, 1080, 60.0))
