"""Tests for the entity mappers.

Each test loads a recorded QBO response JSON from
``tests/fixtures/mcp_responses/`` and pins the mapper's output to what the
downstream ``bulk_upsert`` actually needs: primary keys, derived status,
decimals/dates preserved, and line-item rows materialized correctly.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from qbagent.sync.mappers import MAPPERS
from qbagent.sync.mappers import accounts as accounts_mapper
from qbagent.sync.mappers import bills as bills_mapper
from qbagent.sync.mappers import expenses as expenses_mapper
from qbagent.sync.mappers import invoices as invoices_mapper
from qbagent.sync.mappers import transactions as transactions_mapper

FIXTURES = Path(__file__).parent / "fixtures" / "mcp_responses"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------


def test_account_mapper_extracts_header_fields() -> None:
    result = accounts_mapper.map_to_rows(_load("account_income.json"))
    assert set(result) == {"accounts"}
    row = result["accounts"][0]
    assert row["id"] == "79"
    assert row["name"] == "Services"
    assert row["account_type"] == "Income"
    assert row["account_sub_type"] == "ServiceFeeIncome"
    assert row["parent_id"] == "42"
    assert row["active"] == 1
    assert row["currency_code"] == "USD"
    assert row["current_balance"] == Decimal("0")
    assert row["created_at"] == datetime(2026, 1, 15, 9, 0, 0)
    assert row["updated_at"] == datetime(2026, 3, 18, 14, 30, 0)


def test_account_mapper_handles_inactive_account() -> None:
    entity = _load("account_income.json") | {"Active": False}
    row = accounts_mapper.map_to_rows(entity)["accounts"][0]
    assert row["active"] == 0


# ---------------------------------------------------------------------------
# invoices
# ---------------------------------------------------------------------------


def test_invoice_partially_paid_header_and_lines() -> None:
    result = invoices_mapper.map_to_rows(_load("invoice_partially_paid.json"))
    assert set(result) == {"invoices", "invoice_lines"}

    header = result["invoices"][0]
    assert header["id"] == "1001"
    assert header["doc_number"] == "INV-1001"
    assert header["customer_id"] == "55"
    assert header["customer_name"] == "Acme Corp"
    assert header["invoice_date"] == date(2026, 1, 15)
    assert header["due_date"] == date(2026, 2, 15)
    assert header["total_amount"] == Decimal("2500.00")
    assert header["balance"] == Decimal("1250.00")
    assert header["status"] == "PartiallyPaid"
    assert header["currency_code"] == "USD"

    lines = result["invoice_lines"]
    assert [line["id"] for line in lines] == ["1001:1", "1001:2"]
    assert lines[0]["invoice_id"] == "1001"
    assert lines[0]["amount"] == Decimal("2000.00")
    assert lines[0]["item_id"] == "23"
    assert lines[0]["account_id"] == "79"
    assert lines[0]["quantity"] == Decimal("10")
    assert lines[0]["unit_price"] == Decimal("200.00")
    # The SubTotalLineDetail row must NOT show up as a line item.
    assert all(line["amount"] != Decimal("2500.00") or False for line in lines)
    assert len(lines) == 2


def test_invoice_paid_status() -> None:
    row = invoices_mapper.map_to_rows(_load("invoice_paid.json"))["invoices"][0]
    assert row["status"] == "Paid"
    assert row["balance"] == Decimal("0.00")


def test_invoice_voided_status() -> None:
    row = invoices_mapper.map_to_rows(_load("invoice_voided.json"))["invoices"][0]
    assert row["status"] == "Voided"


def test_invoice_draft_when_not_sent_and_open() -> None:
    entity = _load("invoice_partially_paid.json") | {"EmailStatus": "NotSet"}
    # Make it look like a zero-paid invoice with no email sent
    entity["Balance"] = 2500.00
    row = invoices_mapper.map_to_rows(entity)["invoices"][0]
    assert row["status"] == "Draft"


# ---------------------------------------------------------------------------
# bills
# ---------------------------------------------------------------------------


def test_bill_mapper_header_and_lines() -> None:
    result = bills_mapper.map_to_rows(_load("bill_open.json"))
    assert set(result) == {"bills", "bill_lines"}

    header = result["bills"][0]
    assert header["id"] == "501"
    assert header["vendor_id"] == "88"
    assert header["vendor_name"] == "Cloud Host Co"
    assert header["bill_date"] == date(2026, 1, 20)
    assert header["total_amount"] == Decimal("450.00")
    assert header["balance"] == Decimal("450.00")
    assert header["status"] == "Open"

    lines = result["bill_lines"]
    assert len(lines) == 2
    assert all(line["bill_id"] == "501" for line in lines)
    assert [line["amount"] for line in lines] == [Decimal("300.00"), Decimal("150.00")]
    assert all(line["account_id"] == "120" for line in lines)


def test_bill_paid_when_balance_zero() -> None:
    entity = _load("bill_open.json") | {"Balance": 0}
    row = bills_mapper.map_to_rows(entity)["bills"][0]
    assert row["status"] == "Paid"


# ---------------------------------------------------------------------------
# expenses (Purchase)
# ---------------------------------------------------------------------------


def test_purchase_mapper_produces_expense_row() -> None:
    result = expenses_mapper.map_to_rows(_load("purchase_credit_card.json"))
    assert set(result) == {"expenses"}
    row = result["expenses"][0]
    assert row["id"] == "3001"
    assert row["payment_type"] == "CreditCard"
    assert row["account_id"] == "150"
    assert row["entity_id"] == "92"
    assert row["entity_name"] == "Office Supplies Co"
    assert row["expense_date"] == date(2026, 3, 2)
    assert row["total_amount"] == Decimal("127.44")
    assert row["status"] == "Paid"


# ---------------------------------------------------------------------------
# transactions
# ---------------------------------------------------------------------------


def test_transaction_mapper_uses_explicit_id() -> None:
    row = transactions_mapper.map_to_rows(_load("transaction_row.json"))["transactions"][0]
    assert row["id"] == "T-777"
    assert row["transaction_type"] == "Invoice"
    assert row["doc_number"] == "INV-1001"
    assert row["account_id"] == "79"
    assert row["amount"] == Decimal("2500.00")
    assert row["credit"] == Decimal("2500.00")
    assert row["entity_id"] == "55"
    assert row["transaction_date"] == date(2026, 1, 15)


def test_transaction_mapper_synthesizes_id_when_missing() -> None:
    entity = _load("transaction_row.json")
    entity.pop("Id")
    row_a = transactions_mapper.map_to_rows(entity)["transactions"][0]
    row_b = transactions_mapper.map_to_rows(entity)["transactions"][0]
    assert row_a["id"] == row_b["id"], "synthesized ID must be deterministic"
    assert row_a["id"].startswith("txn-")


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entity", ["accounts", "invoices", "bills", "expenses", "transactions"])
def test_registry_exposes_every_mapper(entity: str) -> None:
    assert entity in MAPPERS
    assert callable(MAPPERS[entity])
