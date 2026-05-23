# querywise-mcp

An **MCP server** (and a CLI) that lets an LLM query your databases in natural
language through a **business semantic layer** — glossary, metric definitions,
data dictionary, knowledge base, and example queries — grounded against your
real schema.

It's a refactor of [QueryWise](../querywise) (a full-stack text-to-SQL app) into
a headless tool: no web UI, no Postgres requirement. The metadata store is an
embedded **SQLite + sqlite-vec** database, so the server runs from a single file.

## Two ways to use it

1. **As an MCP server** — Claude (or any MCP client) calls the tools. The
   recommended loop is:
   `get_semantic_context(connection, question)` → the model writes a read-only
   `SELECT` → `run_sql(connection, sql)`. The client's own model does the
   reasoning; the server provides grounded context + safe execution.
2. **As a CLI** — `querywise ask <connection> "<question>"` runs the full
   server-side NL→SQL pipeline (compose → validate → execute → interpret). This
   path needs an LLM provider key (or local Ollama).

The semantic layer, connectors, and execution are shared by both.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                 # core (SQLite store, sqlite-vec, Postgres + SQLite targets)
pip install -e ".[llm]"          # + Anthropic/OpenAI for `ask` and cloud embeddings
pip install -e ".[bigquery,databricks]"   # + extra target connectors
```

Configuration is via environment variables / `.env` (see `.env.example`). Zero
config works for keyword-only operation; add a key (or Ollama) to unlock
embeddings and the `ask` pipeline.

## Quick start (zero external infra)

```bash
querywise init                                   # create ~/.querywise/querywise.db
querywise connections add shop \
    --connector-type sqlite -c /path/to/app.db   # introspects + embeds
querywise context shop "revenue by segment"      # see the grounded context
querywise sql shop "SELECT ..."                  # run read-only SQL
querywise ask shop "what is total revenue by segment?"   # full pipeline (needs LLM)
```

## Run as an MCP server

```bash
querywise serve            # stdio (for Claude Desktop / Claude Code / Cursor)
querywise serve --http     # Streamable HTTP on MCP_HOST:MCP_PORT (default 127.0.0.1:8077)
```

### Register with Claude

First make sure the store the server will read is initialized (and optionally seeded):

```bash
querywise init                          # create ~/.querywise/querywise.db
querywise seed-sample                   # optional: zero-infra IFRS-9 sample → connection "ifrs-db"
```

> **Use an absolute command path.** MCP clients launch the server with a minimal
> `PATH`, so the bare `querywise-mcp` often won't resolve. Point at the entry
> point inside your venv, e.g. `/path/to/.venv/bin/querywise-mcp`.
>
> **The server won't read your repo `.env`.** It runs from the client's working
> directory, so pass everything it needs (`DATABASE_URL`, provider keys, model)
> in the `env` block below.

**Claude Desktop** — edit
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS), then
fully quit and reopen Claude Desktop:

```json
{
  "mcpServers": {
    "querywise": {
      "command": "/path/to/.venv/bin/querywise-mcp",
      "env": {
        "DEFAULT_LLM_PROVIDER": "ollama",
        "DATABASE_URL": "sqlite+aiosqlite:////Users/me/.querywise/querywise.db"
      }
    }
  }
}
```

**Claude Code** — one command:

```bash
claude mcp add querywise /path/to/.venv/bin/querywise-mcp \
  -e DEFAULT_LLM_PROVIDER=ollama \
  -e DATABASE_URL=sqlite+aiosqlite:////Users/me/.querywise/querywise.db
# verify: claude mcp list   (or /mcp inside a session)
```

Note the **four slashes** in the SQLite URL — `sqlite+aiosqlite://` (scheme) plus
the absolute path `/Users/me/...`.

**Why `DEFAULT_LLM_PROVIDER`?** It's a *server* setting, not your chat model.
Claude is the client LLM — it calls the granular tools and writes the answer, so
it needs no provider config. The server only uses a provider for two things:
**embeddings** (semantic search over your metadata — optional; degrades to
keyword-only without one) and the all-in-one **`ask`/`generate_sql`** tools
(which run their own LLM). Set it to `ollama` for key-free local embeddings, or
to `anthropic`/`openai` (with the matching `*_API_KEY` in `env`) if you want to
call the server-side `ask` tool. Omit it entirely to run keyword-only.

## MCP surface

**Tools** (25): `list_connections`, `create_connection`, `test_connection`,
`introspect_connection`, `delete_connection`, `list_tables`, `describe_table`,
`get_semantic_context`, `run_sql`, `generate_sql`, `ask`, `query_history`,
glossary/metric/dictionary/sample-query/knowledge management
(`list_*`/`add_*`/`delete_*`, plus `add_knowledge_url`).

**Resource**: `querywise://{connection}/schema` — the cached schema as text.
**Prompt**: `text_to_sql(connection, question)` — scaffolds the ground→write→run loop.

`connection` accepts a connection **name or id** everywhere.

## Connectors

| Target | Notes |
|---|---|
| SQLite | Read-only (`mode=ro`), zero infra. Great for local files + demos. |
| PostgreSQL | `asyncpg`, read-only transaction. |
| BigQuery | optional extra; service-account JSON in the connection string. |
| Databricks | optional extra; Unity Catalog or Hive metastore. |

All execution is read-only: a static SQL blocklist (DDL/DML/admin/injection)
plus connector-level read-only enforcement.

## How the semantic layer works

For each question the context builder selects minimal relevant context via a
hybrid of (1) vector similarity over embeddings, (2) keyword matching, and
(3) foreign-key expansion, then resolves glossary terms, metrics, dictionary
value-mappings, knowledge excerpts, and example queries into a structured prompt
block. Embeddings are stored as float32 BLOBs and searched with sqlite-vec's
`vec_distance_cosine`; if the extension can't load, search transparently falls
back to in-process cosine. With no embedding provider, it degrades to
keyword-only matching.

## Architecture

```
MCP client (Claude/…)  ──stdio/http──┐
CLI (`querywise ask`)  ──in-process──┤
                                     ▼
                          server.py / cli.py
                                     │
        ┌────────────────┬──────────┴───────────┬──────────────┐
        ▼                ▼                      ▼              ▼
   semantic/        services/               llm/          connectors/
 context builder   query pipeline      agents+providers  PG/SQLite/BQ/DBX
        │                │                      │              │
        └──────── db/ (SQLite + sqlite-vec metadata store) ────┘
```

## Development

```bash
ruff check src/
python -m compileall src/
```

The metadata schema is created on startup (`db/init.py`) — no migration tool.
Switching embedding providers/dimensions clears now-incompatible vectors
automatically.
