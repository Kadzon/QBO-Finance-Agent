"""Mapper: QBO Bill → qbagent.bills + qbagent.bill_lines rows."""

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

HEADER_TABLE = "bills"
LINE_TABLE = "bill_lines"

_EXPENSE_DETAIL_TYPES = {
    "AccountBasedExpenseLineDetail",
    "ItemBasedExpenseLineDetail",
}


def map_to_rows(entity: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    bill_id = str(entity["Id"])
    created_at, updated_at = meta_timestamps(entity)
    total_amount = as_decimal(entity.get("TotalAmt")) or Decimal("0")
    balance = as_decimal(entity.get("Balance"))
    if balance is None:
        balance = total_amount

    header = {
        "id": bill_id,
        "doc_number": entity.get("DocNumber"),
        "vendor_id": ref_id(entity, "VendorRef"),
        "vendor_name": ref_name(entity, "VendorRef"),
        "bill_date": parse_qbo_date(entity.get("TxnDate")),
        "due_date": parse_qbo_date(entity.get("DueDate")),
        "total_amount": total_amount,
        "balance": balance,
        "status": _derive_status(entity, total_amount=total_amount, balance=balance),
        "currency_code": currency(entity),
        "memo": entity.get("PrivateNote"),
        "created_at": created_at,
        "updated_at": updated_at,
        "raw": raw_json(entity),
    }

    line_rows: list[dict[str, Any]] = []
    for line in entity.get("Line", []) or []:
        if not isinstance(line, dict):
            continue
        if line.get("DetailType") not in _EXPENSE_DETAIL_TYPES:
            continue
        line_num = line.get("LineNum")
        line_id = str(line.get("Id") or line_num or len(line_rows) + 1)
        detail = (
            line.get("AccountBasedExpenseLineDetail")
            or line.get("ItemBasedExpenseLineDetail")
            or {}
        )
        line_rows.append(
            {
                "id": f"{bill_id}:{line_id}",
                "bill_id": bill_id,
                "line_num": int(line_num) if line_num is not None else None,
                "description": line.get("Description"),
                "amount": as_decimal(line.get("Amount")) or Decimal("0"),
                "account_id": ref_id(detail, "AccountRef") or ref_id(detail, "ItemAccountRef"),
            }
        )

    return {HEADER_TABLE: [header], LINE_TABLE: line_rows}


def _derive_status(
    entity: dict[str, Any],
    *,
    total_amount: Decimal,
    balance: Decimal,
) -> str:
    if entity.get("Voided") is True:
        return "Voided"
    if total_amount > 0 and balance == 0:
        return "Paid"
    return "Open"
