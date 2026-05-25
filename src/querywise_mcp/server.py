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
from typing import Annotated

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field
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
# Shared parameter descriptions & annotation presets
# --------------------------------------------------------------------------- #
# Reused for the ubiquitous ``connection`` argument so every tool documents it
# identically (TDQS "Parameter Semantics").
_CONN_DESC = (
    "Target database connection — its name or id (case-insensitive). "
    "List the available connections with list_connections."
)
Connection = Annotated[str, Field(description=_CONN_DESC)]

# Annotation presets (behavioral hints surfaced to MCP clients).
_READ_ONLY = dict(readOnlyHint=True)
_READ_ONLY_EXTERNAL = dict(readOnlyHint=True, openWorldHint=True)
_WRITE = dict(readOnlyHint=False, idempotentHint=False)
_DESTRUCTIVE = dict(readOnlyHint=False, destructiveHint=True, idempotentHint=True)


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
@mcp.tool(annotations=ToolAnnotations(title="List connections", **_READ_ONLY))
async def list_connections() -> list[dict]:
    """List all configured database connections (id, name, type, limits).

    Call this first to discover which databases exist and to get the name or id
    that every other tool's `connection` argument accepts. Read-only; returns an
    empty list when nothing is configured yet (add one with create_connection).
    """
    async with session_scope() as db:
        conns = await connection_service.list_connections(db)
        return [_conn_dict(c) for c in conns]


@mcp.tool(annotations=ToolAnnotations(title="Create connection", **_WRITE))
async def create_connection(
    name: Annotated[
        str,
        Field(description="Unique, human-friendly name used to reference this connection later."),
    ],
    connector_type: Annotated[
        str,
        Field(
            description="One of: postgresql, bigquery, databricks, mysql, snowflake."
        ),
    ],
    connection_string: Annotated[
        str,
        Field(
            description="Driver URL (PostgreSQL/MySQL) or connector-specific JSON config "
            "(BigQuery/Databricks). Stored encrypted at rest."
        ),
    ],
    default_schema: Annotated[
        str,
        Field(description="Default schema to introspect and query when none is specified."),
    ] = "public",
    max_rows: Annotated[
        int,
        Field(description="Maximum number of rows any query on this connection may return."),
    ] = 1000,
    max_query_timeout_seconds: Annotated[
        int,
        Field(description="Per-query timeout, in seconds, for this connection."),
    ] = 30,
) -> dict:
    """Register a new target database connection (credentials encrypted at rest).

    Use once per database before introspecting or querying it. This only stores
    the connection — it does NOT verify connectivity or read the schema; follow
    with test_connection, then introspect_connection. Returns the created
    connection's id and metadata.
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


@mcp.tool(annotations=ToolAnnotations(title="Test connection", **_READ_ONLY_EXTERNAL))
async def test_connection(connection: Connection) -> dict:
    """Check that a configured connection can be reached and authenticated.

    Use after create_connection to validate credentials and network access before
    introspecting. Read-only: opens and closes a probe connection without reading
    the schema (use introspect_connection for that). Returns {success, message}.
    """
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        ok, message = await connection_service.test_connection(db, conn.id)
        return {"success": ok, "message": message}


@mcp.tool(
    annotations=ToolAnnotations(
        title="Introspect connection", readOnlyHint=False, idempotentHint=True, openWorldHint=True
    )
)
async def introspect_connection(
    connection: Connection,
    generate_embeddings: Annotated[
        bool,
        Field(
            description="Also build vector embeddings for semantic schema search. Needs an "
            "embedding provider; otherwise keyword matching is used."
        ),
    ] = True,
) -> dict:
    """Read the target database's structure (tables, columns, foreign keys) and cache it.

    Run once per connection before querying, and again after the schema changes.
    Idempotent — re-running refreshes the cache. The cache is what list_tables,
    describe_table, and get_semantic_context read from. Returns counts of cached
    objects plus the number of embeddings generated.
    """
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        cid = conn.id
        counts = await schema_service.introspect_and_cache(db, cid)
    embedded = 0
    if generate_embeddings:
        embedded = await setup_service.generate_embeddings_inline(cid)
    return {**counts, "embeddings_generated": embedded}


@mcp.tool(annotations=ToolAnnotations(title="Delete connection", **_DESTRUCTIVE))
async def delete_connection(connection: Connection) -> dict:
    """Permanently delete a connection and all its cached schema + semantic metadata.

    Removes the connection plus its glossary, metrics, dictionary, sample queries,
    and knowledge. Destructive and not reversible — use only to retire a database
    you no longer query. Returns {deleted: true}.
    """
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        await connection_service.delete_connection(db, conn.id)
        return {"deleted": True}


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
@mcp.tool(annotations=ToolAnnotations(title="List tables", **_READ_ONLY))
async def list_tables(connection: Connection) -> list[dict]:
    """List a connection's cached tables, each with its columns.

    Returns name, type, comment, row-count estimate, and per-column details
    (type, nullability, primary key). Reads the cache from introspect_connection
    (run that first if the result is empty). Use for a schema-wide overview; for
    one table's foreign keys and relationships, use describe_table. Read-only.
    """
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


@mcp.tool(annotations=ToolAnnotations(title="Describe table", **_READ_ONLY))
async def describe_table(
    connection: Connection,
    table_name: Annotated[
        str,
        Field(description="Exact table name to describe, as shown by list_tables."),
    ],
) -> dict:
    """Describe one cached table in detail, including its foreign-key relationships.

    Returns columns (with defaults/comments), outgoing foreign keys, and incoming
    references from other tables. Use when you need a single table's keys to write
    a join; for a list of all tables use list_tables. Reads the cache (introspect
    first). Raises if the table is not found.
    """
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
@mcp.tool(annotations=ToolAnnotations(title="Get semantic context", **_READ_ONLY))
async def get_semantic_context(
    connection: Connection,
    question: Annotated[
        str,
        Field(
            description="The natural-language question you intend to answer with SQL; used to "
            "select the most relevant schema and semantic-layer entries."
        ),
    ],
) -> str:
    """Assemble grounded, SQL-ready context for a question.

    Returns the relevant tables/columns, foreign keys, business glossary, metric
    definitions, value dictionaries, knowledge excerpts, and example queries as
    formatted text. This is the recommended first step of the lightweight path:
    take the result, write a read-only SELECT yourself, then call run_sql. Needs
    no LLM key. For a fully automated answer instead, use ask.
    """
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        ctx = await build_context(db, conn.id, question, dialect=conn.connector_type)
        return ctx.prompt_context


@mcp.tool(annotations=ToolAnnotations(title="Run SQL", **_READ_ONLY_EXTERNAL))
async def run_sql(
    connection: Connection,
    sql: Annotated[
        str,
        Field(
            description="A single read-only SELECT statement to execute. Non-SELECT or unsafe "
            "SQL is rejected."
        ),
    ],
) -> dict:
    """Execute a read-only SQL SELECT against the target database and return the rows.

    Use to run SQL you wrote from get_semantic_context. Enforces read-only:
    rejects INSERT/UPDATE/DELETE/DDL and other unsafe statements; results are
    row-limited per the connection's max_rows. Returns columns, rows, row_count,
    truncated, and execution_time_ms. To have the server write the SQL for you,
    use generate_sql or ask.
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


