#!/usr/bin/env python3
"""Regenerate the example semantic-layer files under ``scripts/seed_data/``.

The files are derived verbatim from the canonical IFRS 9 seed constants in
``querywise_mcp.services.setup_service`` so the examples never drift from the
data that ``querywise seed-sample`` loads. Run this whenever those constants
change:

    python scripts/export_sample_seed.py

The output files are what ``load_semantic_layer.py`` consumes, so they double
as the format reference for your own connections.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

# Run from a source checkout without needing an editable install.
SRC = Path(__file__).resolve().parent.parent / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))

from querywise_mcp.services import setup_service  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "seed_data"

# The seed has no sample NL->SQL pairs; provide a couple so the format is
# documented and the bundle is complete.
SAMPLE_QUERIES = [
    {
        "natural_language": "What is the total ECL by stage?",
        "sql_query": (
            "SELECT stage, SUM(ecl_lifetime) AS total_ecl "
            "FROM ecl_provisions GROUP BY stage ORDER BY stage"
        ),
        "description": "Total lifetime ECL grouped by IFRS 9 stage.",
    },
    {
        "natural_language": "Show the NPL ratio for the corporate segment",
        "sql_query": (
            "SELECT SUM(e.ead) FILTER (WHERE e.stage = 3) "
            "/ NULLIF(SUM(e.ead), 0) AS npl_ratio "
            "FROM exposures e JOIN facilities f ON f.id = e.facility_id "
            "JOIN counterparties c ON c.id = f.counterparty_id "
            "WHERE c.segment = 'corporate'"
        ),
        "description": "Stage 3 EAD as a share of total EAD, corporate only.",
    },
]


def _flatten_dictionary() -> list[dict]:
    rows: list[dict] = []
    for (table_name, column_name), entries in setup_service.DICTIONARY_ENTRIES.items():
        for entry in entries:
            rows.append({"table_name": table_name, "column_name": column_name, **entry})
    return rows


def _write_json(name: str, data: object) -> None:
    path = OUT_DIR / name
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"  wrote {path.relative_to(OUT_DIR.parent.parent)}")


def _write_metrics_csv(name: str, metrics: list[dict]) -> None:
    """Demonstrate the CSV format: list cells are '|'-joined, filters is JSON."""
    path = OUT_DIR / name
    fields = [
        "metric_name",
        "display_name",
        "description",
        "sql_expression",
        "aggregation_type",
        "related_tables",
        "dimensions",
        "filters",
    ]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for m in metrics:
            writer.writerow(
                {
                    "metric_name": m.get("metric_name", ""),
                    "display_name": m.get("display_name", ""),
                    "description": m.get("description", ""),
                    "sql_expression": m.get("sql_expression", ""),
                    "aggregation_type": m.get("aggregation_type", ""),
                    "related_tables": "|".join(m.get("related_tables") or []),
                    "dimensions": "|".join(m.get("dimensions") or []),
                    "filters": json.dumps(m["filters"]) if m.get("filters") else "",
                }
            )
    print(f"  wrote {path.relative_to(OUT_DIR.parent.parent)}")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Exporting IFRS 9 sample seed -> {OUT_DIR}")

    glossary = setup_service.GLOSSARY_TERMS
    metrics = setup_service.METRICS
    dictionary = _flatten_dictionary()
    knowledge = [setup_service.KNOWLEDGE_DOCUMENT]

    _write_json("ifrs9_glossary.json", glossary)
    _write_json("ifrs9_metrics.json", metrics)
    _write_metrics_csv("ifrs9_metrics.csv", metrics)
    _write_json("ifrs9_dictionary.json", dictionary)
    _write_json("ifrs9_sample_queries.json", SAMPLE_QUERIES)
    _write_json("ifrs9_knowledge.json", knowledge)

    # A single-file bundle the loader can ingest in one pass.
    _write_json(
        "ifrs9_bundle.json",
        {
            "glossary": glossary,
            "metrics": metrics,
            "dictionary": dictionary,
            "sample_queries": SAMPLE_QUERIES,
            "knowledge": knowledge,
        },
    )
    print("Done.")


if __name__ == "__main__":
    main()
