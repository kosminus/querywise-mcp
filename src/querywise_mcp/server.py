"""querywise-mcp — an MCP server exposing databases through a semantic layer.

Design: the MCP *client* is itself an LLM (Claude, etc.), so the server's job is
to ground that model. It exposes:

* ``get_semantic_context`` + ``run_sql`` — the "thin" path: the client writes SQL
  from the assembled context, then executes it read-only.
* ``ask`` / ``generate_sql`` — the "thick" path: run QueryWise's own NL->SQL
  pipeline server-side (needs an LLM key).
* connection, schema, and semantic-layer management tools.

Transports: stdio (default) and Streamable HTTP. Run via ``querywise-mcp`` or
``querywise serve``.
"""

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from mcp.server.fastmcp import FastMCP
from sqlalchemy import desc, func, select

from querywise_mcp.config import settings
from querywise_mcp.db.models.connection import DatabaseConnection
from querywise_mcp.db.models.query_history import QueryExecution
from querywise_mcp.db.session import session_scope
from querywise_mcp.formatting import format_ask_result
from querywise_mcp.semantic.context_builder import build_context
from querywise_mcp.services import (
    connection_service,
    knowledge_service,
    query_service,
    schema_service,
    semantic_service,
    setup_service,
)

logger = logging.getLogger("querywise_mcp")


@asynccontextmanager
async def _lifespan(_server: FastMCP):
    from querywise_mcp.db.init import init_db

    await init_db()
    yield {}


