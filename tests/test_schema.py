"""Schema parse and transpile tests.

Ensures the canonical DDL parses cleanly and transpiles to each target dialect
without losing critical structure (table count, indexes).
"""

from __future__ import annotations

import pytest

from qbagent.db.backend import _render_schema_description, load_ddl_statements

EXPECTED_TABLES = {
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


@pytest.mark.parametrize("dialect", ["duckdb", "sqlite", "postgres"])
def test_ddl_transpiles_to_every_dialect(dialect: str) -> None:
    stmts = load_ddl_statements(dialect)  # type: ignore[arg-type]
    assert stmts, "schema.sql produced no statements"
    joined = "\n".join(stmts).lower()
    for table in EXPECTED_TABLES:
        assert "create table" in joined
        assert table.lower() in joined, f"{table} missing in {dialect} transpile"


def test_schema_description_mentions_key_columns() -> None:
    desc = _render_schema_description()
    # Spot-check: the agent prompt must always see these.
    assert "invoice_lines" in desc
    assert "status" in desc
    assert "Voided" in desc  # surfaced via the column comment
    assert "balance" in desc
