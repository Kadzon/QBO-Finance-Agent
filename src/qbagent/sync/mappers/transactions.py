"""Mapper: QBO transaction row → qbagent.transactions.

QBO's TransactionList is a report, not a raw entity, so shapes vary by MCP
server. We accept a reasonable canonical form and document the expected keys.
If the upstream record lacks a stable ID, we generate a deterministic one
from date+type+doc_number+account so that repeated syncs don't duplicate.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

from qbagent.sync.mappers._qbo import (
    as_decimal,
    meta_timestamps,
    parse_qbo_date,
    raw_json,
    ref_id,
    ref_name,
)

TABLE = "transactions"


def map_to_rows(entity: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    created_at, updated_at = meta_timestamps(entity)
    txn_date = parse_qbo_date(entity.get("TxnDate") or entity.get("Date"))
    txn_type = entity.get("TxnType") or entity.get("TransactionType") or "Unknown"
    doc_number = entity.get("DocNumber") or entity.get("Num")
    account_id = ref_id(entity, "AccountRef")

    row = {
        "id": str(entity.get("Id") or _synthetic_id(txn_date, txn_type, doc_number, account_id)),
        "transaction_date": txn_date,
        "transaction_type": txn_type,
        "doc_number": doc_number,
        "account_id": account_id,
        "debit": as_decimal(entity.get("Debit")),
        "credit": as_decimal(entity.get("Credit")),
        "amount": as_decimal(entity.get("Amount")) or Decimal("0"),
        "entity_id": ref_id(entity, "EntityRef"),
        "entity_name": ref_name(entity, "EntityRef") or entity.get("Name"),
        "memo": entity.get("Memo") or entity.get("PrivateNote"),
        "created_at": created_at,
        "updated_at": updated_at,
        "raw": raw_json(entity),
    }
    return {TABLE: [row]}


def _synthetic_id(
    txn_date: Any,
    txn_type: str,
    doc_number: str | None,
    account_id: str | None,
) -> str:
    """Deterministic hash so duplicate imports don't create duplicate rows."""
    key = f"{txn_date}|{txn_type}|{doc_number or ''}|{account_id or ''}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"txn-{digest}"