@mcp.tool(annotations=ToolAnnotations(title="Generate SQL", **_READ_ONLY_EXTERNAL))
async def generate_sql(
    connection: Connection,
    question: Annotated[
        str,
        Field(description="Natural-language question to translate into SQL."),
    ],
) -> dict:
    """Translate a natural-language question into SQL via the server LLM, without executing it.

    Requires an LLM provider to be configured. Use when you want to review or edit
    the SQL before running it with run_sql. For zero-key operation, use
    get_semantic_context and write the SQL yourself; to also execute and interpret
    in one step, use ask. Returns the generated SQL plus supporting details.
    """
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        return await query_service.generate_sql_only(db, conn.id, question)


@mcp.tool(annotations=ToolAnnotations(title="Ask (NL->answer)", **_READ_ONLY_EXTERNAL))
async def ask(
    connection: Connection,
    question: Annotated[
        str,
        Field(description="Natural-language question to answer end-to-end."),
    ],
) -> str:
    """Answer a natural-language question end-to-end via the server pipeline.

    Builds context, generates SQL, validates, executes it (read-only), and
    interprets the results. This is the fully automated path and requires an LLM
    provider. Use it when you want a finished answer rather than raw rows; use the
    get_semantic_context + run_sql path for manual control, or generate_sql to get
    SQL without executing. Returns a Markdown report (summary, highlights,
    executed SQL, metadata, follow-ups, and a data preview).
    """
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        res = await query_service.execute_nl_query(db, conn.id, question)
        return format_ask_result(res)


