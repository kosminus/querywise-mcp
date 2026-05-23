"""Shared utilities for LLM response parsing."""

import re


def repair_json(text: str) -> str:
    """Best-effort cleanup of malformed JSON from LLM responses.

    Handles common issues from smaller models (Ollama/local):
    - Markdown code fences wrapping JSON
    - Python-style True/False/None instead of true/false/null
    - Trailing commas before } or ]
    """
    s = text.strip()

    # Strip markdown code fences: ```json ... ``` or ``` ... ```
    match = re.match(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", s, re.DOTALL)
    if match:
        s = match.group(1).strip()

    # Fix Python-style booleans/None (only outside quoted strings)
    # Simple approach: replace whole-word occurrences
    s = re.sub(r"\bTrue\b", "true", s)
    s = re.sub(r"\bFalse\b", "false", s)
    s = re.sub(r"\bNone\b", "null", s)

    # Remove trailing commas before } or ]
    s = re.sub(r",\s*([}\]])", r"\1", s)

    return s
