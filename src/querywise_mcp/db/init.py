"""Schema bootstrap + embedding-dimension management for the SQLite store.

Replaces Alembic: for an embedded single-file DB we just ``create_all`` on
startup (idempotent) and track the embedding dimension in a small meta table so
that switching embedding providers (e.g. OpenAI 1536 -> Ollama 768) clears now
-incompatible vectors instead of corrupting distance math.
"""

import logging

from sqlalchemy import text

from querywise_mcp.config import settings
from querywise_mcp.db.base import Base
from querywise_mcp.db.models import (  # noqa: F401  (register all tables)
    DatabaseConnection,
)
from querywise_mcp.db.session import engine

logger = logging.getLogger(__name__)

EMBEDDING_COLUMNS = [
    ("cached_tables", "description_embedding"),
    ("cached_columns", "description_embedding"),
    ("glossary_terms", "term_embedding"),
    ("metric_definitions", "metric_embedding"),
    ("sample_queries", "question_embedding"),
    ("knowledge_chunks", "chunk_embedding"),
]


async def init_db() -> None:
    """Create tables if missing and reconcile the embedding dimension."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS qw_meta "
                "(key TEXT PRIMARY KEY, value TEXT)"
            )
        )
    await ensure_embedding_dimensions()


async def ensure_embedding_dimensions() -> None:
    """If the configured embedding dimension changed, clear stale vectors.

    Because vectors are stored as raw float32 BLOBs, a dimension change makes
    existing embeddings incomparable. We null them so they regenerate lazily
    with the new provider, and record the new dimension.
    """
    target = settings.embedding_dimension
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT value FROM qw_meta WHERE key = 'embedding_dimension'")
            )
        ).scalar_one_or_none()

        current = int(row) if row is not None else None
        if current == target:
            return

        if current is not None:
            logger.info(
                "Embedding dimension changed (%s -> %s); clearing stale vectors.",
                current,
                target,
            )
            for table, column in EMBEDDING_COLUMNS:
                await conn.execute(
                    text(f"UPDATE {table} SET {column} = NULL WHERE {column} IS NOT NULL")
                )

        await conn.execute(
            text(
                "INSERT INTO qw_meta (key, value) VALUES ('embedding_dimension', :v) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            ),
            {"v": str(target)},
        )