@mcp.tool(annotations=ToolAnnotations(title="Query history", **_READ_ONLY))
async def query_history(
    connection: Connection,
    limit: Annotated[
        int,
        Field(description="Maximum number of past executions to return (newest first)."),
    ] = 20,
) -> list[dict]:
    """List recent query executions for a connection, newest first.

    Returns each execution's question, final SQL, status, row count, and
    timestamp. Use to review or reuse previously run queries. Read-only.
    """
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
@mcp.tool(annotations=ToolAnnotations(title="List glossary terms", **_READ_ONLY))
async def list_glossary(connection: Connection) -> list[dict]:
    """List the business glossary terms defined for a connection.

    Returns each term, its plain-language definition, the SQL expression that
    implements it, and related tables. Glossary terms map business language (e.g.
    'active customer') to SQL. Add with add_glossary_term; for numeric KPIs see
    list_metrics. Read-only.
    """
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


@mcp.tool(annotations=ToolAnnotations(title="Add glossary term", **_WRITE))
async def add_glossary_term(
    connection: Connection,
    term: Annotated[
        str,
        Field(description="The business term being defined (e.g. 'active customer')."),
    ],
    definition: Annotated[
        str,
        Field(description="Plain-language meaning of the term."),
    ],
    sql_expression: Annotated[
        str,
        Field(
            description="SQL snippet/predicate that implements the term (e.g. a WHERE condition)."
        ),
    ],
    related_tables: Annotated[
        list[str] | None,
        Field(description="Optional list of table names the term applies to."),
    ] = None,
) -> dict:
    """Define a business glossary term that maps business language to a SQL expression.

    Use to teach the semantic layer phrases like 'active customer' so future
    grounding and generation apply them consistently. For a named, reusable
    aggregate (a KPI) use add_metric instead. Returns the new term's id.
    """
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        obj = await semantic_service.create_glossary(
            db, conn.id, term, definition, sql_expression, related_tables
        )
        return {"id": str(obj.id), "term": obj.term}


@mcp.tool(annotations=ToolAnnotations(title="Delete glossary term", **_DESTRUCTIVE))
async def delete_glossary_term(
    term_id: Annotated[
        str,
        Field(description="Id of the glossary term to delete (from list_glossary)."),
    ],
) -> dict:
    """Delete one business glossary term by its id.

    Destructive and not reversible. Look up ids with list_glossary. Returns
    {deleted} indicating whether a matching term was removed.
    """
    async with session_scope() as db:
        ok = await semantic_service.delete_glossary(db, uuid.UUID(term_id))
        return {"deleted": ok}


@mcp.tool(annotations=ToolAnnotations(title="List metrics", **_READ_ONLY))
async def list_metrics(connection: Connection) -> list[dict]:
    """List the metric definitions for a connection.

    Returns each metric's name, display name, SQL aggregate expression, and
    dimensions. Metrics are named, reusable KPIs. Add with add_metric; for
    phrase-to-SQL term mappings see list_glossary. Read-only.
    """
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


@mcp.tool(annotations=ToolAnnotations(title="Add metric", **_WRITE))
async def add_metric(
    connection: Connection,
    metric_name: Annotated[
        str,
        Field(description="Machine-friendly metric identifier (e.g. 'gross_revenue')."),
    ],
    display_name: Annotated[
        str,
        Field(description="Human-friendly metric label (e.g. 'Gross Revenue')."),
    ],
    sql_expression: Annotated[
        str,
        Field(description="SQL aggregate expression implementing the metric (e.g. SUM(amount))."),
    ],
    description: Annotated[
        str | None,
        Field(description="Optional explanation of what the metric measures."),
    ] = None,
    related_tables: Annotated[
        list[str] | None,
        Field(description="Optional list of table names the metric is computed from."),
    ] = None,
    dimensions: Annotated[
        list[str] | None,
        Field(
            description="Optional dimensions to group the metric by (e.g. ['region','month'])."
        ),
    ] = None,
) -> dict:
    """Define a metric: a named, reusable SQL aggregate (a KPI).

    Use for quantitative measures like revenue or default rate so grounding and
    generation can reuse them; for phrase-to-SQL mappings use add_glossary_term
    instead. Returns the new metric's id and name.
    """
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


@mcp.tool(annotations=ToolAnnotations(title="Delete metric", **_DESTRUCTIVE))
async def delete_metric(
    metric_id: Annotated[
        str,
        Field(description="Id of the metric to delete (from list_metrics)."),
    ],
) -> dict:
    """Delete one metric definition by its id.

    Destructive and not reversible. Look up ids with list_metrics. Returns
    {deleted} indicating whether a matching metric was removed.
    """
    async with session_scope() as db:
        ok = await semantic_service.delete_metric(db, uuid.UUID(metric_id))
        return {"deleted": ok}


