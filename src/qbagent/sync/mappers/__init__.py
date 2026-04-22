"""Entity mappers: QBO JSON → qbagent rows.

Each module exposes a pure ``map_to_rows(entity) -> {table: [rows]}`` function.
The registry keyed by qbagent's entity name is the single place the sync
runner resolves mappers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from qbagent.sync.mappers import accounts, bills, expenses, invoices, transactions

MapToRows = Callable[[dict[str, Any]], dict[str, list[dict[str, Any]]]]

MAPPERS: dict[str, MapToRows] = {
    "accounts": accounts.map_to_rows,
    "invoices": invoices.map_to_rows,
    "bills": bills.map_to_rows,
    "expenses": expenses.map_to_rows,
    "transactions": transactions.map_to_rows,
}

# Primary-key column for each table, consumed by Backend.bulk_upsert.
PK_COLUMNS: dict[str, str] = {
    "accounts": "id",
    "invoices": "id",
    "invoice_lines": "id",
    "bills": "id",
    "bill_lines": "id",
    "expenses": "id",
    "transactions": "id",
}


__all__ = ["MAPPERS", "PK_COLUMNS", "MapToRows"]
