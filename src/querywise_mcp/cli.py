"""querywise — command-line interface.

Provides a local `ask` front-end over the same pipeline the MCP server exposes,
plus connection/semantic management and a `serve` command to launch the MCP
server. Run `querywise --help`.
"""

import asyncio
import json
import logging
import sys

import typer

from querywise_mcp.config import settings
from querywise_mcp.db.init import init_db
from querywise_mcp.db.session import session_scope
from querywise_mcp.formatting import format_ask_result
from querywise_mcp.semantic.context_builder import build_context
from querywise_mcp.services import (
    connection_service,
    query_service,
    schema_service,
    setup_service,
)

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="QueryWise — databases + semantic layer over MCP.",
)
conn_app = typer.Typer(no_args_is_help=True, help="Manage database connections.")
app.add_typer(conn_app, name="connections")


@app.callback()
def _configure(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show debug logs."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only warnings and errors."),
):
    """Configure logging for all commands (logs go to stderr).

    Only QueryWise's own logs follow the chosen level; noisy third-party
    libraries (aiosqlite, httpx, …) stay at WARNING.
    """
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("querywise_mcp").setLevel(level)


def _run(coro):
    try:
        return asyncio.run(coro)
    except Exception as e:  # present a clean message instead of a traceback
        typer.secho(f"Error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None


async def _resolve(db, connection: str):
    from querywise_mcp.server import _resolve_connection

    return await _resolve_connection(db, connection)


# --------------------------------------------------------------------------- #
# Server / setup
# --------------------------------------------------------------------------- #
@app.command()
def serve(
    http: bool = typer.Option(False, "--http", help="Serve over Streamable HTTP instead of stdio."),
    host: str = typer.Option(None, help="HTTP host (default from config)."),
    port: int = typer.Option(None, help="HTTP port (default from config)."),
):
    """Run the MCP server (stdio by default, or --http)."""
    from querywise_mcp import server

    if host:
        server.mcp.settings.host = host
    if port:
        server.mcp.settings.port = port
    transport = "streamable-http" if http else "stdio"
    typer.echo(f"Starting querywise-mcp ({transport})...", err=True)
    server.mcp.run(transport=transport)


@app.command()
def init():
    """Create the SQLite metadata store (idempotent)."""
    _run(init_db())
    typer.echo(f"Initialized store at {settings.resolved_database_url()}")


@app.command("seed-sample")
def seed_sample():
    """Seed the IFRS 9 sample connection + semantic metadata (needs the sample DB)."""
    from querywise_mcp.services.embedding_service import (
        count_items_needing_embeddings,
        embeddings_available,
    )

    async def _go():
        await init_db()
        await setup_service.auto_setup_sample_db()
        async with session_scope() as db:
            conns = await connection_service.list_connections(db)
            rows = []
            for c in conns:
                tables = await schema_service.get_tables(db, c.id)
                pending = await count_items_needing_embeddings(db, c.id)
                rows.append((c.name, len(tables), pending))
        return rows, embeddings_available()

    rows, emb_on = _run(_go())
    typer.echo("Sample setup complete.")
    if not rows:
        typer.echo(
            "  No connections were created — is the sample database reachable at "
            f"{settings.sample_db_connection_string}?"
        )
    for name, n_tables, pending in rows:
        if not emb_on:
            note = "embeddings disabled (keyword-only search)"
        elif pending == 0:
            note = "embeddings generated"
        else:
            note = f"{pending} item(s) still need embeddings"
        typer.echo(f"  • {name}: {n_tables} table(s) cached — {note}")


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #
@conn_app.command("list")
def connections_list():
    """List configured connections."""
    async def _go():
        async with session_scope() as db:
            return await connection_service.list_connections(db)

    conns = _run(_go())
    if not conns:
        typer.echo("No connections. Add one with: querywise connections add ...")
        return
    for c in conns:
        introspected = "yes" if c.last_introspected_at else "no"
        typer.echo(f"{c.id}  {c.name}  [{c.connector_type}]  introspected={introspected}")


@conn_app.command("add")
def connections_add(
    name: str,
    connector_type: str = typer.Option("postgresql", help="postgresql|bigquery|databricks|..."),
    connection_string: str = typer.Option(..., "--connection-string", "-c"),
    default_schema: str = typer.Option("public"),
    introspect: bool = typer.Option(True, help="Introspect schema after creating."),
):
    """Add a connection (and introspect it by default)."""
    async def _go():
        await init_db()
        async with session_scope() as db:
            conn = await connection_service.create_connection(
                db,
                name=name,
                connector_type=connector_type,
                connection_string=connection_string,
                default_schema=default_schema,
            )
            cid = conn.id
        if introspect:
            async with session_scope() as db:
                counts = await schema_service.introspect_and_cache(db, cid)
            await setup_service.generate_embeddings_inline(cid)
            return cid, counts
        return cid, None

    cid, counts = _run(_go())
    typer.echo(f"Created connection {cid}")
    if counts:
        typer.echo(f"Introspected: {counts}")


@conn_app.command("test")
def connections_test(connection: str):
    """Test a connection by name or id."""
    async def _go():
        async with session_scope() as db:
            conn = await _resolve(db, connection)
            return await connection_service.test_connection(db, conn.id)

    ok, message = _run(_go())
    typer.echo(("OK: " if ok else "FAILED: ") + message)


@conn_app.command("introspect")
def connections_introspect(connection: str):
    """Introspect (refresh) a connection's schema + embeddings."""
    async def _go():
        async with session_scope() as db:
            conn = await _resolve(db, connection)
            cid = conn.id
            counts = await schema_service.introspect_and_cache(db, cid)
        embedded = await setup_service.generate_embeddings_inline(cid)
        return counts, embedded

    counts, embedded = _run(_go())
    typer.echo(f"{counts}  embeddings_generated={embedded}")


@conn_app.command("delete")
def connections_delete(connection: str):
    """Delete a connection and its cached metadata."""
    async def _go():
        async with session_scope() as db:
            conn = await _resolve(db, connection)
            await connection_service.delete_connection(db, conn.id)

    _run(_go())
    typer.echo("Deleted.")


# --------------------------------------------------------------------------- #
# Query
# --------------------------------------------------------------------------- #
@app.command()
def ask(connection: str, question: str, json_out: bool = typer.Option(False, "--json")):
    """Ask a natural-language question (full server-side pipeline; needs an LLM key)."""
    async def _go():
        async with session_scope() as db:
            conn = await _resolve(db, connection)
            return await query_service.execute_nl_query(db, conn.id, question)

    result = _run(_go())
    if json_out:
        typer.echo(json.dumps(result, indent=2, default=str))
        return
    typer.echo(format_ask_result(result, max_preview_rows=50))


@app.command()
def sql(connection: str, statement: str):
    """Execute a read-only SQL statement against a connection."""
    async def _go():
        async with session_scope() as db:
            conn = await _resolve(db, connection)
            return await query_service.execute_raw_sql(db, conn.id, statement)

    result = _run(_go())
    typer.echo(" | ".join(result["columns"]))
    for row in result["rows"][:100]:
        typer.echo(" | ".join(str(v) for v in row))
    typer.echo(f"\n({result['row_count']} rows)")


@app.command()
def context(connection: str, question: str):
    """Print the semantic context that would be sent to the LLM for a question."""
    async def _go():
        async with session_scope() as db:
            conn = await _resolve(db, connection)
            ctx = await build_context(db, conn.id, question, dialect=conn.connector_type)
            return ctx.prompt_context

    typer.echo(_run(_go()))


if __name__ == "__main__":
    app()