@mcp.tool(annotations=ToolAnnotations(title="Add dictionary entry", **_WRITE))
async def add_dictionary_entry(
    connection: Connection,
    table_name: Annotated[
        str,
        Field(description="Table containing the column (must already be introspected)."),
    ],
    column_name: Annotated[
        str,
        Field(description="Column whose coded value you are explaining."),
    ],
    raw_value: Annotated[
        str,
        Field(description="The stored/coded value as it appears in the column (e.g. '1')."),
    ],
    display_value: Annotated[
        str,
        Field(description="The business meaning of that value (e.g. 'Performing')."),
    ],
    description: Annotated[
        str | None,
        Field(description="Optional extra explanation of the value."),
    ] = None,
) -> dict:
    """Map a coded column value to its business meaning (e.g. stage '1' -> 'Performing').

    Use so grounding and generation can interpret enum-like codes. Requires the
    connection to be introspected first so the column can be resolved. Returns the
    new entry's id.
    """
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


@mcp.tool(annotations=ToolAnnotations(title="List sample queries", **_READ_ONLY))
async def list_sample_queries(connection: Connection) -> list[dict]:
    """List saved example natural-language -> SQL pairs for a connection.

    These validated pairs are used as few-shot examples that steer SQL
    generation. Add with add_sample_query. Read-only.
    """
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


@mcp.tool(annotations=ToolAnnotations(title="Add sample query", **_WRITE))
async def add_sample_query(
    connection: Connection,
    natural_language: Annotated[
        str,
        Field(description="Example question in natural language."),
    ],
    sql_query: Annotated[
        str,
        Field(description="Correct, validated SQL that answers the question."),
    ],
    description: Annotated[
        str | None,
        Field(description="Optional note about the example."),
    ] = None,
) -> dict:
    """Save a validated natural-language -> SQL example to improve future generation.

    Use to capture good question/SQL pairs for this connection; they are reused as
    few-shot examples by generate_sql and ask. Returns the new example's id.
    """
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        obj = await semantic_service.create_sample_query(
            db, conn.id, natural_language, sql_query, description
        )
        return {"id": str(obj.id)}


@mcp.tool(annotations=ToolAnnotations(title="List knowledge", **_READ_ONLY))
async def list_knowledge(connection: Connection) -> list[dict]:
    """List the knowledge documents imported for a connection.

    Returns each document's title, source URL, and chunk count. Knowledge docs
    are searchable business context (policies, data dictionaries, runbooks) used
    during grounding. Add with add_knowledge or add_knowledge_url. Read-only.
    """
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


@mcp.tool(annotations=ToolAnnotations(title="Add knowledge (text)", **_WRITE))
async def add_knowledge(
    connection: Connection,
    title: Annotated[
        str,
        Field(description="Title for the knowledge document."),
    ],
    content: Annotated[
        str,
        Field(
            description="Document body as plain text or HTML; it is chunked and indexed for search."
        ),
    ],
    source_url: Annotated[
        str | None,
        Field(description="Optional source URL to record as provenance."),
    ] = None,
) -> dict:
    """Import a document you provide (plain text or HTML) as searchable business knowledge.

    Use when you already have the content; to fetch it from a web page instead,
    use add_knowledge_url. The content is chunked and embedded for semantic
    retrieval during grounding. Returns the document id and chunk count.
    """
    async with session_scope() as db:
        conn = await _resolve_connection(db, connection)
        doc = await knowledge_service.import_document(
            db, conn.id, title=title, content=content, source_url=source_url
        )
        return {"id": str(doc.id), "title": doc.title, "chunks": doc.chunk_count}


@mcp.tool(
    annotations=ToolAnnotations(
        title="Add knowledge (URL)", readOnlyHint=False, idempotentHint=False, openWorldHint=True
    )
)
async def add_knowledge_url(
    connection: Connection,
    url: Annotated[
        str,
        Field(description="Public URL to fetch server-side and import."),
    ],
    title: Annotated[
        str | None,
        Field(description="Optional title; defaults to the URL."),
    ] = None,
) -> dict:
    """Fetch a web page server-side and import its content as searchable business knowledge.

    Use to ingest documentation by URL; to import content you already have, use
    add_knowledge. Performs an outbound HTTP GET (follows redirects, 30s timeout),
    then chunks and embeds the page. Returns the document id and chunk count.
    """
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


@mcp.tool(annotations=ToolAnnotations(title="Delete knowledge", **_DESTRUCTIVE))
async def delete_knowledge(
    doc_id: Annotated[
        str,
        Field(description="Id of the knowledge document to delete (from list_knowledge)."),
    ],
) -> dict:
    """Delete one knowledge document (and its chunks) by id.

    Destructive and not reversible. Look up ids with list_knowledge. Returns
    {deleted} indicating whether a matching document was removed.
    """
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
