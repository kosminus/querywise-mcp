import re

# Patterns that indicate dangerous SQL operations
_BLOCKED_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # DDL
    (re.compile(r"\bDROP\b", re.IGNORECASE), "DROP statements are not allowed"),
    (re.compile(r"\bALTER\b", re.IGNORECASE), "ALTER statements are not allowed"),
    (re.compile(r"\bCREATE\b", re.IGNORECASE), "CREATE statements are not allowed"),
    (re.compile(r"\bTRUNCATE\b", re.IGNORECASE), "TRUNCATE statements are not allowed"),
    # DML
    (re.compile(r"\bINSERT\b", re.IGNORECASE), "INSERT statements are not allowed"),
    (re.compile(r"\bUPDATE\b\s", re.IGNORECASE), "UPDATE statements are not allowed"),
    (re.compile(r"\bDELETE\b\s+FROM\b", re.IGNORECASE), "DELETE statements are not allowed"),
    (re.compile(r"\bMERGE\b", re.IGNORECASE), "MERGE statements are not allowed"),
    # Admin / dangerous
    (re.compile(r"\bGRANT\b", re.IGNORECASE), "GRANT statements are not allowed"),
    (re.compile(r"\bREVOKE\b", re.IGNORECASE), "REVOKE statements are not allowed"),
    (re.compile(r"\bCOPY\b", re.IGNORECASE), "COPY statements are not allowed"),
    (re.compile(r"\bEXECUTE\b", re.IGNORECASE), "EXECUTE statements are not allowed"),
    # Postgres-specific dangerous functions
    (re.compile(r"\bpg_sleep\b", re.IGNORECASE), "pg_sleep is not allowed"),
    (re.compile(r"\bpg_terminate_backend\b", re.IGNORECASE), "pg_terminate_backend is not allowed"),
    (re.compile(r"\bpg_cancel_backend\b", re.IGNORECASE), "pg_cancel_backend is not allowed"),
    (re.compile(r"\bdblink\b", re.IGNORECASE), "dblink is not allowed"),
    # BigQuery-specific
    (re.compile(r"\bEXPORT\s+DATA\b", re.IGNORECASE), "EXPORT DATA is not allowed"),
    (re.compile(r"\bLOAD\s+DATA\b", re.IGNORECASE), "LOAD DATA is not allowed"),
    # Databricks-specific
    (re.compile(r"\bCOPY\s+INTO\b", re.IGNORECASE), "COPY INTO is not allowed"),
    (re.compile(r"\bOPTIMIZE\b", re.IGNORECASE), "OPTIMIZE is not allowed"),
    (re.compile(r"\bVACUUM\b", re.IGNORECASE), "VACUUM is not allowed"),
    # Stacked queries (semicolon followed by another statement)
    (
        re.compile(r";\s*\b(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b", re.IGNORECASE),
        "Multiple statements (stacked queries) are not allowed",
    ),
]


# Match single/double-quoted strings and single/multi-line comments
_TOKEN_PATTERN = re.compile(
    r"('(?:\\.|''|[^'\\])*')"       # group 1: single-quoted string
    r"|(\"(?:\\.|\"\"|[^\"\\])*\")" # group 2: double-quoted string
    r"|(--[^\n]*)"                   # group 3: single-line comment
    r"|(/\*.*?\*/)",                 # group 4: multi-line comment
    re.DOTALL
)


def check_sql_safety(sql: str) -> list[str]:
    """Check SQL for dangerous patterns. Returns list of issues found, empty if safe."""
    issues: list[str] = []
    # Strip comments and string literals before checking
    cleaned = _sanitize_query_text(sql)
    for pattern, message in _BLOCKED_PATTERNS:
        if pattern.search(cleaned):
            issues.append(message)
    return issues


def _sanitize_query_text(sql: str) -> str:
    """Strip comments and string literals from SQL text to prevent bypasses and false positives.

    Replaces single-quoted strings with '' and double-quoted strings with "", and
    replaces comments with a single space to preserve word boundaries.
    """
    def replace(match: re.Match) -> str:
        if match.group(1):
            return "''"
        elif match.group(2):
            return '""'
        else:
            return " "

    return _TOKEN_PATTERN.sub(replace, sql)

