# CLAUDE.md

## Project Overview

**querywise-mcp** — an MCP server + CLI for querying databases in natural
language through a semantic metadata layer. Refactored from the QueryWise
full-stack app (`../querywise`): the FastAPI web layer and React frontend are
gone; the metadata store moved from Postgres+pgvector to embedded
**SQLite + sqlite-vec**. The semantic layer, LLM agents, and connectors are
ported largely unchanged.

## Tech Stack

- Python 3.11+, MCP Python SDK (FastMCP), Typer (CLI)
- SQLAlchemy (async) + aiosqlite + sqlite-vec for the metadata store
- Target connectors: SQLite (read-only), PostgreSQL (asyncpg), BigQuery, Databricks
- LLM: provider-agnostic (Anthropic, OpenAI, Ollama) — only needed for `ask`/`generate_sql` and cloud embeddings

## How to Run

```bash
pip install -e ".[llm]"           # install (drop [llm] for keyword-only)
querywise init                    # create the SQLite store
querywise serve                   # MCP server over stdio (--http for Streamable HTTP)
querywise ask <conn> "<question>" # full pipeline via CLI
```

Entry points (`pyproject.toml`): `querywise` → `cli:app`, `querywise-mcp` → `server:main`.

## Layout

```
src/querywise_mcp/
├── config.py          # pydantic-settings; resolved_database_url() -> sqlite path
├── server.py          # FastMCP: 25 tools + schema resource + text_to_sql prompt; stdio/http
├── cli.py             # Typer CLI (ask, sql, context, connections, serve, seed-sample)
├── db/                # SQLite metadata store
│   ├── session.py     # async engine; loads sqlite-vec via await_only in connect event; WAL
│   ├── types.py       # Embedding TypeDecorator (list[float] <-> float32 BLOB)
│   ├── vectors.py     # knn(): vec_distance_cosine when loaded, else in-process cosine
│   ├── init.py        # init_db() create_all + embedding-dimension reconciliation (no Alembic)
│   └── models/        # ORM models (generic types: Uuid, JSON, Embedding)
├── semantic/          # context_builder, schema_linker, glossary_resolver, prompt_assembler
├── llm/               # providers (anthropic/openai/ollama), agents, router, prompts
├── services/          # query_service, connection_service, schema_service,
│                      #   embedding_service, knowledge_service, semantic_service, setup_service
├── connectors/        # base + sqlite/postgresql/bigquery/databricks + registry
├── core/exceptions.py
└── utils/sql_sanitizer.py
```

## Key Conventions & Differences from QueryWise

- **Vectors**: stored as float32 BLOB (`db/types.Embedding`), searched via
  `db/vectors.knn`. `vec_distance_cosine` (sqlite-vec) when available, else
  Python cosine. NEVER reintroduce pgvector / `.cosine_distance()`.
- **sqlite-vec loading**: in `db/session.py` connect event, using
  `sqlalchemy.util.await_only` to drive aiosqlite's async `load_extension` in its
  worker thread. `vectors.vec_enabled` reflects success.
- **Sessions**: each MCP tool / CLI command opens `db.session.session_scope()`
  (commit on success, rollback on error). No FastAPI `Depends`.
- **Embeddings are generated inline** (`setup_service.generate_embeddings_inline`),
  not via background tasks — SQLite is single-writer and the data per connection
  is small.
- **No migrations**: `db/init.py` `create_all` on startup; dimension changes null
  stale vectors via the `qw_meta` table.
- **LLM is optional**: providers register independently
  (`llm/provider_registry._register_defaults`), so Ollama-only works without the
  `anthropic`/`openai` packages. Keyword-only context works with no provider at all.
- **Read-only**: `utils/sql_sanitizer.check_sql_safety` blocklist + connector-level
  enforcement (PG read-only txn; SQLite `mode=ro` + `PRAGMA query_only`).
- **`connection` args** accept name or id (`server._resolve_connection`).

## Commands

```bash
ruff check src/            # lint — clean and CI-gating (E501 in prompt templates +
                           #   setup_service seed data is waived via per-file-ignores)
python -m compileall src/  # byte-compile sanity
# quick MCP check: spawn `querywise-mcp` over stdio and call list_tools
```

## Sample data

`querywise seed-sample` runs `setup_service.auto_setup_sample_db()` (IFRS 9
banking glossary/metrics/dictionary/knowledge — ported verbatim from QueryWise).

**Zero-infra by default**: the sample *target* is a local SQLite file built by
`services/sample_data.build_sample_sqlite()` at `~/.querywise/sample_ifrs9.db`
(6 tables: counterparties, facilities, exposures, ecl_provisions, collateral,
staging_history; deterministic data, RNG seed 42). The schema column names must
match the keys in `setup_service.DICTIONARY_ENTRIES` / glossary / metric SQL — if
you add/rename a referenced column, update `sample_data.SCHEMA_SQL` too.

`auto_setup_sample_db` builds the file, then creates a `sqlite` connection named
`ifrs-db` (short/space-free for easy CLI use; replaces any stale connection of a
different type, and removes legacy-named ones in `LEGACY_CONNECTION_NAMES`). No
Postgres/Docker required. To target Postgres instead, set
`SAMPLE_DB_CONNECTOR_TYPE=postgresql` + `SAMPLE_DB_CONNECTION_STRING`.

Connection references (CLI/MCP `connection` arg) accept a UUID or a name, and
name matching is **case-insensitive** (`server._resolve_connection`).
