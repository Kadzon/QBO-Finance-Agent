"""Mapper: QBO Purchase → qbagent.expenses row.

QBO calls cash-basis purchases "Purchase"; we surface them as
``expenses`` in qbagent's schema for clarity.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from qbagent.sync.mappers._qbo import (
    as_decimal,
    currency,
    meta_timestamps,
    parse_qbo_date,
    raw_json,
    ref_id,
    ref_name,
)

TABLE = "expenses"


def map_to_rows(entity: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    created_at, updated_at = meta_timestamps(entity)
    row = {
        "id": str(entity["Id"]),
        "payment_type": entity.get("PaymentType"),
        "account_id": ref_id(entity, "AccountRef"),
        "entity_id": ref_id(entity, "EntityRef"),
        "entity_name": ref_name(entity, "EntityRef"),
        "expense_date": parse_qbo_date(entity.get("TxnDate")),
        "total_amount": as_decimal(entity.get("TotalAmt")) or Decimal("0"),
        "status": "Voided" if entity.get("Voided") else "Paid",
        "currency_code": currency(entity),
        "memo": entity.get("PrivateNote"),
        "created_at": created_at,
        "updated_at": updated_at,
        "raw": raw_json(entity),
    }
    return {TABLE: [row]}
