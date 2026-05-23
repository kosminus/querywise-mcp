"""Async SQLite engine + session factory for the metadata store.

On every new connection we enable WAL (so background reads don't block the
single writer) and best-effort load the sqlite-vec extension for SQL-level
vector distance. If the extension can't load, vector search transparently
falls back to in-process cosine (see ``db.vectors``).
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.util import await_only

from querywise_mcp import db as _db_pkg  # noqa: F401  (ensure package import)
from querywise_mcp.config import settings
from querywise_mcp.db import vectors

logger = logging.getLogger(__name__)

engine = create_async_engine(
    settings.resolved_database_url(),
    echo=settings.debug,
)


@event.listens_for(engine.sync_engine, "connect")
def _configure_connection(dbapi_conn, _record):
    """Configure each new aiosqlite connection.

    Runs inside SQLAlchemy's greenlet during connect, so the adapter's sync
    cursor works for PRAGMAs and ``await_only`` can drive aiosqlite's async
    extension-loading (which dispatches to its worker thread). If sqlite-vec
    can't load, vector search falls back to in-process cosine.
    """
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
    finally:
        cur.close()

    try:
        import sqlite_vec

        aioconn = dbapi_conn.driver_connection  # aiosqlite.Connection
        await_only(aioconn.enable_load_extension(True))
        await_only(aioconn.load_extension(sqlite_vec.loadable_path()))
        await_only(aioconn.enable_load_extension(False))
        vectors.vec_enabled = True
    except Exception as e:  # pragma: no cover - environment dependent
        if not getattr(_configure_connection, "_warned", False):
            logger.info(
                "sqlite-vec not loaded (%s); using in-process vector search.", e
            )
            _configure_connection._warned = True


async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Transactional session scope for a single tool/CLI invocation."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Compatibility shim for code expecting a session generator."""
    async with session_scope() as session:
        yield session


async def healthcheck() -> bool:
    async with async_session_factory() as session:
        await session.execute(text("SELECT 1"))
    return True
