"""Database backend protocol and shared helpers.

All three concrete backends (DuckDB, SQLite, PostgreSQL) implement the same
interface so that the agent, sync runner, and tests can treat them
interchangeably. Per-backend SQL dialect is exposed via the ``dialect``
attribute for use with ``sqlglot.transpile``.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import sqlglot

SchemaDialect = Literal["duckdb", "sqlite", "postgres"]

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# ---------------------------------------------------------------------------
# Column-level descriptions used by get_schema_description().
#
# Kept in Python (not in the DDL) because ``COMMENT ON`` syntax diverges across
# our three backends. The agent prompt consumes this; keep it short and
# unambiguous.
# ---------------------------------------------------------------------------
COLUMN_COMMENTS: dict[str, dict[str, str]] = {
    "accounts": {
        "id": "QBO account ID",
        "account_type": "Income, Expense, Bank, Asset, Liability, or Equity",
        "active": "1 = active, 0 = inactive",
    },
    "invoices": {
        "status": "one of Draft, Sent, Paid, PartiallyPaid, Voided",
        "invoice_date": "use this for accrual-basis aggregations (default)",
        "total_amount": "invoice header total; for revenue use invoice_lines.amount",
        "balance": "outstanding amount; use this for A/R, not total_amount",
    },
    "invoice_lines": {
        "amount": "line amount; sum these (joined to income accounts) for revenue",
        "account_id": "join to accounts to filter by account_type='Income'",
    },
    "bills": {
        "status": "one of Open, Paid, Voided",
        "bill_date": "use this for accrual-basis aggregations (default)",
        "balance": "outstanding amount; use this for A/P",
    },
    "bill_lines": {
        "amount": "sum these for bill-based expenses",
    },
    "expenses": {
        "total_amount": "sum these (plus bill_lines.amount) for total expenses; do NOT add transactions",
        "status": "one of Paid, Voided",
    },
    "transactions": {
        "transaction_type": "ledger-level; NEVER aggregate alongside invoices/bills — double-counts",
    },
}


class Backend(abc.ABC):
    """Abstract async backend. Concrete classes wire a specific driver."""

    #: sqlglot dialect name used when transpiling LLM-produced SQL to this DB.
    dialect: SchemaDialect

    # --- Lifecycle --------------------------------------------------------
    @abc.abstractmethod
    async def connect(self) -> None:
        """Open the connection/pool. Idempotent."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Close all resources. Idempotent."""

    @abc.abstractmethod
    async def initialize_schema(self) -> None:
        """Create all tables and indexes if missing. Must be safe to re-run."""

    # --- Query execution --------------------------------------------------
    @abc.abstractmethod
    async def execute_read(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a SELECT and return rows as dicts.

        ``sql`` is expected to already match this backend's dialect. Callers
        running LLM-generated SQL should transpile first with
        :func:`sqlglot.transpile`.
        """

    @abc.abstractmethod
    async def execute_write(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] | None = None,
    ) -> int:
        """Execute an INSERT/UPDATE/DELETE and return affected row count."""

    @abc.abstractmethod
    async def bulk_upsert(
        self,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        pk_column: str,
    ) -> int:
        """Upsert ``rows`` into ``table`` keyed on ``pk_column``. Returns row count."""

    # --- Introspection ----------------------------------------------------
    def get_schema_description(self) -> str:
        """Prompt-ready description of the canonical schema.

        Returned string is stable across backends — the agent prompt should
        not vary with the user's chosen backend.
        """
        return _render_schema_description()

    # --- Shared helpers ---------------------------------------------------
    def _load_ddl_statements(self) -> list[str]:
        """Parse schema.sql and transpile each statement to this dialect."""
        return load_ddl_statements(self.dialect)


def load_ddl_statements(dialect: SchemaDialect) -> list[str]:
    """Split the canonical schema into executable statements.

    The DDL is written in portable SQL that every supported backend runs
    natively — no per-dialect transpilation is performed here (``sqlglot``
    rewrites types like ``DECIMAL`` → ``REAL`` and adds unsupported
    ``NULLS LAST`` clauses). The ``dialect`` argument is accepted for symmetry
    and future use but does not affect the output.
    """
    del dialect  # accepted for API stability; DDL is portable by construction
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    return _split_sql_statements(ddl)


def _split_sql_statements(ddl: str) -> list[str]:
    """Strip ``--`` comments and split on ``;`` terminators.

    Trusts that the canonical schema does not contain semicolons inside string
    literals — this function is not a general-purpose SQL parser.
    """
    cleaned: list[str] = []
    for line in ddl.splitlines():
        without_comment = line.split("--", 1)[0].rstrip()
        if without_comment:
            cleaned.append(without_comment)
    joined = "\n".join(cleaned)
    return [s.strip() for s in joined.split(";") if s.strip()]


def _render_schema_description() -> str:
    """Human-readable schema with column comments.

    Parsed directly from schema.sql so it never drifts from the DDL.
    """
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    parts: list[str] = ["# qbagent schema", ""]
    for expr in sqlglot.parse(ddl, read="postgres"):
        if expr is None:
            continue
        if expr.key != "create":
            continue
        if expr.args.get("kind", "").upper() != "TABLE":
            continue
        schema_node = expr.this
        table_name = schema_node.this.name
        parts.append(f"## {table_name}")
        comments = COLUMN_COMMENTS.get(table_name, {})
        for col in schema_node.expressions:
            col_name = col.name
            col_type = col.args["kind"].sql(dialect="postgres") if col.args.get("kind") else ""
            comment = comments.get(col_name)
            line = f"- {col_name} {col_type}".rstrip()
            if comment:
                line += f"   -- {comment}"
            parts.append(line)
        parts.append("")
    return "\n".join(parts)
