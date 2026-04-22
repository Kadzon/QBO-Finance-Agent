"""Tests for SyncRunner against a real DuckDB backend and a FakeMCPClient."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from qbagent.db.backend import Backend
from qbagent.db.duckdb_backend import DuckDBBackend
from qbagent.sync.mcp_client import FakeMCPClient
from qbagent.sync.sync_runner import SYNC_ORDER, SyncRunner

FIXTURES = Path(__file__).parent / "fixtures" / "mcp_responses"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
async def backend(tmp_path: Path) -> AsyncIterator[Backend]:
    b = DuckDBBackend(tmp_path / "sync.duckdb")
    await b.connect()
    await b.initialize_schema()
    try:
        yield b
    finally:
        await b.close()


def _seed_fixture_entities() -> dict[str, list[dict[str, Any]]]:
    return {
        "accounts": [_load("account_income.json")],
        "invoices": [
            _load("invoice_partially_paid.json"),
            _load("invoice_paid.json"),
            _load("invoice_voided.json"),
        ],
        "bills": [_load("bill_open.json")],
        "expenses": [_load("purchase_credit_card.json")],
        "transactions": [_load("transaction_row.json")],
    }


# ---------------------------------------------------------------------------
# sync_full
# ---------------------------------------------------------------------------


async def test_sync_full_populates_every_table(backend: Backend) -> None:
    client = FakeMCPClient(_seed_fixture_entities())
    runner = SyncRunner(client, backend)

    report = await runner.sync_full()

    assert report.ok
    assert [r.entity for r in report.results] == list(SYNC_ORDER)
    assert all(r.status == "success" for r in report.results)

    # Rows actually landed.
    assert (await backend.execute_read("SELECT COUNT(*) AS n FROM accounts"))[0]["n"] == 1
    assert (await backend.execute_read("SELECT COUNT(*) AS n FROM invoices"))[0]["n"] == 3
    assert (await backend.execute_read("SELECT COUNT(*) AS n FROM invoice_lines"))[0]["n"] == 4
    assert (await backend.execute_read("SELECT COUNT(*) AS n FROM bills"))[0]["n"] == 1
    assert (await backend.execute_read("SELECT COUNT(*) AS n FROM bill_lines"))[0]["n"] == 2
    assert (await backend.execute_read("SELECT COUNT(*) AS n FROM expenses"))[0]["n"] == 1
    assert (await backend.execute_read("SELECT COUNT(*) AS n FROM transactions"))[0]["n"] == 1


async def test_sync_full_respects_entity_scope(backend: Backend) -> None:
    client = FakeMCPClient(_seed_fixture_entities())
    runner = SyncRunner(client, backend)

    report = await runner.sync_full(entities=["invoices"])

    assert [r.entity for r in report.results] == ["invoices"]
    # Other entities untouched.
    assert (await backend.execute_read("SELECT COUNT(*) AS n FROM accounts"))[0]["n"] == 0
    assert (await backend.execute_read("SELECT COUNT(*) AS n FROM invoices"))[0]["n"] == 3


async def test_sync_is_idempotent(backend: Backend) -> None:
    client = FakeMCPClient(_seed_fixture_entities())
    runner = SyncRunner(client, backend)

    await runner.sync_full()
    await runner.sync_full()  # second pass must not duplicate rows

    assert (await backend.execute_read("SELECT COUNT(*) AS n FROM invoices"))[0]["n"] == 3
    assert (await backend.execute_read("SELECT COUNT(*) AS n FROM invoice_lines"))[0]["n"] == 4


# ---------------------------------------------------------------------------
# Per-entity failure isolation
# ---------------------------------------------------------------------------


async def test_one_entity_failure_does_not_block_others(backend: Backend) -> None:
    client = FakeMCPClient(_seed_fixture_entities())
    client.fail_on_list("bills", RuntimeError("MCP server hiccup"))
    runner = SyncRunner(client, backend)

    report = await runner.sync_full()

    statuses = {r.entity: r.status for r in report.results}
    assert statuses["accounts"] == "success"
    assert statuses["bills"] == "error"
    assert statuses["invoices"] == "success"
    assert statuses["expenses"] == "success"
    assert statuses["transactions"] == "success"
    assert not report.ok

    # sync_log records the error status with a message.
    rows = await backend.execute_read(
        "SELECT entity, last_sync_status, last_error FROM sync_log WHERE entity = 'bills'"
    )
    assert rows[0]["last_sync_status"] == "error"
    assert "hiccup" in (rows[0]["last_error"] or "")


async def test_unknown_entity_rejected(backend: Backend) -> None:
    client = FakeMCPClient()
    runner = SyncRunner(client, backend)
    with pytest.raises(ValueError, match="Unknown entities"):
        await runner.sync_full(entities=["widgets"])


# ---------------------------------------------------------------------------
# Incremental
# ---------------------------------------------------------------------------


async def test_sync_incremental_uses_cursor(backend: Backend) -> None:
    client = FakeMCPClient(
        {"accounts": [_load("account_income.json")]},
        cursors={"accounts": "cursor-v1"},
    )
    runner = SyncRunner(client, backend)

    await runner.sync_incremental(entities=["accounts"])

    cursor_row = await backend.execute_read(
        "SELECT last_cursor, last_sync_status FROM sync_log WHERE entity = 'accounts'"
    )
    assert cursor_row[0]["last_cursor"] == "cursor-v1"
    assert cursor_row[0]["last_sync_status"] == "success"

    # Next run passes the cursor through.
    await runner.sync_incremental(entities=["accounts"])
    call_entries = [c for c in client.calls if c[0] == "get_changes_since"]
    assert call_entries[-1][1]["cursor"] == "cursor-v1"


async def test_sync_incremental_falls_back_to_full_when_cdc_unsupported(
    backend: Backend,
) -> None:
    # cursors dict is empty → FakeMCPClient returns cursor=None → fallback to list.
    client = FakeMCPClient({"accounts": [_load("account_income.json")]})
    runner = SyncRunner(client, backend)

    await runner.sync_incremental(entities=["accounts"])

    # A list_entities call happened (the fallback).
    assert any(call[0] == "list_entities" for call in client.calls)
    rows = await backend.execute_read("SELECT COUNT(*) AS n FROM accounts")
    assert rows[0]["n"] == 1
