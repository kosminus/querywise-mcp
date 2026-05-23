import time
from typing import Any

import asyncpg

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


class PostgreSQLConnector(BaseConnector):
    connector_type = ConnectorType.POSTGRESQL

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    async def connect(self, connection_string: str, **kwargs: Any) -> None:
        try:
            self._pool = await asyncpg.create_pool(
                connection_string,
                min_size=1,
                max_size=kwargs.get("pool_size", 5),
                command_timeout=kwargs.get("command_timeout", 60),
            )
        except Exception as e:
            raise ConnectionError(str(e)) from e

    async def disconnect(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def test_connection(self) -> bool:
        if not self._pool:
            return False
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    async def introspect_schemas(self) -> list[str]:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT schema_name
                FROM information_schema.schemata
                WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                ORDER BY schema_name
                """
            )
            return [row["schema_name"] for row in rows]

    async def introspect_tables(self, schema: str = "public") -> list[TableInfo]:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            # Get tables and views
            table_rows = await conn.fetch(
                """
                SELECT
                    t.table_schema,
                    t.table_name,
                    t.table_type,
                    pg_catalog.obj_description(c.oid, 'pg_class') AS table_comment,
                    c.reltuples::bigint AS row_count_estimate
                FROM information_schema.tables t
                LEFT JOIN pg_catalog.pg_class c
                    ON c.relname = t.table_name
                    AND c.relnamespace = (
                        SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = t.table_schema
                    )
                WHERE t.table_schema = $1
                    AND t.table_type IN ('BASE TABLE', 'VIEW')
                ORDER BY t.table_name
                """,
                schema,
            )

            tables: list[TableInfo] = []
            for trow in table_rows:
                table_type = "table" if trow["table_type"] == "BASE TABLE" else "view"

                # Get columns
                col_rows = await conn.fetch(
                    """
                    SELECT
                        c.column_name,
                        c.data_type,
                        c.is_nullable,
                        c.column_default,
                        c.ordinal_position,
                        pg_catalog.col_description(
                            (SELECT oid FROM pg_catalog.pg_class WHERE relname = c.table_name
                             AND relnamespace = (SELECT oid FROM pg_catalog.pg_namespace
                                                 WHERE nspname = c.table_schema)),
                            c.ordinal_position
                        ) AS column_comment,
                        CASE WHEN pk.column_name IS NOT NULL THEN true ELSE false END AS is_pk
                    FROM information_schema.columns c
                    LEFT JOIN (
                        SELECT ku.column_name
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage ku
                            ON tc.constraint_name = ku.constraint_name
                            AND tc.table_schema = ku.table_schema
                        WHERE tc.constraint_type = 'PRIMARY KEY'
                            AND tc.table_schema = $1
                            AND tc.table_name = $2
                    ) pk ON pk.column_name = c.column_name
                    WHERE c.table_schema = $1 AND c.table_name = $2
                    ORDER BY c.ordinal_position
                    """,
                    schema,
                    trow["table_name"],
                )

                columns = [
                    ColumnInfo(
                        name=cr["column_name"],
                        data_type=cr["data_type"],
                        is_nullable=cr["is_nullable"] == "YES",
                        is_primary_key=cr["is_pk"],
                        default_value=cr["column_default"],
                        comment=cr["column_comment"],
                        ordinal_position=cr["ordinal_position"],
                    )
                    for cr in col_rows
                ]

                # Get foreign keys
                fk_rows = await conn.fetch(
                    """
                    SELECT
                        tc.constraint_name,
                        kcu.column_name,
                        ccu.table_schema AS referred_schema,
                        ccu.table_name AS referred_table,
                        ccu.column_name AS referred_column
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                        ON tc.constraint_name = kcu.constraint_name
                        AND tc.table_schema = kcu.table_schema
                    JOIN information_schema.constraint_column_usage ccu
                        ON ccu.constraint_name = tc.constraint_name
                        AND ccu.table_schema = tc.table_schema
                    WHERE tc.constraint_type = 'FOREIGN KEY'
                        AND tc.table_schema = $1
                        AND tc.table_name = $2
                    """,
                    schema,
                    trow["table_name"],
                )

                foreign_keys = [
                    ForeignKeyInfo(
                        constraint_name=fk["constraint_name"],
                        column_name=fk["column_name"],
                        referred_schema=fk["referred_schema"],
                        referred_table=fk["referred_table"],
                        referred_column=fk["referred_column"],
                    )
                    for fk in fk_rows
                ]

                tables.append(
                    TableInfo(
                        schema_name=schema,
                        table_name=trow["table_name"],
                        table_type=table_type,
                        comment=trow["table_comment"],
                        columns=columns,
                        foreign_keys=foreign_keys,
                        row_count_estimate=max(0, trow["row_count_estimate"] or 0),
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
        # Safety check first
        issues = check_sql_safety(sql)
        if issues:
            raise SQLSafetyError("; ".join(issues))

        assert self._pool is not None

        # Wrap with LIMIT if not already present
        wrapped_sql = sql.rstrip().rstrip(";")
        if "limit" not in wrapped_sql.lower():
            wrapped_sql = f"SELECT * FROM ({wrapped_sql}) AS _q LIMIT {max_rows + 1}"

        start = time.monotonic()
        try:
            async with self._pool.acquire() as conn:
                # Enforce read-only and timeout at transaction level
                async with conn.transaction(readonly=True):
                    await conn.execute(f"SET LOCAL statement_timeout = '{timeout_seconds * 1000}'")
                    rows = await conn.fetch(wrapped_sql)
        except asyncpg.QueryCanceledError as e:
            raise QueryTimeoutError(timeout_seconds) from e

        elapsed_ms = (time.monotonic() - start) * 1000
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]

        if not rows:
            return QueryResult(
                columns=[],
                column_types=[],
                rows=[],
                row_count=0,
                execution_time_ms=elapsed_ms,
                truncated=False,
            )

        columns = list(rows[0].keys())
        # Map asyncpg type OIDs to readable type names
        column_types = [_pg_type_name(rows[0].get(col)) for col in columns]
        result_rows = [list(row.values()) for row in rows]

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
        assert self._pool is not None
        # Use identifier quoting for safety
        query = f"""
            SELECT DISTINCT "{column}"
            FROM "{schema}"."{table}"
            WHERE "{column}" IS NOT NULL
            ORDER BY "{column}"
            LIMIT {limit}
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                rows = await conn.fetch(query)
        return [row[column] for row in rows]


def _pg_type_name(value: Any) -> str:
    """Infer a type name from a Python value returned by asyncpg."""
    if value is None:
        return "unknown"
    type_map = {
        int: "integer",
        float: "double precision",
        str: "text",
        bool: "boolean",
        bytes: "bytea",
    }
    for py_type, pg_name in type_map.items():
        if isinstance(value, py_type):
            return pg_name
    return type(value).__name__
