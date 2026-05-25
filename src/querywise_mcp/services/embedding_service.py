import hashlib
import logging
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from querywise_mcp.db.models.glossary import GlossaryTerm
from querywise_mcp.db.models.knowledge import KnowledgeChunk, KnowledgeDocument
from querywise_mcp.db.models.metric import MetricDefinition
from querywise_mcp.db.models.sample_query import SampleQuery
from querywise_mcp.db.models.schema_cache import CachedColumn, CachedTable
from querywise_mcp.llm.provider_registry import get_embedding_provider

logger = logging.getLogger(__name__)

_provider = None
_provider_error: str | None = None
_warned_unavailable = False


class EmbeddingsUnavailableError(RuntimeError):
    """No embedding provider could be initialized (e.g. missing API key)."""


def _get_provider():
    """Return the cached embedding provider, or raise EmbeddingsUnavailableError.

    The failure is cached so we don't repeatedly try (and fail) to construct a
    provider — which previously produced a traceback per item embedded.
    """
    global _provider, _provider_error
    if _provider is not None:
        return _provider
    if _provider_error is not None:
        raise EmbeddingsUnavailableError(_provider_error)
    try:
        _provider = get_embedding_provider()
        return _provider
    except Exception as e:
        _provider_error = str(e)
        raise EmbeddingsUnavailableError(_provider_error) from e


def embeddings_available() -> bool:
    """Whether embeddings can be generated. Logs a one-time how-to-fix hint.

    When False, callers should skip embedding and rely on keyword search.
    """
    global _warned_unavailable
    try:
        _get_provider()
        return True
    except EmbeddingsUnavailableError as e:
        if not _warned_unavailable:
            logger.warning(
                "Embeddings disabled (%s). Using keyword-only search. To enable, set "
                "OPENAI_API_KEY, or DEFAULT_LLM_PROVIDER=ollama for local embeddings.",
                e,
            )
            _warned_unavailable = True
        return False


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def embed_text(text: str) -> list[float]:
    """Generate an embedding vector for the given text."""
    provider = _get_provider()
    return await provider.generate_embedding(text)


async def embed_table(table: CachedTable) -> list[float]:
    """Generate an embedding for a table's description."""
    text = f"{table.schema_name}.{table.table_name}"
    if table.comment:
        text += f": {table.comment}"
    return await embed_text(text)


async def embed_column(column: CachedColumn, table_name: str) -> list[float]:
    """Generate an embedding for a column's description."""
    text = f"{table_name}.{column.column_name} ({column.data_type})"
    if column.comment:
        text += f": {column.comment}"
    return await embed_text(text)


async def embed_glossary_term(term: GlossaryTerm) -> list[float]:
    """Generate an embedding for a glossary term."""
    text = f"{term.term}: {term.definition}"
    return await embed_text(text)


async def embed_metric(metric: MetricDefinition) -> list[float]:
    """Generate an embedding for a metric definition."""
    text = f"{metric.display_name}"
    if metric.description:
        text += f": {metric.description}"
    return await embed_text(text)


async def embed_sample_query(query: SampleQuery) -> list[float]:
    """Generate an embedding for a sample query's NL question."""
    return await embed_text(query.natural_language)


async def embed_knowledge_chunk(chunk: KnowledgeChunk) -> list[float]:
    """Generate an embedding for a knowledge chunk."""
    return await embed_text(chunk.content)


async def count_items_needing_embeddings(
    db: AsyncSession, connection_id
) -> int:
    """Count all metadata items that need embedding generation."""
    total = 0

    result = await db.execute(
        select(func.count()).select_from(CachedTable).where(
            CachedTable.connection_id == connection_id,
            CachedTable.description_embedding.is_(None),
        )
    )
    total += result.scalar_one()

    result = await db.execute(
        select(func.count())
        .select_from(CachedColumn)
        .join(CachedTable, CachedColumn.table_id == CachedTable.id)
        .where(
            CachedTable.connection_id == connection_id,
            CachedColumn.description_embedding.is_(None),
        )
    )
    total += result.scalar_one()

    result = await db.execute(
        select(func.count()).select_from(GlossaryTerm).where(
            GlossaryTerm.connection_id == connection_id,
            GlossaryTerm.term_embedding.is_(None),
        )
    )
    total += result.scalar_one()

    result = await db.execute(
        select(func.count()).select_from(MetricDefinition).where(
            MetricDefinition.connection_id == connection_id,
            MetricDefinition.metric_embedding.is_(None),
        )
    )
    total += result.scalar_one()

    result = await db.execute(
        select(func.count()).select_from(SampleQuery).where(
            SampleQuery.connection_id == connection_id,
            SampleQuery.question_embedding.is_(None),
        )
    )
    total += result.scalar_one()

    result = await db.execute(
        select(func.count())
        .select_from(KnowledgeChunk)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(
            KnowledgeDocument.connection_id == connection_id,
            KnowledgeChunk.chunk_embedding.is_(None),
        )
    )
    total += result.scalar_one()

    return total


async def generate_embeddings_for_connection(
    db: AsyncSession,
    connection_id,
    on_progress: Callable[[], None] | None = None,
) -> int:
    """Generate embeddings for all metadata of a connection. Returns count of items embedded."""
    count = 0

    # Tables
    result = await db.execute(
        select(CachedTable).where(
            CachedTable.connection_id == connection_id,
            CachedTable.description_embedding.is_(None),
        )
    )
    tables = result.scalars().all()
    for table in tables:
        table.description_embedding = await embed_table(table)
        count += 1
        if on_progress:
            on_progress()

        # Columns of this table
        col_result = await db.execute(
            select(CachedColumn).where(
                CachedColumn.table_id == table.id,
                CachedColumn.description_embedding.is_(None),
            )
        )
        columns = col_result.scalars().all()
        for col in columns:
            col.description_embedding = await embed_column(col, table.table_name)
            count += 1
            if on_progress:
                on_progress()

    # Glossary terms
    result = await db.execute(
        select(GlossaryTerm).where(
            GlossaryTerm.connection_id == connection_id,
            GlossaryTerm.term_embedding.is_(None),
        )
    )
    for term in result.scalars().all():
        term.term_embedding = await embed_glossary_term(term)
        count += 1
        if on_progress:
            on_progress()

    # Metrics
    result = await db.execute(
        select(MetricDefinition).where(
            MetricDefinition.connection_id == connection_id,
            MetricDefinition.metric_embedding.is_(None),
        )
    )
    for metric in result.scalars().all():
        metric.metric_embedding = await embed_metric(metric)
        count += 1
        if on_progress:
            on_progress()

    # Sample queries
    result = await db.execute(
        select(SampleQuery).where(
            SampleQuery.connection_id == connection_id,
            SampleQuery.question_embedding.is_(None),
        )
    )
    for sq in result.scalars().all():
        sq.question_embedding = await embed_sample_query(sq)
        count += 1
        if on_progress:
            on_progress()

    # Knowledge chunks
    result = await db.execute(
        select(KnowledgeChunk)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(
            KnowledgeDocument.connection_id == connection_id,
            KnowledgeChunk.chunk_embedding.is_(None),
        )
    )
    for chunk in result.scalars().all():
        chunk.chunk_embedding = await embed_knowledge_chunk(chunk)
        count += 1
        if on_progress:
            on_progress()

    await db.flush()
    return count