mcp = FastMCP(
    "querywise",
    instructions=(
        "Query databases in natural language through a business semantic layer. "
        "Recommended loop for a question: call get_semantic_context(connection, "
        "question) to get grounded schema + glossary + metric + example context, "
        "write a read-only SELECT, then call run_sql(connection, sql). Use ask() "
        "to delegate the whole NL->SQL pipeline to the server instead. Manage the "
        "semantic layer with the glossary/metric/dictionary/knowledge tools. "
        "'connection' accepts a connection name or id."
    ),
    host=settings.mcp_host,
    port=settings.mcp_port,
    lifespan=_lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _resolve_connection(db, connection: str) -> DatabaseConnection:
    """Resolve a connection by id (preferred) or by name (case-insensitive)."""
    try:
        cid = uuid.UUID(str(connection))
        conn = await db.get(DatabaseConnection, cid)
        if conn:
            return conn
    except (ValueError, TypeError):
        pass

    name = str(connection).strip()
    res = await db.execute(
        select(DatabaseConnection).where(
            func.lower(DatabaseConnection.name) == name.lower()
        )
    )
    conn = res.scalars().first()
    if not conn:
        raise ValueError(
            f"No connection matching '{connection}'. Use list_connections to see options."
        )
    return conn


def _conn_dict(c: DatabaseConnection) -> dict:
    return {
        "id": str(c.id),
        "name": c.name,
        "connector_type": c.connector_type,
        "default_schema": c.default_schema,
        "max_rows": c.max_rows,
        "max_query_timeout_seconds": c.max_query_timeout_seconds,
        "last_introspected_at": c.last_introspected_at.isoformat()
        if isinstance(c.last_introspected_at, datetime)
        else None,
    }


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #
@mcp.tool()
async def list_connections() -> list[dict]:
    """List all configured database connections."""
    async with session_scope() as db:
        conns = await connection_service.list_connections(db)
        return [_conn_dict(c) for c in conns]


@mcp.tool()
async def create_connection(
    name: str,
    connector_type: str,
    connection_string: str,
    default_schema: str = "public",
    max_rows: int = 1000,
    max_query_timeout_seconds: int = 30,
) -> dict:
    """Create a database connection.

    connector_type is one of: postgresql, bigquery, databricks, mysql, snowflake.
    connection_string is the driver URL (postgres) or the connector-specific JSON
    config (bigquery/databricks). It is encrypted at rest.
    """
    async with session_scope() as db:
        conn = await connection_service.create_connection(
            db,
            name=name,
            connector_type=connector_type,
            connection_string=connection_string,
            default_schema=default_schema,
            max_rows=max_rows,
            max_query_timeout_seconds=max_query_timeout_seconds,
        )
        return _conn_dict(conn)


@mcp.tool()
async def test_connection(connection: str) -> dict:
    """Test connectivity to a configured connection (by name or id)."""
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        ok, message = await connection_service.test_connection(db, conn.id)
        return {"success": ok, "message": message}


@mcp.tool()
async def introspect_connection(connection: str, generate_embeddings: bool = True) -> dict:
    """Introspect and cache a connection's schema (tables, columns, foreign keys).

    Run this once per connection before querying. With generate_embeddings=True,
    also builds vector embeddings for semantic search (requires an embedding
    provider; otherwise keyword matching is used).
    """
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        cid = conn.id
        counts = await schema_service.introspect_and_cache(db, cid)
    embedded = 0
    if generate_embeddings:
        embedded = await setup_service.generate_embeddings_inline(cid)
    return {**counts, "embeddings_generated": embedded}


@mcp.tool()
async def delete_connection(connection: str) -> dict:
    """Delete a connection and all its cached schema + semantic metadata."""
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        await connection_service.delete_connection(db, conn.id)
        return {"deleted": True}


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
@mcp.tool()
async def list_tables(connection: str) -> list[dict]:
    """List cached tables for a connection with their columns."""
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        tables = await schema_service.get_tables(db, conn.id)
        return [
            {
                "id": str(t.id),
                "schema": t.schema_name,
                "name": t.table_name,
                "type": t.table_type,
                "comment": t.comment,
                "row_count_estimate": t.row_count_estimate,
                "columns": [
                    {
                        "name": col.column_name,
                        "type": col.data_type,
                        "nullable": col.is_nullable,
                        "primary_key": col.is_primary_key,
                        "comment": col.comment,
                    }
                    for col in sorted(t.columns, key=lambda c: c.ordinal_position)
                ],
            }
            for t in tables
        ]


@mcp.tool()
async def describe_table(connection: str, table_name: str) -> dict:
    """Describe one table: columns plus incoming/outgoing foreign keys."""
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        tables = await schema_service.get_tables(db, conn.id)
        match = next((t for t in tables if t.table_name == table_name), None)
        if not match:
            raise ValueError(f"Table '{table_name}' not found on '{conn.name}'.")
        detail = await schema_service.get_table_detail(db, match.id)
        return {
            "schema": detail.schema_name,
            "name": detail.table_name,
            "comment": detail.comment,
            "columns": [
                {
                    "name": c.column_name,
                    "type": c.data_type,
                    "nullable": c.is_nullable,
                    "primary_key": c.is_primary_key,
                    "default": c.default_value,
                    "comment": c.comment,
                }
                for c in sorted(detail.columns, key=lambda c: c.ordinal_position)
            ],
            "foreign_keys": [
                {
                    "column": r.source_column,
                    "references_table": r.target_table.table_name,
                    "references_column": r.target_column,
                }
                for r in detail.outgoing_relationships
            ],
            "referenced_by": [
                {
                    "table": r.source_table.table_name,
                    "column": r.source_column,
                    "references_column": r.target_column,
                }
                for r in detail.incoming_relationships
            ],
        }


# --------------------------------------------------------------------------- #
# Core query tools
# --------------------------------------------------------------------------- #
@mcp.tool()
async def get_semantic_context(connection: str, question: str) -> str:
    """Return grounded context for a question: the relevant schema, foreign keys,
    business glossary, metric definitions, knowledge excerpts, value dictionaries,
    and example queries — formatted for SQL generation.

    This is the recommended first step: pass the result to your own reasoning,
    write a read-only SELECT, then call run_sql.
    """
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        ctx = await build_context(db, conn.id, question, dialect=conn.connector_type)
        return ctx.prompt_context


@mcp.tool()
async def run_sql(connection: str, sql: str) -> dict:
    """Execute a read-only SQL query against a connection and return the rows.

    Rejects non-SELECT / unsafe SQL. Results are row-limited per the connection's
    settings. Use this after writing SQL from get_semantic_context.
    """
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        result = await query_service.execute_raw_sql(db, conn.id, sql)
        return {
            "columns": result["columns"],
            "rows": result["rows"],
            "row_count": result["row_count"],
            "truncated": result["truncated"],
            "execution_time_ms": result["execution_time_ms"],
        }


@mcp.tool()
async def generate_sql(connection: str, question: str) -> dict:
    """Generate SQL for a question using the server-side LLM pipeline (no execute).

    Requires an LLM provider to be configured. For zero-key operation, prefer
    get_semantic_context + your own SQL + run_sql.
    """
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        return await query_service.generate_sql_only(db, conn.id, question)


@mcp.tool()
async def ask(connection: str, question: str) -> str:
    """Answer a natural-language question end-to-end via the server pipeline:
    build context, generate SQL, validate, execute, and interpret.

    Returns a Markdown report (answer summary, highlights, executed SQL, metadata,
    suggested follow-ups, and a data preview). Requires an LLM provider configured.
    """
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        res = await query_service.execute_nl_query(db, conn.id, question)
        return format_ask_result(res)


@mcp.tool()
async def query_history(connection: str, limit: int = 20) -> list[dict]:
    """Recent query executions for a connection (newest first)."""
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        res = await db.execute(
            select(QueryExecution)
            .where(QueryExecution.connection_id == conn.id)
            .order_by(desc(QueryExecution.created_at))
            .limit(limit)
        )
        return [
            {
                "id": str(q.id),
                "question": q.natural_language,
                "sql": q.final_sql,
                "status": q.execution_status,
                "row_count": q.row_count,
                "created_at": q.created_at.isoformat() if q.created_at else None,
            }
            for q in res.scalars().all()
        ]


# --------------------------------------------------------------------------- #
# Semantic layer: glossary / metrics / dictionary / sample queries / knowledge
# --------------------------------------------------------------------------- #
@mcp.tool()
async def list_glossary(connection: str) -> list[dict]:
    """List business glossary terms for a connection."""
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        terms = await semantic_service.list_glossary(db, conn.id)
        return [
            {
                "id": str(t.id),
                "term": t.term,
                "definition": t.definition,
                "sql_expression": t.sql_expression,
                "related_tables": t.related_tables or [],
            }
            for t in terms
        ]


@mcp.tool()
async def add_glossary_term(
    connection: str,
    term: str,
    definition: str,
    sql_expression: str,
    related_tables: list[str] | None = None,
) -> dict:
    """Add a business glossary term (e.g. how 'active customer' maps to SQL)."""
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        obj = await semantic_service.create_glossary(
            db, conn.id, term, definition, sql_expression, related_tables
        )
        return {"id": str(obj.id), "term": obj.term}


@mcp.tool()
async def delete_glossary_term(term_id: str) -> dict:
    """Delete a glossary term by id."""
    async with session_scope() as db:
        ok = await semantic_service.delete_glossary(db, uuid.UUID(term_id))
        return {"deleted": ok}


@mcp.tool()
async def list_metrics(connection: str) -> list[dict]:
    """List metric definitions for a connection."""
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        metrics = await semantic_service.list_metrics(db, conn.id)
        return [
            {
                "id": str(m.id),
                "metric_name": m.metric_name,
                "display_name": m.display_name,
                "sql_expression": m.sql_expression,
                "dimensions": m.dimensions or [],
            }
            for m in metrics
        ]


@mcp.tool()
async def add_metric(
    connection: str,
    metric_name: str,
    display_name: str,
    sql_expression: str,
    description: str | None = None,
    related_tables: list[str] | None = None,
    dimensions: list[str] | None = None,
) -> dict:
    """Add a metric definition (a named, reusable SQL aggregate)."""
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        obj = await semantic_service.create_metric(
            db,
            conn.id,
            metric_name,
            display_name,
            sql_expression,
            description,
            related_tables,
            dimensions,
        )
        return {"id": str(obj.id), "metric_name": obj.metric_name}


@mcp.tool()
async def delete_metric(metric_id: str) -> dict:
    """Delete a metric definition by id."""
    async with session_scope() as db:
        ok = await semantic_service.delete_metric(db, uuid.UUID(metric_id))
        return {"deleted": ok}


@mcp.tool()
async def add_dictionary_entry(
    connection: str,
    table_name: str,
    column_name: str,
    raw_value: str,
    display_value: str,
    description: str | None = None,
) -> dict:
    """Map a raw column value to its business meaning (e.g. stage '1' -> 'Performing')."""
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        col_id = await semantic_service.resolve_column_id(
            db, conn.id, table_name, column_name
        )
        if not col_id:
            raise ValueError(
                f"Column {table_name}.{column_name} not found "
                "(introspect the connection first)."
            )
        obj = await semantic_service.create_dictionary_entry(
            db, col_id, raw_value, display_value, description
        )
        return {"id": str(obj.id), "raw_value": obj.raw_value}


@mcp.tool()
async def list_sample_queries(connection: str) -> list[dict]:
    """List saved example NL->SQL pairs used as few-shot examples."""
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        items = await semantic_service.list_sample_queries(db, conn.id)
        return [
            {
                "id": str(s.id),
                "natural_language": s.natural_language,
                "sql_query": s.sql_query,
                "is_validated": s.is_validated,
            }
            for s in items
        ]


@mcp.tool()
async def add_sample_query(
    connection: str,
    natural_language: str,
    sql_query: str,
    description: str | None = None,
) -> dict:
    """Add a validated example NL->SQL pair (improves future generations)."""
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        obj = await semantic_service.create_sample_query(
            db, conn.id, natural_language, sql_query, description
        )
        return {"id": str(obj.id)}


@mcp.tool()
async def list_knowledge(connection: str) -> list[dict]:
    """List imported knowledge documents for a connection."""
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        docs = await semantic_service.list_knowledge(db, conn.id)
        return [
            {
                "id": str(d.id),
                "title": d.title,
                "source_url": d.source_url,
                "chunk_count": d.chunk_count,
            }
            for d in docs
        ]


@mcp.tool()
async def add_knowledge(
    connection: str,
    title: str,
    content: str,
    source_url: str | None = None,
) -> dict:
    """Import documentation (plain text or HTML) as searchable business knowledge."""
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        doc = await knowledge_service.import_document(
            db, conn.id, title=title, content=content, source_url=source_url
        )
        return {"id": str(doc.id), "title": doc.title, "chunks": doc.chunk_count}


@mcp.tool()
async def add_knowledge_url(connection: str, url: str, title: str | None = None) -> dict:
    """Fetch a URL server-side and import its content as business knowledge."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        doc = await knowledge_service.import_document(
            db, conn.id, title=title or url, content=html, source_url=url
        )
        return {"id": str(doc.id), "title": doc.title, "chunks": doc.chunk_count}


@mcp.tool()
async def delete_knowledge(doc_id: str) -> dict:
    """Delete a knowledge document by id."""
    async with session_scope() as db:
        ok = await semantic_service.delete_knowledge(db, uuid.UUID(doc_id))
        return {"deleted": ok}


# --------------------------------------------------------------------------- #
# Resources & prompts
# --------------------------------------------------------------------------- #
@mcp.resource("querywise://{connection}/schema")
async def schema_resource(connection: str) -> str:
    """The full cached schema for a connection as readable text."""
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        tables = await schema_service.get_tables(db, conn.id)
    lines = [f"# Schema for {conn.name} ({conn.connector_type})", ""]
    for t in tables:
        lines.append(f"## {t.schema_name}.{t.table_name}")
        if t.comment:
            lines.append(f"_{t.comment}_")
        for c in sorted(t.columns, key=lambda c: c.ordinal_position):
            pk = " PK" if c.is_primary_key else ""
            lines.append(f"- {c.column_name} ({c.data_type}){pk}")
        lines.append("")
    return "\n".join(lines)


@mcp.prompt()
def text_to_sql(connection: str, question: str) -> str:
    """A prompt scaffold instructing the client to ground, write, and run SQL."""
    return (
        f"Answer this question about the '{connection}' database: {question}\n\n"
        f"Steps:\n"
        f"1. Call get_semantic_context('{connection}', '{question}') to get the "
        f"relevant schema, glossary, metrics, and examples.\n"
        f"2. Write a single read-only SELECT using only the tables/columns shown.\n"
        f"3. Call run_sql('{connection}', <your sql>) to execute it.\n"
        f"4. Summarize the results for the user."
    )


def main() -> None:
    """Console entry point for the MCP server."""
    logging.basicConfig(level=logging.INFO)
    transport = "streamable-http" if settings.mcp_transport == "http" else "stdio"
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
