"""Human-readable Markdown rendering of query results.

Used by the `ask` MCP tool and the `querywise ask` CLI command so both surfaces
render an answer identically: summary, highlights, executed SQL, metadata,
suggested follow-ups, and a bounded data preview.

(Adapted from mcp3's `query_database_nl` formatting.)
"""

from typing import Any


def format_ask_result(res: dict[str, Any], max_preview_rows: int = 20) -> str:
    """Render the dict returned by ``query_service.execute_nl_query`` as Markdown."""
    out: list[str] = [
        "### Answer Summary",
        res.get("summary") or "No natural-language summary generated.",
    ]

    highlights = res.get("highlights") or []
    if highlights:
        out.append("\n### Highlights")
        out.extend(f"- {hl}" for hl in highlights)

    out += [
        "\n### Executed SQL Query",
        f"```sql\n{res.get('final_sql') or res.get('generated_sql') or ''}\n```",
        "\n### Query Metadata",
        f"- Execution time: {(res.get('execution_time_ms') or 0):.2f} ms",
        f"- Row count: {res.get('row_count', 0)}",
        f"- LLM provider/model: {res.get('llm_provider')} ({res.get('llm_model')})",
        f"- Retries required: {res.get('retry_count', 0)}",
    ]

    followups = res.get("suggested_followups") or []
    if followups:
        out.append("\n### Suggested Follow-ups")
        out.extend(f"- {f}" for f in followups)

    rows = res.get("rows") or []
    cols = res.get("columns") or []
    if rows:
        out.append(f"\n### Query Data Preview (Top {max_preview_rows} rows)")
        header = " | ".join(str(c) for c in cols)
        out.append(header)
        out.append("-" * max(3, len(header)))
        for r in rows[:max_preview_rows]:
            out.append(" | ".join("NULL" if v is None else str(v) for v in r))
        if len(rows) > max_preview_rows:
            out.append(f"... and {len(rows) - max_preview_rows} more rows (truncated)")

    return "\n".join(out)
