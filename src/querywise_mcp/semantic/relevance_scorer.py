"""Scores table/column relevance using a weighted hybrid approach."""

import re
from dataclasses import dataclass


@dataclass
class ScoredItem:
    """A scored table or glossary/metric item."""
    id: str
    name: str
    embedding_score: float = 0.0  # Cosine similarity (0-1)
    keyword_score: float = 0.0    # Keyword match (0 or 1)
    relationship_score: float = 0.0  # FK proximity boost (0 or 1)
    glossary_boost: float = 0.0   # Referenced by glossary/metric (0 or 0.2)

    @property
    def final_score(self) -> float:
        """Weighted combination: 50% embedding + 30% keyword + 20% relationship."""
        return (
            0.5 * self.embedding_score
            + 0.3 * self.keyword_score
            + 0.2 * self.relationship_score
            + self.glossary_boost
        )


def extract_keywords(question: str) -> list[str]:
    """Extract potential table/column name keywords from a NL question.

    Simple approach: split into words, filter out common English stop words,
    keep words that look like they could be identifiers.
    """
    stop_words = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "need", "dare", "ought",
        "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
        "as", "into", "through", "during", "before", "after", "above", "below",
        "between", "out", "off", "over", "under", "again", "further", "then",
        "once", "here", "there", "when", "where", "why", "how", "all", "each",
        "every", "both", "few", "more", "most", "other", "some", "such", "no",
        "not", "only", "own", "same", "so", "than", "too", "very", "just",
        "because", "but", "and", "or", "if", "while", "about", "what", "which",
        "who", "whom", "this", "that", "these", "those", "am", "it", "its",
        "i", "me", "my", "we", "our", "you", "your", "he", "him", "his",
        "she", "her", "they", "them", "their", "show", "give", "get", "tell",
        "find", "list", "display", "many", "much", "last", "first", "top",
        "bottom", "highest", "lowest", "total", "average", "count", "sum",
        "per", "number",
    }

    # Lowercase, split on non-alphanumeric
    words = re.findall(r"[a-z][a-z0-9_]*", question.lower())
    # Filter stop words and very short words
    keywords = [w for w in words if w not in stop_words and len(w) > 1]
    return keywords


def keyword_match_score(name: str, keywords: list[str]) -> float:
    """Score how well a table/column name matches extracted keywords.

    Returns 1.0 for exact match, 0.5 for substring match, 0.0 for no match.
    """
    name_lower = name.lower()
    # Check for exact match
    if name_lower in keywords:
        return 1.0
    # Check for substring match (e.g., "order" matches "orders" table)
    for kw in keywords:
        if kw in name_lower or name_lower in kw:
            return 0.5
    # Check if any keyword is a component of an underscore-separated name
    name_parts = set(name_lower.split("_"))
    for kw in keywords:
        if kw in name_parts:
            return 0.7
    return 0.0
