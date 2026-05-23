"""Agent: Error Handler — diagnoses SQL errors and produces corrected SQL."""

import json
from dataclasses import dataclass

from querywise_mcp.llm.base_provider import BaseLLMProvider, LLMConfig, LLMMessage
from querywise_mcp.llm.utils import repair_json


@dataclass
class ErrorResolution:
    corrected_sql: str
    explanation: str
    should_retry: bool


ERROR_SYSTEM_PROMPT = """You are a SQL debugging expert. A SQL query failed to execute against a PostgreSQL database.
Your job is to analyze the error and produce a corrected SQL query.

You will receive:
1. The original natural language question
2. The SQL that failed
3. The error message from the database
4. The database schema context
5. Any previous failed attempts

Rules:
- Fix ONLY the issue causing the error
- Keep the intent of the original query
- Generate ONLY SELECT statements
- If the error is unrecoverable (e.g., the user is asking about data that doesn't exist), set should_retry to false

Output format:
{
  "corrected_sql": "THE FIXED SQL",
  "explanation": "What was wrong and how you fixed it",
  "should_retry": true/false
}"""


class ErrorHandlerAgent:
    MAX_RETRIES = 3

    def __init__(self, provider: BaseLLMProvider, config: LLMConfig):
        self.provider = provider
        self.config = config

    async def handle_error(
        self,
        question: str,
        failed_sql: str,
        error_message: str,
        schema_context: str,
        attempt_number: int = 1,
        previous_attempts: list[str] | None = None,
    ) -> ErrorResolution:
        """Analyze a SQL error and produce corrected SQL."""
        if attempt_number > self.MAX_RETRIES:
            return ErrorResolution(
                corrected_sql="",
                explanation=f"Max retries ({self.MAX_RETRIES}) exceeded",
                should_retry=False,
            )

        previous = ""
        if previous_attempts:
            previous = "\n\nPrevious failed attempts:\n" + "\n---\n".join(previous_attempts)

        user_prompt = f"""Original question: "{question}"

Failed SQL (attempt {attempt_number}):
{failed_sql}

Error from database:
{error_message}

Database schema:
{schema_context}
{previous}

Analyze the error and provide a corrected SQL query. Respond with JSON: corrected_sql, explanation, should_retry."""

        messages = [
            LLMMessage(role="system", content=ERROR_SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_prompt),
        ]

        response = await self.provider.complete(messages, self.config)

        try:
            parsed = json.loads(repair_json(response.content))
        except json.JSONDecodeError:
            # Try to extract SQL from the response
            import re

            match = re.search(r"```sql?\s*\n?(.*?)\n?```", response.content, re.DOTALL)
            sql = match.group(1).strip() if match else ""
            parsed = {
                "corrected_sql": sql,
                "explanation": response.content,
                "should_retry": bool(sql),
            }

        return ErrorResolution(
            corrected_sql=parsed.get("corrected_sql", ""),
            explanation=parsed.get("explanation", ""),
            should_retry=parsed.get("should_retry", False),
        )
