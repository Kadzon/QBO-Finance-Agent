"""Mapper: QBO Invoice → qbagent.invoices + qbagent.invoice_lines rows."""

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

HEADER_TABLE = "invoices"
LINE_TABLE = "invoice_lines"

# QBO "DetailType" values we emit as line rows. Summary/subtotal detail types
# exist solely for totaling in the QBO UI and must not be aggregated.
_ITEM_DETAIL_TYPES = {
    "SalesItemLineDetail",
    "GroupLineDetail",
    "DescriptionOnly",
}


def map_to_rows(entity: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    invoice_id = str(entity["Id"])
    created_at, updated_at = meta_timestamps(entity)
    total_amount = as_decimal(entity.get("TotalAmt")) or Decimal("0")
    balance = as_decimal(entity.get("Balance"))
    if balance is None:
        balance = total_amount

    header = {
        "id": invoice_id,
        "doc_number": entity.get("DocNumber"),
        "customer_id": ref_id(entity, "CustomerRef"),
        "customer_name": ref_name(entity, "CustomerRef"),
        "invoice_date": parse_qbo_date(entity.get("TxnDate")),
        "due_date": parse_qbo_date(entity.get("DueDate")),
        "total_amount": total_amount,
        "balance": balance,
        "status": _derive_status(entity, total_amount=total_amount, balance=balance),
        "currency_code": currency(entity),
        "memo": entity.get("CustomerMemo", {}).get("value")
        if isinstance(entity.get("CustomerMemo"), dict)
        else entity.get("PrivateNote"),
        "created_at": created_at,
        "updated_at": updated_at,
        "raw": raw_json(entity),
    }

    line_rows: list[dict[str, Any]] = []
    for line in entity.get("Line", []) or []:
        if not isinstance(line, dict):
            continue
        detail_type = line.get("DetailType")
        if detail_type not in _ITEM_DETAIL_TYPES:
            continue
        line_num = line.get("LineNum")
        line_id = str(line.get("Id") or line_num or len(line_rows) + 1)
        sales_detail = line.get("SalesItemLineDetail") or {}
        line_rows.append(
            {
                "id": f"{invoice_id}:{line_id}",
                "invoice_id": invoice_id,
                "line_num": int(line_num) if line_num is not None else None,
                "description": line.get("Description"),
                "amount": as_decimal(line.get("Amount")) or Decimal("0"),
                "account_id": ref_id(sales_detail, "ItemAccountRef"),
                "item_id": ref_id(sales_detail, "ItemRef"),
                "quantity": as_decimal(sales_detail.get("Qty")),
                "unit_price": as_decimal(sales_detail.get("UnitPrice")),
            }
        )

    return {HEADER_TABLE: [header], LINE_TABLE: line_rows}


def _derive_status(
    entity: dict[str, Any],
    *,
    total_amount: Decimal,
    balance: Decimal,
) -> str:
    """Translate QBO's scattered status fields into our discrete enum.

    QBO doesn't expose a single 'status' field on Invoice — the state is
    spread across Voided indicators, Balance, and email/print flags.
    """
    if _is_voided(entity):
        return "Voided"
    if total_amount > 0 and balance == 0:
        return "Paid"
    if 0 < balance < total_amount:
        return "PartiallyPaid"
    if _looks_sent(entity):
        return "Sent"
    return "Draft"


def _is_voided(entity: dict[str, Any]) -> bool:
    if entity.get("Voided") is True:
        return True
    memo = str(entity.get("PrivateNote") or "").lower()
    return "voided" in memo and "void" in (entity.get("DocNumber") or "").lower()


def _looks_sent(entity: dict[str, Any]) -> bool:
    if entity.get("Status") == "Sent":
        return True
    email_status = entity.get("EmailStatus")
    if email_status and email_status not in {"NotSet", "NeedToSend"}:
        return True
    return entity.get("PrintStatus") == "PrintComplete"
