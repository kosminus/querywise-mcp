#!/usr/bin/env python3
"""Bulk-load a connection's semantic layer from JSON or CSV files.

Adds business glossary terms, metrics, value-dictionary entries, sample
NL->SQL queries, and knowledge documents to an existing QueryWise connection,
then generates embeddings for the connection in one batch (so vector search
works immediately). It mirrors what ``querywise seed-sample`` does for the
IFRS 9 demo, but for any connection and any data you supply.

Quick start (loads the bundled IFRS 9 example into the sample connection):

    python scripts/load_semantic_layer.py ifrs-db \\
        --bundle scripts/seed_data/ifrs9_bundle.json

Per-kind files (JSON or CSV, mix freely):

    python scripts/load_semantic_layer.py mydb \\
        --glossary glossary.json \\
        --metrics  metrics.csv \\
        --dictionary value_map.csv \\
        --sample-queries examples.json \\
        --knowledge docs.json

Notes:
  * 'connection' is a connection name (case-insensitive) or id — same as the CLI.
  * Re-running is safe: existing rows are skipped by key (glossary term,
    metric_name, sample question, knowledge title, or dictionary value).
    Use --replace to overwrite matching rows instead.
  * Dictionary entries need the connection introspected first (so columns exist).
  * --no-embeddings stores rows with NULL vectors; keyword search still works
    and you can embed later with `querywise connections introspect <conn>`.

File formats — see scripts/seed_data/ for working examples. In CSV, list
columns (related_tables, dimensions, examples, related_columns) are
'|'-separated and the metric `filters` column is a JSON object string.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

# Run from a source checkout without needing an editable install.
SRC = Path(__file__).resolve().parent.parent / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))

from sqlalchemy import func, select  # noqa: E402

from querywise_mcp.db.init import init_db  # noqa: E402
from querywise_mcp.db.models.dictionary import DictionaryEntry  # noqa: E402
from querywise_mcp.db.models.glossary import GlossaryTerm  # noqa: E402
from querywise_mcp.db.models.knowledge import KnowledgeDocument  # noqa: E402
from querywise_mcp.db.models.metric import MetricDefinition  # noqa: E402
from querywise_mcp.db.models.sample_query import SampleQuery  # noqa: E402
from querywise_mcp.db.session import session_scope  # noqa: E402
from querywise_mcp.services import knowledge_service, semantic_service, setup_service  # noqa: E402

KINDS = ("glossary", "metrics", "dictionary", "sample_queries", "knowledge")


# --------------------------------------------------------------------------- #
# Cell coercion (JSON values pass through; CSV values arrive as strings)
# --------------------------------------------------------------------------- #
def _as_list(value) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return value or None
    parts = [p.strip() for p in str(value).split("|") if p.strip()]
    return parts or None


def _as_dict(value) -> dict | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value or None
    return json.loads(value)


def _as_int(value, default: int = 0) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _clean(value):
    """Empty CSV cells become None; everything else passes through."""
    return None if value == "" else value


# --------------------------------------------------------------------------- #
# File reading
# --------------------------------------------------------------------------- #
def read_records(path: Path) -> list[dict]:
    """Read a .json (list of objects) or .csv (list of rows) file."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text())
        if not isinstance(data, list):
            raise ValueError(f"{path}: expected a JSON array of objects")
        return data
    if suffix == ".csv":
        with path.open(newline="") as fh:
            return list(csv.DictReader(fh))
    raise ValueError(f"{path}: unsupported extension {suffix!r} (use .json or .csv)")


# --------------------------------------------------------------------------- #
# Per-kind loaders. Each returns a dict of counters.
# --------------------------------------------------------------------------- #
async def _existing_keys(db, model, key_col, connection_id) -> set:
    res = await db.execute(
        select(key_col).where(model.connection_id == connection_id)
    )
    return set(res.scalars().all())


