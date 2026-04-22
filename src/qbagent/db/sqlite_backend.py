"""SQLite backend (aiosqlite).

SQLite has no native ``DECIMAL``, ``DATE``, or ``TIMESTAMP`` types; we register
converters/adapters so round-tripping preserves ``Decimal`` / ``date`` /
``datetime`` objects end-to-end.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite

from qbagent.db.backend import Backend, SchemaDialect

_ADAPTERS_REGISTERED = False


def _register_type_handlers() -> None:
    """Register adapters/converters once per process.

    Python 3.12 deprecated the default ``datetime`` adapter; we provide our own
    so the behavior is explicit and version-independent.
    """
    global _ADAPTERS_REGISTERED
    if _ADAPTERS_REGISTERED:
        return

    sqlite3.register_adapter(Decimal, str)
    sqlite3.register_adapter(date, lambda d: d.isoformat())
    sqlite3.register_adapter(datetime, lambda d: d.isoformat(sep=" "))

    sqlite3.register_converter("DECIMAL", lambda b: Decimal(b.decode()))
    sqlite3.register_converter("DATE", lambda b: date.fromisoformat(b.decode()))
    sqlite3.register_converter(
        "TIMESTAMP",
        lambda b: datetime.fromisoformat(b.decode().replace(" ", "T")),
    )
    _ADAPTERS_REGISTERED = True


class SQLiteBackend(Backend):
    dialect: SchemaDialect = "sqlite"

    def __init__(self, db_path: Path | str) -> None:
        _register_type_handlers()
        self._path = str(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(
            self._path,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        await conn.close()

    async def initialize_schema(self) -> None:
        conn = self._require_conn()
        async with self._lock:
            for stmt in self._load_ddl_statements():
                await conn.execute(stmt)
            await conn.commit()

    async def execute_read(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        conn = self._require_conn()
        async with self._lock, conn.execute(sql, params or ()) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    async def execute_write(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] | None = None,
    ) -> int:
        conn = self._require_conn()
        async with self._lock:
            cur = await conn.execute(sql, params or ())
            await conn.commit()
            return cur.rowcount

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
            update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
            sql = (
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                f"ON CONFLICT({pk_column}) DO UPDATE SET {update_clause}"
            )
        else:
            sql = (
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                f"ON CONFLICT({pk_column}) DO NOTHING"
            )
        values_list = [tuple(row.get(c) for c in columns) for row in rows]

        conn = self._require_conn()
        async with self._lock:
            await conn.executemany(sql, values_list)
            await conn.commit()
            return len(values_list)

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteBackend.connect() must be called before use.")
        return self._conn
