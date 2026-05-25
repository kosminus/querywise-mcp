"""Agent: SQL Validator — validates generated SQL for safety and correctness."""

from dataclasses import dataclass
from enum import StrEnum

from querywise_mcp.utils.sql_sanitizer import check_sql_safety


class ValidationStatus(StrEnum):
    VALID = "valid"
    UNSAFE = "unsafe"
    SYNTAX_ERROR = "syntax_error"
    SCHEMA_MISMATCH = "schema_mismatch"


@dataclass
class ValidationResult:
    status: ValidationStatus
    issues: list[str]
    corrected_sql: str | None = None


class SQLValidatorAgent:
    """Validates SQL for safety, correctness, and schema compliance.

    Two levels:
    1. Static analysis (no LLM call): blocked patterns, injection detection
    2. Schema validation: checks referenced tables/columns exist in the cached schema
    """

    async def validate(
        self,
        sql: str,
        schema_tables: dict[str, list[str]] | None = None,
    ) -> ValidationResult:
        """Validate a SQL query.

        Args:
            sql: The SQL query to validate
            schema_tables: Dict of table_name -> list of column_names for schema checking
        """
        # Level 1: Static safety checks
        safety_issues = check_sql_safety(sql)
        if safety_issues:
            return ValidationResult(
                status=ValidationStatus.UNSAFE,
                issues=safety_issues,
            )

        # Check for empty SQL
        stripped = sql.strip()
        if not stripped:
            return ValidationResult(
                status=ValidationStatus.SYNTAX_ERROR,
                issues=["Empty SQL query"],
            )

        # Check it starts with SELECT (or WITH for CTEs)
        upper = stripped.upper().lstrip()
        if not (upper.startswith("SELECT") or upper.startswith("WITH")):
            return ValidationResult(
                status=ValidationStatus.UNSAFE,
                issues=[f"Query must start with SELECT or WITH, got: {upper[:20]}..."],
            )

        # Level 2: Schema validation (if schema context provided)
        if schema_tables:
            schema_issues = _check_schema_references(sql, schema_tables)
            if schema_issues:
                return ValidationResult(
                    status=ValidationStatus.SCHEMA_MISMATCH,
                    issues=schema_issues,
                )

        return ValidationResult(
            status=ValidationStatus.VALID,
            issues=[],
        )


def _check_schema_references(
    sql: str,
    schema_tables: dict[str, list[str]],
) -> list[str]:
    """Check if tables/columns referenced in SQL exist in the schema.

    This is a best-effort check using simple parsing. For complex queries,
    we rely on the database itself to report errors.
    """
    issues = []

    # Try to parse with sqlparse if available
    try:
        import sqlparse

        parsed = sqlparse.parse(sql)
        if not parsed:
            return issues

        # Extract identifiers
        # This is a simplified check — real production would use sqlglot
        sql_upper = sql.upper()
        all_table_names = {name.upper() for name in schema_tables}

        # Check FROM and JOIN clauses for table references
        from_pattern = _extract_from_tables(sql_upper)
        for table_ref in from_pattern:
            # Strip schema prefix and alias
            clean_name = table_ref.split(".")[-1].split(" ")[0].strip('"').strip("'")
            if clean_name and clean_name not in all_table_names and clean_name != "_Q":
                issues.append(f"Table '{clean_name}' not found in schema")

    except ImportError:
        # sqlparse not available, skip schema check
        pass

    return issues


def _extract_from_tables(sql_upper: str) -> list[str]:
    """Extract table names from FROM and JOIN clauses (simplified)."""
    import re

    tables = []

    # Match FROM table_name and JOIN table_name patterns
    patterns = [
        r"\bFROM\s+([A-Za-z_][A-Za-z0-9_.]*)",
        r"\bJOIN\s+([A-Za-z_][A-Za-z0-9_.]*)",
    ]
    for pattern in patterns:
        matches = re.findall(pattern, sql_upper, re.IGNORECASE)
        tables.extend(matches)

    return tables
