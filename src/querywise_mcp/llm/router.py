"""LLM Router — routes queries to different providers/models by complexity."""

import re
from enum import StrEnum

from querywise_mcp.config import settings
from querywise_mcp.llm.base_provider import BaseLLMProvider, LLMConfig
from querywise_mcp.llm.provider_registry import get_provider


class QueryComplexity(StrEnum):
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


def _build_default_routes() -> dict[QueryComplexity, dict]:
    """Build default routing rules from settings."""
    provider = settings.default_llm_provider
    # Use the Ollama-specific model name when provider is Ollama
    model = settings.ollama_model if provider == "ollama" else settings.default_llm_model
    return {
        QueryComplexity.SIMPLE: {
            "provider": provider,
            "model": model,
            "max_tokens": 2048,
        },
        QueryComplexity.MODERATE: {
            "provider": provider,
            "model": model,
            "max_tokens": 4096,
        },
        QueryComplexity.COMPLEX: {
            "provider": provider,
            "model": model,
            "max_tokens": 4096,
        },
    }


def estimate_complexity(question: str) -> QueryComplexity:
    """Estimate query complexity from the NL question.

    Heuristic based on signal keywords:
    - COMPLEX: compare, trend, correlation, over time, window, percentile, rank
    - MODERATE: group by, aggregate, join, per, by, total, average
    - SIMPLE: single entity lookups, "what is", "show me", basic filters
    """
    q_lower = question.lower()

    complex_signals = [
        r"\bcompare\b", r"\btrend\b", r"\bover time\b", r"\bcorrelat",
        r"\bpercentile\b", r"\brank\b", r"\bwindow\b", r"\brunning\b",
        r"\bcumulative\b", r"\byear.over.year\b", r"\bmonth.over.month\b",
        r"\bpivot\b", r"\bcohort\b", r"\bretention\b", r"\bforecast\b",
    ]
    for pattern in complex_signals:
        if re.search(pattern, q_lower):
            return QueryComplexity.COMPLEX

    moderate_signals = [
        r"\bgroup\b", r"\btotal\b", r"\baverage\b", r"\bsum\b",
        r"\bcount\b", r"\bper\b", r"\bby\b.*\bby\b",  # "by X by Y" = multi-dimension
        r"\bjoin\b", r"\bcombine\b", r"\bwith their\b",
        r"\btop \d+\b", r"\bbottom \d+\b", r"\bmost\b", r"\bleast\b",
        r"\bfilter\b", r"\bwhere\b", r"\bhaving\b",
    ]
    moderate_count = sum(1 for p in moderate_signals if re.search(p, q_lower))
    if moderate_count >= 2:
        return QueryComplexity.COMPLEX
    if moderate_count >= 1:
        return QueryComplexity.MODERATE

    return QueryComplexity.SIMPLE


def route(
    question: str,
    routes: dict | None = None,
) -> tuple[BaseLLMProvider, LLMConfig]:
    """Route a question to the appropriate LLM provider and model."""
    if routes is None:
        routes = _build_default_routes()

    complexity = estimate_complexity(question)
    route_config = routes.get(complexity, routes[QueryComplexity.MODERATE])

    provider = get_provider(route_config["provider"])
    config = LLMConfig(
        model=route_config["model"],
        temperature=0.0,
        max_tokens=route_config.get("max_tokens", 4096),
    )

    return provider, config
