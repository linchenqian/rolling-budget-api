from __future__ import annotations

import os
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from functools import lru_cache
from sqlite3 import Connection as SQLiteConnection

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

DEFAULT_DATABASE_URL = "sqlite:////data/budget.db"


def _resolve_database_url(database_url: str | None = None) -> str:
    return database_url or os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL


@lru_cache(maxsize=8)
def _create_engine(database_url: str) -> Engine:
    if make_url(database_url).get_backend_name() == "sqlite":
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False, "timeout": 30},
            poolclass=QueuePool,
            pool_size=1,
            max_overflow=0,
            pool_timeout=30,
            pool_pre_ping=True,
        )

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
            if not isinstance(dbapi_connection, SQLiteConnection):
                return
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=DELETE")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA secure_delete=ON")
            cursor.execute("PRAGMA trusted_schema=OFF")
            cursor.close()

        return engine

    return create_engine(database_url, pool_pre_ping=True, pool_recycle=1_800)


def get_engine(database_url: str | None = None) -> Engine:
    """Return a cached SQLAlchemy engine without resolving secrets at import time."""

    return _create_engine(_resolve_database_url(database_url))


def begin_write_transaction(session: Session) -> None:
    """Reserve SQLite's single writer before a service performs its first read."""

    bind = session.get_bind()
    if bind.dialect.name == "sqlite" and not session.in_transaction():
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


@lru_cache(maxsize=8)
def _create_session_factory(database_url: str) -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(database_url),
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )


def get_session_factory(
    database_url: str | None = None,
) -> sessionmaker[Session]:
    return _create_session_factory(_resolve_database_url(database_url))


def SessionLocal() -> Session:
    """Compatibility factory for code that expects the usual SessionLocal API."""

    return get_session_factory()()


@contextmanager
def session_scope(database_url: str | None = None) -> Iterator[Session]:
    session = get_session_factory(database_url)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency. Endpoint/service code owns commit boundaries."""

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