async def load_glossary(db, cid, records, *, replace, dry_run) -> dict:
    existing = await _existing_keys(db, GlossaryTerm, GlossaryTerm.term, cid)
    counts = {"added": 0, "skipped": 0, "replaced": 0}
    for r in records:
        term = r["term"]
        if term in existing:
            if not replace:
                counts["skipped"] += 1
                continue
            if not dry_run:
                await db.execute(
                    GlossaryTerm.__table__.delete().where(
                        (GlossaryTerm.connection_id == cid)
                        & (GlossaryTerm.term == term)
                    )
                )
            counts["replaced"] += 1
        else:
            counts["added"] += 1
        if not dry_run:
            db.add(
                GlossaryTerm(
                    connection_id=cid,
                    term=term,
                    definition=r["definition"],
                    sql_expression=r["sql_expression"],
                    related_tables=_as_list(r.get("related_tables")),
                    related_columns=_as_list(r.get("related_columns")),
                    examples=_as_list(r.get("examples")) or [],
                )
            )
    if not dry_run:
        await db.flush()
    return counts


async def load_metrics(db, cid, records, *, replace, dry_run) -> dict:
    existing = await _existing_keys(db, MetricDefinition, MetricDefinition.metric_name, cid)
    counts = {"added": 0, "skipped": 0, "replaced": 0}
    for r in records:
        name = r["metric_name"]
        if name in existing:
            if not replace:
                counts["skipped"] += 1
                continue
            if not dry_run:
                await db.execute(
                    MetricDefinition.__table__.delete().where(
                        (MetricDefinition.connection_id == cid)
                        & (MetricDefinition.metric_name == name)
                    )
                )
            counts["replaced"] += 1
        else:
            counts["added"] += 1
        if not dry_run:
            db.add(
                MetricDefinition(
                    connection_id=cid,
                    metric_name=name,
                    display_name=r["display_name"],
                    description=_clean(r.get("description")),
                    sql_expression=r["sql_expression"],
                    aggregation_type=_clean(r.get("aggregation_type")),
                    related_tables=_as_list(r.get("related_tables")),
                    dimensions=_as_list(r.get("dimensions")),
                    filters=_as_dict(r.get("filters")) or {},
                )
            )
    if not dry_run:
        await db.flush()
    return counts


async def load_sample_queries(db, cid, records, *, replace, dry_run) -> dict:
    existing = await _existing_keys(
        db, SampleQuery, SampleQuery.natural_language, cid
    )
    counts = {"added": 0, "skipped": 0, "replaced": 0}
    for r in records:
        nl = r["natural_language"]
        if nl in existing:
            if not replace:
                counts["skipped"] += 1
                continue
            if not dry_run:
                await db.execute(
                    SampleQuery.__table__.delete().where(
                        (SampleQuery.connection_id == cid)
                        & (SampleQuery.natural_language == nl)
                    )
                )
            counts["replaced"] += 1
        else:
            counts["added"] += 1
        if not dry_run:
            db.add(
                SampleQuery(
                    connection_id=cid,
                    natural_language=nl,
                    sql_query=r["sql_query"],
                    description=_clean(r.get("description")),
                    is_validated=True,
                )
            )
    if not dry_run:
        await db.flush()
    return counts


async def load_dictionary(db, cid, records, *, replace, dry_run) -> dict:
    counts = {"added": 0, "skipped": 0, "replaced": 0, "missing_column": 0}
    for r in records:
        table_name, column_name = r["table_name"], r["column_name"]
        col_id = await semantic_service.resolve_column_id(
            db, cid, table_name, column_name
        )
        if not col_id:
            print(
                f"  ! column {table_name}.{column_name} not found "
                "(introspect the connection first) — skipping",
                file=sys.stderr,
            )
            counts["missing_column"] += 1
            continue

        raw_value = str(r["raw_value"])
        existing = await db.scalar(
            select(func.count())
            .select_from(DictionaryEntry)
            .where(
                DictionaryEntry.column_id == col_id,
                DictionaryEntry.raw_value == raw_value,
            )
        )
        if existing:
            if not replace:
                counts["skipped"] += 1
                continue
            if not dry_run:
                await db.execute(
                    DictionaryEntry.__table__.delete().where(
                        (DictionaryEntry.column_id == col_id)
                        & (DictionaryEntry.raw_value == raw_value)
                    )
                )
            counts["replaced"] += 1
        else:
            counts["added"] += 1
        if not dry_run:
            db.add(
                DictionaryEntry(
                    column_id=col_id,
                    raw_value=raw_value,
                    display_value=r["display_value"],
                    description=_clean(r.get("description")),
                    sort_order=_as_int(r.get("sort_order")),
                )
            )
    if not dry_run:
        await db.flush()
    return counts


