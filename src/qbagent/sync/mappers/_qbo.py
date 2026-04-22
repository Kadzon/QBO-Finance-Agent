"""Helpers shared by every entity mapper.

QBO's JSON has a handful of recurring shapes (``CustomerRef {"value": ...}``,
ISO timestamps with timezones, ``MetaData.CreateTime``, etc.) — centralizing
the parsing keeps each mapper small and uniform.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def parse_qbo_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_qbo_timestamp(value: Any) -> datetime | None:
    """Parse QBO's ``2026-01-15T09:30:00-08:00`` timestamps.

    Stored as naive in our schema (we drop timezone for portability); callers
    that care about TZ should parse ``raw`` instead.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def ref_id(entity: dict[str, Any], key: str) -> str | None:
    ref = entity.get(key)
    if isinstance(ref, dict):
        value = ref.get("value")
        return str(value) if value else None
    return None


def ref_name(entity: dict[str, Any], key: str) -> str | None:
    ref = entity.get(key)
    if isinstance(ref, dict):
        name = ref.get("name")
        return str(name) if name else None
    return None


def as_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def as_int_bool(value: Any, *, default: int = 1) -> int:
    """Normalize QBO's loose booleans (``true``, ``"true"``, ``1``) to 0/1."""
    if value is None:
        return default
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if value else 0
    if isinstance(value, str):
        return 1 if value.strip().lower() in {"true", "1", "yes"} else 0
    return default


def meta_timestamps(entity: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    meta = entity.get("MetaData") or {}
    return (
        parse_qbo_timestamp(meta.get("CreateTime")),
        parse_qbo_timestamp(meta.get("LastUpdatedTime")),
    )


def raw_json(entity: dict[str, Any]) -> str:
    """Serialize the full QBO entity for audit / debug."""
    return json.dumps(entity, separators=(",", ":"), sort_keys=True, default=str)


def currency(entity: dict[str, Any]) -> str | None:
    return ref_id(entity, "CurrencyRef")
