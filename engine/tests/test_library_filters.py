"""Library filters must count and paginate the full filtered collection."""
from datetime import datetime
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import models
from app.api.deps import get_db
from app.api.routers import downloads
from app.core.database import Base


class LibraryFiltersTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        user = models.User(state_key="@library-test", username="library-test", nickname="Test Creator")
        self.db.add(user)
        self.db.flush()
        self.user_id = user.id
        for index, (title, source, kind, timestamp) in enumerate([
            ("Before", "manual", "video", "2026-10-28T23:59:59"),
            ("Alpha", "manual", "video", "2026-10-29T00:00:00"),
            ("Zulu", "manual", "image", "2026-10-30T23:59:59.999999"),
            ("Telegram", "telegram", "image", "2026-10-30T12:00:00"),
            ("After", "manual", "video", "2026-10-31T00:00:00"),
        ]):
            self.db.add(models.Download(user_id=user.id, video_id=str(index), title=title,
                                       source=source, post_type=kind, created_at=datetime.fromisoformat(timestamp)))
        self.db.commit()
        app = FastAPI()
        app.include_router(downloads.router)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_filtered_total_and_pagination_include_both_calendar_boundaries(self):
        path = "/api/downloads?source=manual&date_from=2026-10-29&date_to=2026-10-30&page_size=1"
        first = self.client.get(path).json()
        second = self.client.get(path + "&page=2").json()
        self.assertEqual((first["total"], first["pages"]), (2, 2))
        self.assertEqual([first["items"][0]["title"], second["items"][0]["title"]], ["Zulu", "Alpha"])

    def test_source_type_and_creator_search_combine(self):
        result = self.client.get("/api/downloads?source=manual&post_type=image&search=Test%20Creator").json()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "Zulu")

    def test_name_sort_and_unfiltered_listing_still_work(self):
        result = self.client.get("/api/downloads?sort_by=title&sort_dir=asc").json()
        self.assertEqual(result["total"], 5)
        self.assertEqual([item["title"] for item in result["items"]], ["After", "Alpha", "Before", "Telegram", "Zulu"])

    def test_invalid_and_reversed_calendar_ranges_are_rejected(self):
        for query in ("date_from=invalid", "date_from=2026-10-31&date_to=2026-10-29"):
            self.assertEqual(self.client.get("/api/downloads?" + query).status_code, 422)

    def add_source(self, source, title, kind="video"):
        self.db.add(models.Download(user_id=self.user_id, video_id="source-" + source,
                                   title=title, source=source, post_type=kind,
                                   created_at=datetime.fromisoformat("2026-10-30T12:00:00")))
        self.db.commit()

    def test_four_source_groups_partition_all_downloads(self):
        for source in ("monitor", "catchup", "story", "bulk", "import", "instagram", "custom"):
            self.add_source(source, source)
        seen = set()
        for group, expected in (("manual", 8), ("monitor", 2), ("story", 1), ("telegram", 1)):
            response = self.client.get("/api/downloads", params={"source_group": group}).json()
            self.assertEqual(response["total"], expected)
            identifiers = {item["id"] for item in response["items"]}
            self.assertFalse(seen & identifiers)
            seen |= identifiers
        self.assertEqual(len(seen), self.client.get("/api/downloads").json()["total"])

    def test_monitoring_group_combines_filters_and_paginates_catchup(self):
        self.add_source("monitor", "Scheduled")
        self.add_source("catchup", "Catch-up", "image")
        query = {"source_group": "monitor", "search": "Test Creator", "date_from": "2026-10-29",
                 "date_to": "2026-10-30", "page_size": 1, "sort_by": "title", "sort_dir": "asc"}
        first = self.client.get("/api/downloads", params=query).json()
        second = self.client.get("/api/downloads", params={**query, "page": 2}).json()
        self.assertEqual((first["total"], first["pages"]), (2, 2))
        self.assertEqual([first["items"][0]["title"], second["items"][0]["title"]], ["Catch-up", "Scheduled"])
        image = self.client.get("/api/downloads", params={**query, "post_type": "image"}).json()
        self.assertEqual((image["total"], image["items"][0]["source"]), (1, "catchup"))

    def test_exact_source_filters_remain_backward_compatible(self):
        self.add_source("catchup", "Catch-up")
        self.add_source("bulk", "Bulk")
        self.assertEqual(self.client.get("/api/downloads?source=manual").json()["total"], 4)
        self.assertEqual(self.client.get("/api/downloads?source=catchup").json()["total"], 1)
        self.assertEqual(self.client.get("/api/downloads?source_group=manual&source=bulk").json()["total"], 1)
        self.assertEqual(self.client.get("/api/downloads?source_group=monitor&source=manual").json()["total"], 0)

    def test_unknown_source_groups_are_rejected(self):
        self.assertEqual(self.client.get("/api/downloads?source_group=tiktok").status_code, 422)


if __name__ == "__main__":
    unittest.main()
