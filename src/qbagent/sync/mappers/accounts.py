"""Mapper: QBO Account → qbagent.accounts row."""

from __future__ import annotations

from typing import Any

from qbagent.sync.mappers._qbo import (
    as_decimal,
    as_int_bool,
    currency,
    meta_timestamps,
    raw_json,
    ref_id,
)

TABLE = "accounts"


def map_to_rows(entity: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Map one QBO Account entity to a single row in the ``accounts`` table."""
    created_at, updated_at = meta_timestamps(entity)
    row = {
        "id": str(entity["Id"]),
        "name": entity.get("Name") or entity.get("FullyQualifiedName") or "",
        "account_type": entity.get("AccountType"),
        "account_sub_type": entity.get("AccountSubType"),
        "parent_id": ref_id(entity, "ParentRef"),
        "active": as_int_bool(entity.get("Active"), default=1),
        "currency_code": currency(entity),
        "current_balance": as_decimal(entity.get("CurrentBalance")),
        "created_at": created_at,
        "updated_at": updated_at,
        "raw": raw_json(entity),
    }
    return {TABLE: [row]}
