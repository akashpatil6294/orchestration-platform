"""Database engine, session factory and FastAPI dependency.

The platform targets PostgreSQL as the durable shared store and keeps SQLite
available for zero-setup local development. Both are driven through the same
SQLAlchemy 2.x ORM layer; anything dialect-specific is isolated here.
"""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


def build_engine(database_url: str | None = None) -> Engine:
    url = database_url or settings.database_url
    kwargs: dict[str, object] = {"pool_pre_ping": True, "echo": settings.sql_echo, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        # A file-backed SQLite database is shared between the API, the scheduler
        # and the worker poller, so allow cross-thread use and enable WAL.
        kwargs["poolclass"] = None  # type: ignore[assignment]
        engine = create_engine(url, **kwargs)  # type: ignore[arg-type]

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - driver hook
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

        return engine

    kwargs["pool_size"] = settings.db_pool_size
    kwargs["max_overflow"] = settings.db_max_overflow
    kwargs["pool_recycle"] = settings.db_pool_recycle_seconds
    return create_engine(url, **kwargs)  # type: ignore[arg-type]


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, class_=Session)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Context manager for background tasks (scheduler, outbox relay)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_database() -> tuple[bool, str | None]:
    """Readiness probe. Returns ``(ok, error_message)``."""
    try:
        with engine.connect() as connection:
            connection.execute(select(1))
        return True, None
    except Exception as exc:  # pragma: no cover - depends on environment
        return False, f"{type(exc).__name__}: {exc}"


def database_backend() -> str:
    return "sqlite" if engine.dialect.name == "sqlite" else "postgresql"


def advisory_lock(db: Session, key: int, *, wait: bool = False) -> bool:
    """Take a cross-process lock where the dialect supports one.

    PostgreSQL uses ``pg_try_advisory_xact_lock``; SQLite is a single-writer
    database so callers already serialise through the write lock.
    """
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        query = "SELECT pg_try_advisory_xact_lock(:key)" if not wait else "SELECT pg_advisory_xact_lock(:key)"
        result = db.execute(text(query), {"key": key}).scalar()
        return bool(result) if not wait else True
    return True


__all__ = [
    "SessionLocal",
    "advisory_lock",
    "build_engine",
    "check_database",
    "database_backend",
    "engine",
    "get_db",
    "session_scope",
]
