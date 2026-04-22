"""Shared backend contract tests.

Every backend must pass every test in here. Postgres is skipped unless the
``QBAGENT_TEST_POSTGRES_URL`` env var points at a writable database.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from qbagent.db.backend import Backend
from qbagent.db.duckdb_backend import DuckDBBackend
from qbagent.db.sqlite_backend import SQLiteBackend

POSTGRES_TEST_URL = os.environ.get("QBAGENT_TEST_POSTGRES_URL")


async def _make_duckdb(tmp_path: Path) -> Backend:
    b = DuckDBBackend(tmp_path / "test.duckdb")
    await b.connect()
    await b.initialize_schema()
    return b


async def _make_sqlite(tmp_path: Path) -> Backend:
    b = SQLiteBackend(tmp_path / "test.sqlite")
    await b.connect()
    await b.initialize_schema()
    return b


async def _make_postgres(_: Path) -> Backend:
    from qbagent.db.postgres_backend import PostgresBackend

    assert POSTGRES_TEST_URL is not None
    b = PostgresBackend(POSTGRES_TEST_URL)
    await b.connect()
    # Ensure a clean slate per test.
    await b.execute_write(
        "DROP TABLE IF EXISTS accounts, invoices, invoice_lines, bills, bill_lines, "
        "expenses, transactions, sync_log, memory_rules, query_log CASCADE"
    )
    await b.initialize_schema()
    return b


BACKEND_FACTORIES: list[tuple[str, object]] = [
    ("duckdb", _make_duckdb),
    ("sqlite", _make_sqlite),
]
if POSTGRES_TEST_URL:
    BACKEND_FACTORIES.append(("postgres", _make_postgres))


@pytest.fixture(params=[name for name, _ in BACKEND_FACTORIES], ids=lambda p: p)
async def backend(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Backend]:
    factory = dict(BACKEND_FACTORIES)[request.param]
    b = await factory(tmp_path)  # type: ignore[operator]
    try:
        yield b
    finally:
        await b.close()


# ---------------------------------------------------------------------------
# Schema + identity
# ---------------------------------------------------------------------------


async def test_initialize_schema_is_idempotent(backend: Backend) -> None:
    # Already initialized by the fixture; call again and ensure no error.
    await backend.initialize_schema()
    rows = await backend.execute_read("SELECT COUNT(*) AS n FROM accounts")
    assert rows[0]["n"] == 0


async def test_every_table_exists(backend: Backend) -> None:
    expected = {
        "accounts",
        "invoices",
        "invoice_lines",
        "bills",
        "bill_lines",
        "expenses",
        "transactions",
        "sync_log",
        "memory_rules",
        "query_log",
    }
    for table in expected:
        rows = await backend.execute_read(f"SELECT COUNT(*) AS n FROM {table}")
        assert rows[0]["n"] == 0, f"{table} should be empty after fresh init"


async def test_select_1(backend: Backend) -> None:
    rows = await backend.execute_read("SELECT 1 AS one")
    assert rows == [{"one": 1}]


# ---------------------------------------------------------------------------
# Type round-trip: DATE, TIMESTAMP, DECIMAL
# ---------------------------------------------------------------------------


async def test_round_trip_date_decimal_timestamp(backend: Backend) -> None:
    row = {
        "id": "inv-1",
        "doc_number": "1001",
        "customer_id": "c-1",
        "customer_name": "Acme",
        "invoice_date": date(2026, 1, 15),
        "due_date": date(2026, 2, 15),
        "total_amount": Decimal("1234.5678"),
        "balance": Decimal("1234.5678"),
        "status": "Sent",
        "currency_code": "USD",
        "memo": None,
        "created_at": datetime(2026, 1, 15, 9, 30, 0),
        "updated_at": datetime(2026, 1, 15, 9, 30, 0),
        "raw": "{}",
    }
    await backend.bulk_upsert("invoices", [row], pk_column="id")

    out = await backend.execute_read("SELECT * FROM invoices WHERE id = 'inv-1'")
    assert len(out) == 1
    got = out[0]
    assert got["invoice_date"] == date(2026, 1, 15)
    assert got["due_date"] == date(2026, 2, 15)
    assert got["created_at"] == datetime(2026, 1, 15, 9, 30, 0)
    assert got["total_amount"] == Decimal("1234.5678")
    assert got["balance"] == Decimal("1234.5678")


# ---------------------------------------------------------------------------
# bulk_upsert semantics
# ---------------------------------------------------------------------------


async def test_bulk_upsert_inserts_new_rows(backend: Backend) -> None:
    rows = [
        {
            "id": f"a-{i}",
            "name": f"Account {i}",
            "account_type": "Income",
            "account_sub_type": None,
            "parent_id": None,
            "active": 1,
            "currency_code": "USD",
            "current_balance": Decimal("0.00"),
            "created_at": datetime(2026, 1, 1, 0, 0),
            "updated_at": datetime(2026, 1, 1, 0, 0),
            "raw": "{}",
        }
        for i in range(3)
    ]
    count = await backend.bulk_upsert("accounts", rows, pk_column="id")
    assert count == 3

    check = await backend.execute_read("SELECT COUNT(*) AS n FROM accounts")
    assert check[0]["n"] == 3


async def test_bulk_upsert_updates_on_conflict(backend: Backend) -> None:
    base = {
        "id": "a-1",
        "name": "Original",
        "account_type": "Income",
        "account_sub_type": None,
        "parent_id": None,
        "active": 1,
        "currency_code": "USD",
        "current_balance": Decimal("100.00"),
        "created_at": datetime(2026, 1, 1, 0, 0),
        "updated_at": datetime(2026, 1, 1, 0, 0),
        "raw": "{}",
    }
    await backend.bulk_upsert("accounts", [base], pk_column="id")

    updated = dict(base, name="Renamed", current_balance=Decimal("500.00"))
    await backend.bulk_upsert("accounts", [updated], pk_column="id")

    rows = await backend.execute_read("SELECT name, current_balance FROM accounts WHERE id = 'a-1'")
    assert rows[0]["name"] == "Renamed"
    assert rows[0]["current_balance"] == Decimal("500.00")

    count = await backend.execute_read("SELECT COUNT(*) AS n FROM accounts")
    assert count[0]["n"] == 1, "upsert must not create a duplicate row"


async def test_bulk_upsert_empty_is_noop(backend: Backend) -> None:
    count = await backend.bulk_upsert("accounts", [], pk_column="id")
    assert count == 0


# ---------------------------------------------------------------------------
# Read-path shape
# ---------------------------------------------------------------------------


async def test_execute_read_returns_list_of_dicts_with_column_names(
    backend: Backend,
) -> None:
    await backend.bulk_upsert(
        "memory_rules",
        [
            {
                "id": "r-1",
                "text": "always filter out voided invoices",
                "source": "curated",
                "tags": '["voided"]',
                "created_at": datetime(2026, 1, 1),
                "active": 1,
            }
        ],
        pk_column="id",
    )
    rows = await backend.execute_read(
        "SELECT id, text, source, active FROM memory_rules ORDER BY id"
    )
    assert rows == [
        {"id": "r-1", "text": "always filter out voided invoices", "source": "curated", "active": 1}
    ]


async def test_execute_read_empty_result(backend: Backend) -> None:
    rows = await backend.execute_read("SELECT id FROM accounts WHERE id = 'nope'")
    assert rows == []
