"""Profile and wire compatibility for the self-contained Lora 1 engine."""
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.core.compat import LEGACY_DATABASE, apply_environment_aliases, upgrade_profile_database
from app.core.engine_security import wrap


class EngineMigrationTests(unittest.TestCase):
    def test_database_backup_preserves_source_and_committed_wal(self):
        base = Path(__file__).resolve().parents[1] / "build"
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as directory:
            root = Path(directory).resolve()
            old = root / LEGACY_DATABASE
            with closing(sqlite3.connect(old)) as source:
                source.execute("PRAGMA journal_mode=WAL")
                source.execute("CREATE TABLE saved (value TEXT)")
                source.execute("INSERT INTO saved VALUES ('kept')")
                source.commit()
                current = upgrade_profile_database(root)
                self.assertTrue(old.exists())
                with closing(sqlite3.connect(current)) as copied:
                    self.assertEqual(copied.execute("SELECT value FROM saved").fetchall(), [("kept",)])
                source.execute("INSERT INTO saved VALUES ('later')")
                source.commit()
                self.assertEqual(upgrade_profile_database(root), current)
                with closing(sqlite3.connect(current)) as copied:
                    self.assertEqual(copied.execute("SELECT COUNT(*) FROM saved").fetchone()[0], 1)

    def test_canonical_environment_values_take_priority(self):
        with patch.dict(os.environ, {"LORA1_PORT": "8100", "SOLSTICE_PORT": "8200"}):
            apply_environment_aliases()
            self.assertEqual(os.environ["LORA1_PORT"], "8100")


class EngineGuardTests(unittest.TestCase):
    def test_canonical_and_historical_headers_are_authenticated(self):
        app = FastAPI()
        @app.get("/api/ping")
        def ping():
            return {"ok": True, "app": "Lora 1"}
        client = TestClient(wrap(app, "private-test-token", lambda: None))
        try:
            self.assertEqual(client.get("/api/ping").status_code, 401)
            self.assertEqual(client.get("/api/ping", headers={"X-Lora1-Token": "wrong"}).status_code, 401)
            for header in ("X-Lora1-Token", "X-Solstice-Token"):
                result = client.get("/api/ping", headers={header: "private-test-token"})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["app"], "Lora 1")
        finally:
            client.close()
