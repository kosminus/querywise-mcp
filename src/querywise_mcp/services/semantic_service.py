"""CRUD for the semantic layer (glossary, metrics, dictionary, sample queries).

Each create embeds the new item inline so vector search works immediately.
Embedding is best-effort: if no embedding provider is configured the item is
still stored and keyword matching covers it.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from querywise_mcp.db.models.dictionary import DictionaryEntry
from querywise_mcp.db.models.glossary import GlossaryTerm
from querywise_mcp.db.models.knowledge import KnowledgeDocument
from querywise_mcp.db.models.metric import MetricDefinition
from querywise_mcp.db.models.sample_query import SampleQuery
from querywise_mcp.db.models.schema_cache import CachedColumn, CachedTable
from querywise_mcp.services import embedding_service

logger = logging.getLogger(__name__)


async def _safe_embed(coro):
    try:
        return await coro
    except Exception:
        logger.warning("Inline embedding failed; keyword matching will be used.")
        return None


# --- Glossary ---------------------------------------------------------------
async def list_glossary(db: AsyncSession, connection_id: uuid.UUID) -> list[GlossaryTerm]:
    res = await db.execute(
        select(GlossaryTerm).where(GlossaryTerm.connection_id == connection_id)
    )
    return list(res.scalars().all())


async def create_glossary(
    db: AsyncSession,
    connection_id: uuid.UUID,
    term: str,
    definition: str,
    sql_expression: str,
    related_tables: list[str] | None = None,
    related_columns: list[str] | None = None,
) -> GlossaryTerm:
    obj = GlossaryTerm(
        connection_id=connection_id,
        term=term,
        definition=definition,
        sql_expression=sql_expression,
        related_tables=related_tables,
        related_columns=related_columns,
    )
    db.add(obj)
    await db.flush()
    obj.term_embedding = await _safe_embed(embedding_service.embed_glossary_term(obj))
    await db.flush()
    return obj


async def delete_glossary(db: AsyncSession, term_id: uuid.UUID) -> bool:
    obj = await db.get(GlossaryTerm, term_id)
    if not obj:
        return False
    await db.delete(obj)
    await db.flush()
    return True


# --- Metrics ----------------------------------------------------------------
async def list_metrics(db: AsyncSession, connection_id: uuid.UUID) -> list[MetricDefinition]:
    res = await db.execute(
        select(MetricDefinition).where(MetricDefinition.connection_id == connection_id)
    )
    return list(res.scalars().all())


async def create_metric(
    db: AsyncSession,
    connection_id: uuid.UUID,
    metric_name: str,
    display_name: str,
    sql_expression: str,
    description: str | None = None,
    related_tables: list[str] | None = None,
    dimensions: list[str] | None = None,
) -> MetricDefinition:
    obj = MetricDefinition(
        connection_id=connection_id,
        metric_name=metric_name,
        display_name=display_name,
        sql_expression=sql_expression,
        description=description,
        related_tables=related_tables,
        dimensions=dimensions,
    )
    db.add(obj)
    await db.flush()
    obj.metric_embedding = await _safe_embed(embedding_service.embed_metric(obj))
    await db.flush()
    return obj


async def delete_metric(db: AsyncSession, metric_id: uuid.UUID) -> bool:
    obj = await db.get(MetricDefinition, metric_id)
    if not obj:
        return False
    await db.delete(obj)
    await db.flush()
    return True


# --- Sample queries ---------------------------------------------------------
async def list_sample_queries(
    db: AsyncSession, connection_id: uuid.UUID
) -> list[SampleQuery]:
    res = await db.execute(
        select(SampleQuery).where(SampleQuery.connection_id == connection_id)
    )
    return list(res.scalars().all())


async def create_sample_query(
    db: AsyncSession,
    connection_id: uuid.UUID,
    natural_language: str,
    sql_query: str,
    description: str | None = None,
    is_validated: bool = True,
) -> SampleQuery:
    obj = SampleQuery(
        connection_id=connection_id,
        natural_language=natural_language,
        sql_query=sql_query,
        description=description,
        is_validated=is_validated,
    )
    db.add(obj)
    await db.flush()
    obj.question_embedding = await _safe_embed(embedding_service.embed_sample_query(obj))
    await db.flush()
    return obj


async def delete_sample_query(db: AsyncSession, sq_id: uuid.UUID) -> bool:
    obj = await db.get(SampleQuery, sq_id)
    if not obj:
        return False
    await db.delete(obj)
    await db.flush()
    return True


# --- Dictionary -------------------------------------------------------------
async def resolve_column_id(
    db: AsyncSession, connection_id: uuid.UUID, table_name: str, column_name: str
) -> uuid.UUID | None:
    res = await db.execute(
        select(CachedColumn.id)
        .join(CachedTable, CachedColumn.table_id == CachedTable.id)
        .where(
            CachedTable.connection_id == connection_id,
            CachedTable.table_name == table_name,
            CachedColumn.column_name == column_name,
        )
    )
    return res.scalar_one_or_none()


async def list_dictionary(db: AsyncSession, column_id: uuid.UUID) -> list[DictionaryEntry]:
    res = await db.execute(
        select(DictionaryEntry)
        .where(DictionaryEntry.column_id == column_id)
        .order_by(DictionaryEntry.sort_order)
    )
    return list(res.scalars().all())


async def create_dictionary_entry(
    db: AsyncSession,
    column_id: uuid.UUID,
    raw_value: str,
    display_value: str,
    description: str | None = None,
) -> DictionaryEntry:
    obj = DictionaryEntry(
        column_id=column_id,
        raw_value=raw_value,
        display_value=display_value,
        description=description,
    )
    db.add(obj)
    await db.flush()
    return obj


# --- Knowledge --------------------------------------------------------------
async def list_knowledge(
    db: AsyncSession, connection_id: uuid.UUID
) -> list[KnowledgeDocument]:
    res = await db.execute(
        select(KnowledgeDocument).where(
            KnowledgeDocument.connection_id == connection_id
        )
    )
    return list(res.scalars().all())


async def delete_knowledge(db: AsyncSession, doc_id: uuid.UUID) -> bool:
    obj = await db.get(KnowledgeDocument, doc_id)
    if not obj:
        return False
    await db.delete(obj)
    await db.flush()
    return True
