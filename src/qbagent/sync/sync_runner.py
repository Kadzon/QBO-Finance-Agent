"""Sync runner — pulls QBO data through the MCP client into the local DB.

The runner is entity-oriented: it walks a fixed dependency order (accounts
first, then invoices/bills/expenses, then transactions that reference them),
mapping each QBO record through the per-entity mapper and upserting the
resulting rows. Failure of one entity is logged and skipped — it must not
stop the others from syncing.

``sync_full`` clears per-entity cursors and pulls every record.
``sync_incremental`` uses ``sync_log.last_cursor`` to pull only what has
changed. When the MCP server does not support CDC, ``get_changes_since``
returns ``cursor=None`` and we fall back to a full list for that entity.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from qbagent.db.backend import Backend
from qbagent.sync.mappers import MAPPERS, PK_COLUMNS
from qbagent.sync.mcp_client import CANONICAL_ENTITIES, MCPClient

log = structlog.get_logger(__name__)

#: Order matters — accounts must exist before the transactional entities that
#: reference them, and transactions land last because they reference everything.
SYNC_ORDER: tuple[str, ...] = (
    "accounts",
    "invoices",
    "bills",
    "expenses",
    "transactions",
)


@dataclass(slots=True)
class EntitySyncResult:
    entity: str
    status: str  # "success" | "error" | "skipped"
    rows_synced: int = 0
    cursor: str | None = None
    error: str | None = None


@dataclass(slots=True)
class SyncReport:
    results: list[EntitySyncResult]
    started_at: datetime
    finished_at: datetime

    @property
    def total_rows(self) -> int:
        return sum(r.rows_synced for r in self.results if r.status == "success")

    @property
    def ok(self) -> bool:
        return all(r.status != "error" for r in self.results)


class SyncRunner:
    def __init__(self, client: MCPClient, backend: Backend) -> None:
        self._client = client
        self._backend = backend

    async def sync_full(self, entities: Iterable[str] | None = None) -> SyncReport:
        """Pull every record. Wipes per-entity cursors up front."""
        targets = _resolve_targets(entities)
        started = datetime.now(UTC).replace(tzinfo=None)
        results: list[EntitySyncResult] = []
        for entity in targets:
            result = await self._sync_one(entity, incremental=False)
            results.append(result)
        finished = datetime.now(UTC).replace(tzinfo=None)
        return SyncReport(results=results, started_at=started, finished_at=finished)

    async def sync_incremental(self, entities: Iterable[str] | None = None) -> SyncReport:
        """Pull only what has changed per ``sync_log.last_cursor``.

        If the client reports CDC is unsupported for an entity (returns
        ``cursor=None``), that entity falls back to a full pull.
        """
        targets = _resolve_targets(entities)
        started = datetime.now(UTC).replace(tzinfo=None)
        results: list[EntitySyncResult] = []
        for entity in targets:
            result = await self._sync_one(entity, incremental=True)
            results.append(result)
        finished = datetime.now(UTC).replace(tzinfo=None)
        return SyncReport(results=results, started_at=started, finished_at=finished)

    # --- Per-entity ------------------------------------------------------

    async def _sync_one(self, entity: str, *, incremental: bool) -> EntitySyncResult:
        mapper = MAPPERS.get(entity)
        if mapper is None:
            log.warning("sync.unknown_entity", entity=entity)
            return EntitySyncResult(entity=entity, status="skipped", error="unknown entity")

        # Capture cursor before any state mutation so ``_mark_in_progress``
        # can preserve it — otherwise incremental syncs would reset to
        # full-pull on every run.
        prev_cursor = await self._read_cursor(entity)
        await self._mark_in_progress(entity, cursor=prev_cursor)
        started = datetime.now(UTC).replace(tzinfo=None)

        try:
            records, cursor = await self._fetch(
                entity, incremental=incremental, prev_cursor=prev_cursor
            )
            table_rows = _apply_mapper(mapper, records)
            for table, rows in table_rows.items():
                if not rows:
                    continue
                pk = PK_COLUMNS.get(table, "id")
                await self._backend.bulk_upsert(table, rows, pk_column=pk)
            rows_synced = sum(len(rows) for rows in table_rows.values())
            await self._mark_success(
                entity, rows_synced=rows_synced, cursor=cursor, finished_at=started
            )
            log.info(
                "sync.entity_ok",
                entity=entity,
                rows=rows_synced,
                incremental=incremental,
                cursor=cursor,
            )
            return EntitySyncResult(
                entity=entity, status="success", rows_synced=rows_synced, cursor=cursor
            )
        except Exception as exc:  # isolate this entity's failure
            log.exception("sync.entity_failed", entity=entity)
            await self._mark_error(entity, str(exc), finished_at=started)
            return EntitySyncResult(entity=entity, status="error", error=str(exc))

    async def _fetch(
        self, entity: str, *, incremental: bool, prev_cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        if incremental:
            records, new_cursor = await self._client.get_changes_since(entity, prev_cursor)
            if new_cursor is not None:
                return records, new_cursor
            log.info("sync.cdc_fallback", entity=entity)

        records_all: list[dict[str, Any]] = []
        async for record in self._client.list_entities(entity):
            records_all.append(record)
        return records_all, None

    # --- sync_log helpers -------------------------------------------------

    async def _read_cursor(self, entity: str) -> str | None:
        rows = await self._backend.execute_read(
            "SELECT last_cursor FROM sync_log WHERE entity = ?", (entity,)
        )
        if not rows:
            return None
        cursor = rows[0].get("last_cursor")
        return str(cursor) if cursor else None

    async def _mark_in_progress(self, entity: str, *, cursor: str | None) -> None:
        await self._backend.bulk_upsert(
            "sync_log",
            [
                {
                    "entity": entity,
                    "last_cursor": cursor,
                    "last_sync_at": datetime.now(UTC).replace(tzinfo=None),
                    "last_sync_status": "in_progress",
                    "last_error": None,
                    "rows_synced": 0,
                }
            ],
            pk_column="entity",
        )

    async def _mark_success(
        self,
        entity: str,
        *,
        rows_synced: int,
        cursor: str | None,
        finished_at: datetime,
    ) -> None:
        prev = await self._read_cursor(entity)
        effective_cursor = cursor if cursor is not None else prev
        await self._backend.bulk_upsert(
            "sync_log",
            [
                {
                    "entity": entity,
                    "last_cursor": effective_cursor,
                    "last_sync_at": finished_at,
                    "last_sync_status": "success",
                    "last_error": None,
                    "rows_synced": rows_synced,
                }
            ],
            pk_column="entity",
        )

    async def _mark_error(self, entity: str, error: str, *, finished_at: datetime) -> None:
        await self._backend.bulk_upsert(
            "sync_log",
            [
                {
                    "entity": entity,
                    "last_cursor": None,
                    "last_sync_at": finished_at,
                    "last_sync_status": "error",
                    "last_error": error,
                    "rows_synced": 0,
                }
            ],
            pk_column="entity",
        )


def _resolve_targets(entities: Iterable[str] | None) -> list[str]:
    if entities is None:
        return list(SYNC_ORDER)
    requested = list(entities)
    unknown = [e for e in requested if e not in CANONICAL_ENTITIES]
    if unknown:
        raise ValueError(f"Unknown entities: {', '.join(unknown)}")
    # Preserve SYNC_ORDER even when a subset is requested.
    return [e for e in SYNC_ORDER if e in requested]


def _apply_mapper(
    mapper: Any,
    records: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        mapped = mapper(record)
        for table, rows in mapped.items():
            out.setdefault(table, []).extend(rows)
    return out
