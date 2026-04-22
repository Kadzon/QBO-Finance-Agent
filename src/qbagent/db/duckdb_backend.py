"""DuckDB backend.

DuckDB ships with a synchronous driver; every call is off-loaded to a worker
thread so the rest of the app can stay on the event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import duckdb

from qbagent.db.backend import Backend, SchemaDialect


class DuckDBBackend(Backend):
    dialect: SchemaDialect = "duckdb"

    def __init__(self, db_path: Path | str) -> None:
        self._path = str(db_path)
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._lock = asyncio.Lock()

    # --- Lifecycle --------------------------------------------------------
    async def connect(self) -> None:
        if self._conn is not None:
            return
        self._conn = await asyncio.to_thread(duckdb.connect, self._path)

    async def close(self) -> None:
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        await asyncio.to_thread(conn.close)

    async def initialize_schema(self) -> None:
        self._require_conn()
        for stmt in self._load_ddl_statements():
            await self._execute(stmt)

    # --- Query execution --------------------------------------------------
    async def execute_read(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        conn = self._require_conn()

        def _run() -> list[dict[str, Any]]:
            cur = conn.execute(sql, params) if params is not None else conn.execute(sql)
            columns = [d[0] for d in cur.description or []]
            return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]

        async with self._lock:
            return await asyncio.to_thread(_run)

    async def execute_write(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] | None = None,
    ) -> int:
        return await self._execute(sql, params)

    async def bulk_upsert(
        self,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        pk_column: str,
    ) -> int:
        if not rows:
            return 0
        columns = list(rows[0].keys())
        placeholders = ", ".join(["?"] * len(columns))
        col_list = ", ".join(columns)
        update_cols = [c for c in columns if c != pk_column]
        if update_cols:
            update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
            sql = (
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                f"ON CONFLICT ({pk_column}) DO UPDATE SET {update_clause}"
            )
        else:
            sql = (
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                f"ON CONFLICT ({pk_column}) DO NOTHING"
            )

        conn = self._require_conn()
        values_list = [[row.get(c) for c in columns] for row in rows]

        def _run() -> int:
            # DuckDB's executemany does not support ON CONFLICT in a single
            # call reliably across all versions — do it row by row inside a
            # transaction for atomicity.
            conn.execute("BEGIN")
            try:
                for values in values_list:
                    conn.execute(sql, values)
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return len(values_list)

        async with self._lock:
            return await asyncio.to_thread(_run)

    # --- Internals --------------------------------------------------------
    async def _execute(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] | None = None,
    ) -> int:
        conn = self._require_conn()

        def _run() -> int:
            cur = conn.execute(sql, params) if params is not None else conn.execute(sql)
            # DuckDB does not always populate rowcount for DDL; return 0 in that case.
            return getattr(cur, "rowcount", 0) or 0

        async with self._lock:
            return await asyncio.to_thread(_run)

    def _require_conn(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            raise RuntimeError("DuckDBBackend.connect() must be called before use.")
        return self._conn
