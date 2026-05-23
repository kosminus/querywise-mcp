# scripts/

Utilities for managing a connection's **semantic layer** (business glossary,
metrics, value dictionary, sample queries, knowledge docs) in bulk from files,
instead of one MCP/CLI call at a time.

| Script | What it does |
| --- | --- |
| [`load_semantic_layer.py`](load_semantic_layer.py) | Bulk-load glossary / metrics / dictionary / sample queries / knowledge into a connection from JSON or CSV, then embed the connection. |
| [`export_sample_seed.py`](export_sample_seed.py) | Regenerate the example files in [`seed_data/`](seed_data) from the canonical IFRS 9 seed constants. |

Both run from a source checkout (they add `../src` to `sys.path`) or against an
installed `querywise-mcp`. They write to the same SQLite metadata store the CLI
and MCP server use (`~/.querywise/querywise.db`, or `$QUERYWISE_HOME`).

## Bulk-loading a new database's semantic layer

1. **Create and introspect the connection first** (the loader adds metadata to
   an existing connection; dictionary entries need cached columns):

   ```bash
   querywise connections add mydb \
       --connector-type postgresql \
       -c "postgresql://user:pass@host/db"
   # `connections add` introspects by default.
   ```

2. **Write your files** (see [`seed_data/`](seed_data) for working examples), then load:

   ```bash
   # One bundle file with any subset of the five kinds:
   python scripts/load_semantic_layer.py mydb --bundle my_semantic_layer.json

   # …or per-kind files, mixing JSON and CSV freely:
   python scripts/load_semantic_layer.py mydb \
       --glossary glossary.json \
       --metrics  metrics.csv \
       --dictionary value_map.csv \
       --sample-queries examples.json \
       --knowledge docs.json
   ```

   After inserting, the loader generates embeddings for the whole connection in
   one batch (the same step `querywise seed-sample` runs), so vector search
   works immediately.

### Try it with the bundled IFRS 9 example

```bash
querywise init
querywise seed-sample          # builds the sample SQLite DB + `ifrs-db` connection
# `ifrs-db` is already seeded; load into any other connection you've introspected:
python scripts/load_semantic_layer.py <your-conn> --bundle scripts/seed_data/ifrs9_bundle.json
```

### Options

| Flag | Effect |
| --- | --- |
| `--bundle FILE` | Single JSON object with any of `glossary`, `metrics`, `dictionary`, `sample_queries`, `knowledge` keys. |
| `--glossary` / `--metrics` / `--dictionary` / `--sample-queries` / `--knowledge` | Per-kind file (`.json` array or `.csv`). Combine with `--bundle`. |
| `--replace` | Overwrite rows that match an existing key (default: skip them — re-running is safe). |
| `--no-embeddings` | Insert rows with NULL vectors (keyword search still works). Embed later via `querywise connections introspect <conn>`. |
| `--dry-run` | Parse files and print counts without writing anything. |

`connection` is a name (case-insensitive) or id — the same reference the CLI and
MCP tools accept. Re-running skips existing rows, matched by key: glossary
**term**, metric **metric_name**, sample-query **natural_language**, knowledge
**title**, and dictionary **(column, raw_value)**.

## File formats

JSON files are an array of objects (a bundle is one object keyed by kind). The
fields below map 1:1 to the MCP `add_*` tools and the ORM models. See
[`seed_data/`](seed_data) for complete examples.

**glossary** — `term`, `definition`, `sql_expression`, `related_tables[]`, `related_columns[]`, `examples[]`
**metrics** — `metric_name`, `display_name`, `description`, `sql_expression`, `aggregation_type`, `related_tables[]`, `dimensions[]`, `filters{}`
**dictionary** — `table_name`, `column_name`, `raw_value`, `display_value`, `description`, `sort_order`
**sample_queries** — `natural_language`, `sql_query`, `description`
**knowledge** — `title`, `content`, `source_url`

### CSV conventions

CSV uses the same column names as the JSON fields, with two encodings for
non-scalar cells (see [`seed_data/ifrs9_metrics.csv`](seed_data/ifrs9_metrics.csv)):

- **List columns** (`related_tables`, `related_columns`, `dimensions`, `examples`)
  are `|`-separated, e.g. `facility_type|segment|currency`.
- The metric **`filters`** column is a JSON object string, e.g. `{"stage": 1}`.

Empty cells become `NULL`/omitted.
