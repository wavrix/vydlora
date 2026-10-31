"""Database engine, session factory, and FastAPI dependency."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

_is_sqlite = settings.database_url.startswith("sqlite")

# The download worker pool holds one DB connection per busy worker for the whole
# download, and the scheduler, API requests and WS broadcasts also need
# connections. The default pool (size 5 + overflow 10 = 15) is exhausted as soon
# as the worker count climbs (e.g. 20), raising QueuePool TimeoutError. Size the
# pool to comfortably exceed the worker-count cap (64) plus overhead. Overflow
# connections are created on demand for bursts and discarded when idle, so this
# doesn't keep dozens of connections open at rest.
_POOL_SIZE = max(10, settings.download_workers)
_MAX_OVERFLOW = 80

engine = create_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=_POOL_SIZE,
    max_overflow=_MAX_OVERFLOW,
    pool_timeout=30,
    pool_recycle=1800,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def get_db() -> Iterator[Session]:
    """FastAPI dependency - yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context-managed session for workers/services (auto commit/rollback)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """Create all tables and apply lightweight SQLite tuning."""
    from .. import models  # noqa: F401  (ensure models are imported/registered)

    if _is_sqlite:
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _rec):  # pragma: no cover
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA synchronous=NORMAL")
            # With many concurrent workers, WAL still allows only ONE writer at a
            # time; busy_timeout makes writers wait instead of raising
            # "database is locked".
            cur.execute("PRAGMA busy_timeout=10000")
            cur.close()

    Base.metadata.create_all(bind=engine)

    # Lightweight, idempotent column migrations (create_all never ALTERs an
    # existing table). SQLite supports ADD COLUMN; guard on the column being
    # absent so this is a no-op on already-migrated databases.
    if _is_sqlite:
        _add_column_if_missing("users", "monitored", "BOOLEAN NOT NULL DEFAULT 1")
        _add_column_if_missing("users", "scan_failures", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing("users", "platform", "VARCHAR(16) NOT NULL DEFAULT 'tiktok'")
        _add_column_if_missing("users", "sec_uid", "VARCHAR(120)")
        _add_column_if_missing("users", "platform_uid", "VARCHAR(64)")
        _add_column_if_missing("posts", "is_story", "BOOLEAN NOT NULL DEFAULT 0")
        # Who asked for it. `source` only ever said "telegram", so a download
        # could not be attributed to the person who requested it.
        _add_column_if_missing("queue_jobs", "telegram_chat_id", "BIGINT")
        _add_column_if_missing("downloads", "telegram_chat_id", "BIGINT")
        # Media stored in MEGA rather than on this machine.
        _add_column_if_missing("downloads", "remote_path", "TEXT")

    # Composite indexes backing the hot per-user count/lookup queries (the
    # users/downloads listings aggregate by user_id + status on every page).
    # IF NOT EXISTS keeps this idempotent on both fresh and existing databases.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_downloads_user_status ON downloads (user_id, status)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_queue_user_status ON queue_jobs (user_id, status)"
        )

        # Data recovery: an earlier bug soft-deleted (status='deleted') any
        # account whose scan transiently failed - a rate-limited / WAF-challenged
        # live account looks "not found" under load - which removed it from the
        # monitor list. User-initiated deletes are HARD deletes (the row is
        # removed), so any surviving 'deleted' row is a bug casualty. Restore it
        # to monitoring. Idempotent and a no-op once there are none left; the
        # scan path no longer produces 'deleted' rows.
        restored = conn.exec_driver_sql(
            "UPDATE users SET status='monitoring', last_error=NULL WHERE status='deleted'"
        ).rowcount
        if restored:
            from .logging_config import get_logger

            get_logger("main").warning(
                "Restored %d creator(s) that a prior scan-failure bug had soft-deleted", restored
            )


def _add_column_if_missing(table: str, column: str, ddl_type: str) -> None:
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
