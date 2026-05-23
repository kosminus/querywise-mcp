"""Agent: Result Interpreter — converts query results into human-readable answers."""

import json
from dataclasses import dataclass

from querywise_mcp.llm.base_provider import BaseLLMProvider, LLMConfig, LLMMessage
from querywise_mcp.llm.prompts.interpreter_prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from querywise_mcp.llm.utils import repair_json


@dataclass
class InterpretationOutput:
    summary: str
    highlights: list[str]
    suggested_followups: list[str]


class ResultInterpreterAgent:
    def __init__(self, provider: BaseLLMProvider, config: LLMConfig):
        self.provider = provider
        self.config = config

    async def interpret(
        self,
        question: str,
        sql: str,
        columns: list[str],
        rows: list[list],
        row_count: int,
    ) -> InterpretationOutput:
        """Convert raw query results into a human-readable answer."""
        # Build a preview of the results (limit to avoid huge prompts)
        results_preview = _format_results_preview(columns, rows, max_rows=20)

        user_prompt = USER_PROMPT_TEMPLATE.format(
            question=question,
            sql=sql,
            row_count=row_count,
            columns=", ".join(columns),
            results_preview=results_preview,
        )

        messages = [
            LLMMessage(role="system", content=SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_prompt),
        ]

        response = await self.provider.complete(messages, self.config)

        try:
            parsed = json.loads(repair_json(response.content))
        except json.JSONDecodeError:
            # Fallback: use the raw response as the summary
            parsed = {
                "summary": response.content,
                "highlights": [],
                "suggested_followups": [],
            }

        return InterpretationOutput(
            summary=parsed.get("summary", response.content),
            highlights=parsed.get("highlights", []),
            suggested_followups=parsed.get("suggested_followups", []),
        )


def _format_results_preview(
    columns: list[str],
    rows: list[list],
    max_rows: int = 20,
) -> str:
    """Format query results as a readable text table for the LLM."""
    if not rows:
        return "(no results)"

    preview_rows = rows[:max_rows]
    lines = []

    # Header
    lines.append(" | ".join(columns))
    lines.append("-" * len(lines[0]))

    # Rows
    for row in preview_rows:
        lines.append(" | ".join(str(v) if v is not None else "NULL" for v in row))

    if len(rows) > max_rows:
        lines.append(f"... and {len(rows) - max_rows} more rows")

    return "\n".join(lines)
