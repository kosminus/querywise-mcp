"""Maps NL terms to relevant tables and columns using hybrid search."""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from querywise_mcp.config import settings
from querywise_mcp.db.models.schema_cache import CachedColumn, CachedRelationship, CachedTable
from querywise_mcp.db.vectors import knn
from querywise_mcp.semantic.relevance_scorer import (
    ScoredItem,
    extract_keywords,
    keyword_match_score,
)

logger = logging.getLogger(__name__)


@dataclass
class LinkedTable:
    table: CachedTable
    columns: list[CachedColumn]
    score: float
    match_reason: str  # "embedding" | "keyword" | "relationship"


async def find_relevant_tables(
    db: AsyncSession,
    connection_id: uuid.UUID,
    question_embedding: list[float] | None,
    question: str,
    max_tables: int | None = None,
) -> list[LinkedTable]:
    """Find the most relevant tables for a question using hybrid search.

    Combines:
    1. Vector similarity search on table description embeddings (when available)
    2. Keyword matching on table names
    3. FK relationship expansion
    """
    if max_tables is None:
        max_tables = settings.max_context_tables

    keywords = extract_keywords(question)

    # Stage 1: Embedding similarity search (skip if no embedding available)
    embedding_results: list[tuple[CachedTable, float]] = []
    if question_embedding is not None:
        embedding_results = await _vector_search_tables(
            db, connection_id, question_embedding, limit=15
        )

    # Stage 2: Keyword search
    keyword_results = await _keyword_search_tables(db, connection_id, keywords)

    # Merge candidates and score
    scored: dict[str, ScoredItem] = {}

    for table, similarity in embedding_results:
        key = str(table.id)
        if key not in scored:
            scored[key] = ScoredItem(id=key, name=table.table_name)
        scored[key].embedding_score = similarity

    for table in keyword_results:
        key = str(table.id)
        if key not in scored:
            scored[key] = ScoredItem(id=key, name=table.table_name)
        scored[key].keyword_score = keyword_match_score(table.table_name, keywords)

    # Stage 3: Relationship expansion — boost tables connected via FK to high-scoring tables
    top_table_ids = [
        uuid.UUID(s.id) for s in sorted(scored.values(), key=lambda s: s.final_score, reverse=True)[:5]
    ]
    related_tables = await _get_related_tables(db, connection_id, top_table_ids)
    for table in related_tables:
        key = str(table.id)
        if key not in scored:
            scored[key] = ScoredItem(id=key, name=table.table_name)
        scored[key].relationship_score = 1.0

    # Sort by final score, take top N
    sorted_items = sorted(scored.values(), key=lambda s: s.final_score, reverse=True)
    top_items = sorted_items[:max_tables]

    # Load full table data with columns
    results: list[LinkedTable] = []
    for item in top_items:
        table_id = uuid.UUID(item.id)
        table = await db.get(CachedTable, table_id)
        if not table:
            continue

        # Load columns
        col_result = await db.execute(
            select(CachedColumn)
            .where(CachedColumn.table_id == table_id)
            .order_by(CachedColumn.ordinal_position)
        )
        columns = list(col_result.scalars().all())

        reason = "embedding"
        if item.keyword_score > 0:
            reason = "keyword"
        elif item.relationship_score > 0:
            reason = "relationship"

        results.append(LinkedTable(
            table=table,
            columns=columns,
            score=item.final_score,
            match_reason=reason,
        ))

    return results


async def _vector_search_tables(
    db: AsyncSession,
    connection_id: uuid.UUID,
    embedding: list[float],
    limit: int = 15,
) -> list[tuple[CachedTable, float]]:
    """Find tables by embedding similarity (cosine, higher = closer)."""
    try:
        return await knn(
            db,
            CachedTable,
            "description_embedding",
            embedding,
            limit,
            CachedTable.connection_id == connection_id,
        )
    except Exception:
        logger.warning(
            "Vector search failed (possible dimension mismatch). "
            "Check EMBEDDING_DIMENSION matches your model. Falling back to keyword search.",
            exc_info=True,
        )
        await db.rollback()
        return []


async def _keyword_search_tables(
    db: AsyncSession,
    connection_id: uuid.UUID,
    keywords: list[str],
) -> list[CachedTable]:
    """Find tables whose names match any keyword."""
    if not keywords:
        return []

    # Build ILIKE conditions for each keyword
    conditions = []
    for kw in keywords:
        conditions.append(CachedTable.table_name.ilike(f"%{kw}%"))

    from sqlalchemy import or_

    stmt = (
        select(CachedTable)
        .where(CachedTable.connection_id == connection_id, or_(*conditions))
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _get_related_tables(
    db: AsyncSession,
    connection_id: uuid.UUID,
    table_ids: list[uuid.UUID],
) -> list[CachedTable]:
    """Find tables connected via FK to the given tables."""
    if not table_ids:
        return []

    from sqlalchemy import or_

    # Find relationships where source or target is in our table set
    rel_result = await db.execute(
        select(CachedRelationship).where(
            CachedRelationship.connection_id == connection_id,
            or_(
                CachedRelationship.source_table_id.in_(table_ids),
                CachedRelationship.target_table_id.in_(table_ids),
            ),
        )
    )
    relationships = rel_result.scalars().all()

    # Collect IDs of related tables not already in our set
    related_ids = set()
    for rel in relationships:
        if rel.source_table_id not in table_ids:
            related_ids.add(rel.source_table_id)
        if rel.target_table_id not in table_ids:
            related_ids.add(rel.target_table_id)

    if not related_ids:
        return []

    result = await db.execute(
        select(CachedTable).where(CachedTable.id.in_(related_ids))
    )
    return list(result.scalars().all())
