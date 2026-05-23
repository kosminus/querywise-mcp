"""Read-only SQLite target connector.

Lets the server query a local SQLite file with zero external infrastructure —
useful for demos, local analytics files, and testing the full pipeline. The
connection is opened read-only (URI ``mode=ro`` + ``PRAGMA query_only``) and the
shared SQL blocklist still applies.

connection_string accepts a path (``/data/app.db``) or a ``sqlite:///`` URL.
"""

import asyncio
import time
from typing import Any

import aiosqlite

from querywise_mcp.connectors.base_connector import (
    BaseConnector,
    ColumnInfo,
    ConnectorType,
    ForeignKeyInfo,
    QueryResult,
    TableInfo,
)
from querywise_mcp.core.exceptions import ConnectionError, QueryTimeoutError, SQLSafetyError
from querywise_mcp.utils.sql_sanitizer import check_sql_safety


def _path_from_connection_string(connection_string: str) -> str:
    """Accept a filesystem path or a sqlite URL and return the file path."""
    s = connection_string.strip()
    for prefix in ("sqlite+aiosqlite://", "sqlite://"):
        if s.startswith(prefix):
            return s[len(prefix):]  # 'sqlite:///abs/path' -> '/abs/path'
    return s


class SQLiteConnector(BaseConnector):
    connector_type = ConnectorType.SQLITE

    def __init__(self) -> None:
        self._path: str | None = None

    async def connect(self, connection_string: str, **kwargs: Any) -> None:
        self._path = _path_from_connection_string(connection_string)
        try:
            async with self._ro() as conn:
                await conn.execute("SELECT 1")
        except Exception as e:
            raise ConnectionError(str(e)) from e

    def _ro(self):
        # Read-only URI connection; query_only as a second guard.
        uri = f"file:{self._path}?mode=ro"
        return aiosqlite.connect(uri, uri=True)

    async def disconnect(self) -> None:
        self._path = None

    async def test_connection(self) -> bool:
        try:
            async with self._ro() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def introspect_schemas(self) -> list[str]:
        return ["main"]

    async def introspect_tables(self, schema: str = "main") -> list[TableInfo]:
        assert self._path is not None
        tables: list[TableInfo] = []
        async with self._ro() as conn:
            cur = await conn.execute(
                "SELECT name, type FROM sqlite_master "
                "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
            objects = await cur.fetchall()

            for name, obj_type in objects:
                col_cur = await conn.execute(f'PRAGMA table_info("{name}")')
                col_rows = await col_cur.fetchall()
                columns = [
                    ColumnInfo(
                        name=cr[1],
                        data_type=cr[2] or "TEXT",
                        is_nullable=not cr[3],
                        is_primary_key=bool(cr[5]),
                        default_value=cr[4],
                        comment=None,
                        ordinal_position=cr[0] + 1,
                    )
                    for cr in col_rows
                ]

                fk_cur = await conn.execute(f'PRAGMA foreign_key_list("{name}")')
                fk_rows = await fk_cur.fetchall()
                foreign_keys = [
                    ForeignKeyInfo(
                        constraint_name=f"{name}_fk_{fr[0]}",
                        column_name=fr[3],
                        referred_schema="main",
                        referred_table=fr[2],
                        referred_column=fr[4],
                    )
                    for fr in fk_rows
                ]

                tables.append(
                    TableInfo(
                        schema_name="main",
                        table_name=name,
                        table_type="table" if obj_type == "table" else "view",
                        comment=None,
                        columns=columns,
                        foreign_keys=foreign_keys,
                        row_count_estimate=None,
                    )
                )
        return tables

    async def execute_query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: int = 30,
        max_rows: int = 1000,
    ) -> QueryResult:
        issues = check_sql_safety(sql)
        if issues:
            raise SQLSafetyError("; ".join(issues))
        assert self._path is not None

        wrapped_sql = sql.rstrip().rstrip(";")
        if "limit" not in wrapped_sql.lower():
            wrapped_sql = f"SELECT * FROM ({wrapped_sql}) AS _q LIMIT {max_rows + 1}"

        start = time.monotonic()
        try:
            async with self._ro() as conn:
                await conn.execute("PRAGMA query_only=ON")
                cur = await asyncio.wait_for(
                    conn.execute(wrapped_sql), timeout=timeout_seconds
                )
                rows = await cur.fetchall()
                description = cur.description
        except TimeoutError as e:
            raise QueryTimeoutError(timeout_seconds) from e

        elapsed_ms = (time.monotonic() - start) * 1000
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]

        if not description:
            return QueryResult(
                columns=[], column_types=[], rows=[], row_count=0,
                execution_time_ms=elapsed_ms, truncated=False,
            )

        columns = [d[0] for d in description]
        result_rows = [list(r) for r in rows]
        column_types = [_infer_type(result_rows[0][i]) if result_rows else "unknown"
                        for i in range(len(columns))]
        return QueryResult(
            columns=columns,
            column_types=column_types,
            rows=result_rows,
            row_count=len(result_rows),
            execution_time_ms=elapsed_ms,
            truncated=truncated,
        )

    async def get_sample_values(
        self, schema: str, table: str, column: str, limit: int = 20
    ) -> list[Any]:
        assert self._path is not None
        query = (
            f'SELECT DISTINCT "{column}" FROM "{table}" '
            f'WHERE "{column}" IS NOT NULL ORDER BY "{column}" LIMIT {limit}'
        )
        async with self._ro() as conn:
            cur = await conn.execute(query)
            rows = await cur.fetchall()
        return [r[0] for r in rows]


def _infer_type(value: Any) -> str:
    if value is None:
        return "unknown"
    return {int: "integer", float: "real", str: "text", bytes: "blob"}.get(
        type(value), type(value).__name__
    )