async def load_knowledge(db, cid, records, *, replace, dry_run) -> dict:
    existing = await _existing_keys(
        db, KnowledgeDocument, KnowledgeDocument.title, cid
    )
    counts = {"added": 0, "skipped": 0, "replaced": 0}
    for r in records:
        title = r["title"]
        if title in existing:
            if not replace:
                counts["skipped"] += 1
                continue
            if not dry_run:
                docs = await db.execute(
                    select(KnowledgeDocument).where(
                        (KnowledgeDocument.connection_id == cid)
                        & (KnowledgeDocument.title == title)
                    )
                )
                for old in docs.scalars().all():
                    await db.delete(old)
                await db.flush()
            counts["replaced"] += 1
        else:
            counts["added"] += 1
        if not dry_run:
            await knowledge_service.import_document(
                db,
                connection_id=cid,
                title=title,
                content=r["content"],
                source_url=_clean(r.get("source_url")),
            )
    return counts


LOADERS = {
    "glossary": load_glossary,
    "metrics": load_metrics,
    "dictionary": load_dictionary,
    "sample_queries": load_sample_queries,
    "knowledge": load_knowledge,
}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def collect_sources(args) -> dict[str, list[dict]]:
    sources: dict[str, list[dict]] = {k: [] for k in KINDS}

    if args.bundle:
        bundle = json.loads(Path(args.bundle).read_text())
        if not isinstance(bundle, dict):
            raise ValueError(f"{args.bundle}: bundle must be a JSON object")
        for kind in KINDS:
            if bundle.get(kind):
                sources[kind].extend(bundle[kind])

    for kind in KINDS:
        path = getattr(args, kind)
        if path:
            sources[kind].extend(read_records(Path(path)))

    return {k: v for k, v in sources.items() if v}


async def run(args) -> int:
    sources = collect_sources(args)
    if not sources:
        print("Nothing to load. Pass --bundle and/or per-kind file options.")
        return 1

    await init_db()

    async with session_scope() as db:
        # Resolve by name or id, exactly like the CLI / MCP tools.
        from querywise_mcp.server import _resolve_connection

        conn = await _resolve_connection(db, args.connection)
        cid = conn.id
        print(f"Connection: {conn.name} ({cid}) [{conn.connector_type}]")
        if args.dry_run:
            print("DRY RUN — no changes will be written.\n")

        results: dict[str, dict] = {}
        for kind in KINDS:  # stable, dependency-friendly order
            if kind not in sources:
                continue
            loader = LOADERS[kind]
            results[kind] = await loader(
                db, cid, sources[kind], replace=args.replace, dry_run=args.dry_run
            )

        if args.dry_run:
            await db.rollback()

    print("\nSummary:")
    for kind, counts in results.items():
        detail = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
        print(f"  {kind:15s} {detail or 'no changes'}")

    if args.dry_run:
        print("\n(dry run — nothing written, embeddings skipped)")
        return 0

    if args.no_embeddings:
        print(
            "\nEmbeddings skipped (--no-embeddings). Rows use keyword search until "
            "embedded; run `querywise connections introspect <conn>` to embed."
        )
        return 0

    print("\nGenerating embeddings for the connection...")
    embedded = await setup_service.generate_embeddings_inline(cid)
    if embedded:
        print(f"  embedded {embedded} item(s).")
    else:
        print(
            "  no embeddings generated (no provider configured, or nothing pending) "
            "— keyword search will be used."
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bulk-load a connection's semantic layer from JSON/CSV files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("connection", help="Connection name (case-insensitive) or id.")
    p.add_argument("--bundle", help="Single JSON file with any of: " + ", ".join(KINDS))
    p.add_argument("--glossary", help="Glossary terms file (.json or .csv).")
    p.add_argument("--metrics", help="Metric definitions file (.json or .csv).")
    p.add_argument("--dictionary", help="Value-dictionary entries file (.json or .csv).")
    p.add_argument(
        "--sample-queries",
        dest="sample_queries",
        help="Sample NL->SQL pairs file (.json or .csv).",
    )
    p.add_argument("--knowledge", help="Knowledge documents file (.json or .csv).")
    p.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite existing rows that match by key (default: skip them).",
    )
    p.add_argument(
        "--no-embeddings",
        action="store_true",
        help="Insert rows without generating embeddings.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report counts without writing anything.",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()
    try:
        raise SystemExit(asyncio.run(run(args)))
    except SystemExit:
        raise
    except Exception as e:  # clean message instead of a traceback
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
