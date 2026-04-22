"""MCP client abstraction over a pluggable QBO MCP server.

Everything the sync runner needs expressed as four entity-oriented primitives:

* :meth:`MCPClient.list_entities` — full page-through of a QBO entity
* :meth:`MCPClient.get_entity` — single-record live lookup by ID
* :meth:`MCPClient.query` — QBO SQL pass-through
* :meth:`MCPClient.get_changes_since` — incremental CDC via a cursor

QBO itself is reached through whatever MCP server the user has configured
(Intuit's official one, MCPBundles, a custom wrapper, etc.). Concrete tool
names vary by server, so :class:`StdioMCPClient` takes a :class:`ToolMapping`
that callers can override; the defaults match the most common naming.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import structlog

from qbagent.config import Settings

log = structlog.get_logger(__name__)

# The five QBO entities qbagent knows how to sync.
EntityName = str  # keep it loose so users can add custom entities

CANONICAL_ENTITIES: tuple[str, ...] = (
    "accounts",
    "invoices",
    "bills",
    "expenses",  # QBO "Purchase"
    "transactions",  # QBO TransactionList report
)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MCPClient(Protocol):
    """Async QBO data source. Both real and fake implementations conform."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    def list_entities(
        self,
        entity: EntityName,
        *,
        page_size: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every record of ``entity``, one at a time, across pagination.

        Implementations must handle the underlying MCP server's pagination
        internally; callers just iterate until exhaustion.
        """

    async def get_entity(self, entity: EntityName, entity_id: str) -> dict[str, Any] | None:
        """Fetch a single record by its QBO ID."""

    async def query(self, qbo_sql: str) -> list[dict[str, Any]]:
        """Execute a QBO SQL statement (e.g. ``SELECT * FROM Invoice``)."""

    async def get_changes_since(
        self,
        entity: EntityName,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return (changed records, new cursor) since the last sync.

        Returning ``cursor=None`` tells the sync runner that incremental CDC
        isn't available and to fall back to a full list.
        """


# ---------------------------------------------------------------------------
# Tool mapping — which MCP tool names service which primitives.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolMapping:
    """Maps qbagent's entity-oriented primitives to concrete MCP tool names.

    Default values target Intuit-style MCP servers that expose one ``query``
    tool plus per-entity ``list_{entity}`` / ``get_{entity}`` helpers. Users
    whose MCP server differs can instantiate a mapping with the names their
    server exposes and pass it to :class:`StdioMCPClient`.
    """

    query: str = "query"
    cdc: str | None = "cdc"  # set to None if the server doesn't support CDC
    list_tool: dict[str, str] = field(default_factory=dict)
    get_tool: dict[str, str] = field(default_factory=dict)

    #: QBO entity name used in SQL for each qbagent entity (canonical CamelCase).
    qbo_entity_name: dict[str, str] = field(
        default_factory=lambda: {
            "accounts": "Account",
            "invoices": "Invoice",
            "bills": "Bill",
            "expenses": "Purchase",
            "transactions": "Transaction",
        }
    )

    def list_for(self, entity: EntityName) -> str | None:
        return self.list_tool.get(entity)

    def get_for(self, entity: EntityName) -> str | None:
        return self.get_tool.get(entity)


# ---------------------------------------------------------------------------
# StdioMCPClient — real client that spawns the configured MCP server.
# ---------------------------------------------------------------------------


class StdioMCPClient:
    """Launches the configured MCP server and calls its tools.

    The server is spawned as a child process whose stdin/stdout carry the MCP
    framed protocol. We use the official ``mcp`` Python SDK's stdio client.

    If the server does not expose a dedicated list tool for an entity, we fall
    back to its generic ``query`` tool and issue the QBO SQL form
    ``SELECT * FROM <Entity> STARTPOSITION <n> MAXRESULTS <page_size>``.
    """

    def __init__(
        self,
        settings: Settings,
        tool_mapping: ToolMapping | None = None,
    ) -> None:
        settings.require_mcp()
        self._settings = settings
        self._mapping = tool_mapping or ToolMapping()
        self._stack: AsyncExitStack | None = None
        self._session: Any = None  # mcp.ClientSession

    async def connect(self) -> None:
        if self._session is not None:
            return
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        argv = self._settings.mcp_server_argv
        if not argv:
            raise RuntimeError("MCP server command is not configured.")
        params = StdioServerParameters(
            command=argv[0],
            args=argv[1:],
            env=self._mcp_env(),
        )

        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._session = session
        log.info("mcp.connected", command=argv[0], args=argv[1:])

    async def close(self) -> None:
        if self._stack is None:
            return
        stack, self._stack = self._stack, None
        self._session = None
        await stack.aclose()

    # --- Primitives -------------------------------------------------------

    async def list_entities(
        self,
        entity: EntityName,
        *,
        page_size: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        tool_name = self._mapping.list_for(entity)
        if tool_name:
            async for row in self._paginate_tool(tool_name, page_size):
                yield row
            return

        # Fallback: use QBO SQL via the query tool.
        qbo_name = self._mapping.qbo_entity_name.get(entity, entity)
        start = 1
        while True:
            rows = await self.query(
                f"SELECT * FROM {qbo_name} STARTPOSITION {start} MAXRESULTS {page_size}"
            )
            if not rows:
                return
            for row in rows:
                yield row
            if len(rows) < page_size:
                return
            start += page_size

    async def get_entity(self, entity: EntityName, entity_id: str) -> dict[str, Any] | None:
        tool_name = self._mapping.get_for(entity)
        session = self._require_session()
        if tool_name:
            result = await session.call_tool(tool_name, {"id": entity_id})
            return _single_record(result)

        qbo_name = self._mapping.qbo_entity_name.get(entity, entity)
        rows = await self.query(f"SELECT * FROM {qbo_name} WHERE Id = '{entity_id}'")
        return rows[0] if rows else None

    async def query(self, qbo_sql: str) -> list[dict[str, Any]]:
        session = self._require_session()
        result = await session.call_tool(self._mapping.query, {"sql": qbo_sql})
        return _records(result)

    async def get_changes_since(
        self,
        entity: EntityName,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if self._mapping.cdc is None:
            return [], None
        session = self._require_session()
        qbo_name = self._mapping.qbo_entity_name.get(entity, entity)
        args: dict[str, Any] = {"entities": qbo_name}
        if cursor:
            args["changed_since"] = cursor
        try:
            result = await session.call_tool(self._mapping.cdc, args)
        except Exception as exc:
            log.warning("mcp.cdc_unsupported", entity=entity, error=str(exc))
            return [], None
        records = _records(result)
        new_cursor = _cursor_from(result)
        return records, new_cursor

    # --- Internals --------------------------------------------------------

    async def _paginate_tool(
        self,
        tool_name: str,
        page_size: int,
    ) -> AsyncIterator[dict[str, Any]]:
        session = self._require_session()
        page_token: str | None = None
        while True:
            args: dict[str, Any] = {"page_size": page_size}
            if page_token:
                args["page_token"] = page_token
            result = await session.call_tool(tool_name, args)
            rows = _records(result)
            for row in rows:
                yield row
            page_token = _next_page_token(result)
            if not page_token:
                return

    def _require_session(self) -> Any:
        if self._session is None:
            raise RuntimeError("MCP client is not connected; call connect() first.")
        return self._session

    def _mcp_env(self) -> dict[str, str]:
        """Environment variables forwarded to the MCP server subprocess.

        We pass QBO credentials through explicitly rather than relying on the
        parent process's env, since some spawners strip it.
        """
        import os

        env = dict(os.environ)
        if self._settings.qbo_client_id:
            env["QUICKBOOKS_CLIENT_ID"] = self._settings.qbo_client_id
        if self._settings.qbo_client_secret:
            env["QUICKBOOKS_CLIENT_SECRET"] = self._settings.qbo_client_secret
        if self._settings.qbo_realm_id:
            env["QUICKBOOKS_REALM_ID"] = self._settings.qbo_realm_id
        if self._settings.qbo_refresh_token:
            env["QUICKBOOKS_REFRESH_TOKEN"] = self._settings.qbo_refresh_token
        env["QUICKBOOKS_ENVIRONMENT"] = self._settings.qbo_environment.value
        return env


# ---------------------------------------------------------------------------
# MCP CallToolResult parsing helpers.
#
# The mcp SDK returns a CallToolResult whose ``content`` is a list of
# TextContent / EmbeddedResource / ImageContent blocks. QBO MCP servers tend
# to return text blocks containing either a JSON array or a JSON object with
# ``records`` / ``QueryResponse`` / ``items`` fields. We parse defensively.
# ---------------------------------------------------------------------------


def _records(result: Any) -> list[dict[str, Any]]:
    payload = _decode(result)
    if payload is None:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("records", "items", "data", "results"):
            if key in payload and isinstance(payload[key], list):
                return [row for row in payload[key] if isinstance(row, dict)]
        # QBO-style: {"QueryResponse": {"Invoice": [...], ...}}
        qr = payload.get("QueryResponse")
        if isinstance(qr, dict):
            for v in qr.values():
                if isinstance(v, list):
                    return [row for row in v if isinstance(row, dict)]
    return []


def _single_record(result: Any) -> dict[str, Any] | None:
    payload = _decode(result)
    if isinstance(payload, dict):
        # QBO single-entity endpoints return ``{"Invoice": {...}}`` etc.
        if len(payload) == 1:
            (only,) = payload.values()
            if isinstance(only, dict):
                return only
        return payload
    if isinstance(payload, list) and payload:
        first = payload[0]
        return first if isinstance(first, dict) else None
    return None


def _next_page_token(result: Any) -> str | None:
    payload = _decode(result)
    if isinstance(payload, dict):
        for key in ("next_page_token", "nextPageToken", "next_cursor"):
            token = payload.get(key)
            if isinstance(token, str) and token:
                return token
    return None


def _cursor_from(result: Any) -> str | None:
    payload = _decode(result)
    if isinstance(payload, dict):
        for key in ("cursor", "changed_since", "last_updated_time"):
            token = payload.get(key)
            if isinstance(token, str) and token:
                return token
    return None


def _decode(result: Any) -> Any:
    """Extract the JSON payload from an MCP CallToolResult."""
    content = getattr(result, "content", None)
    if content is None:
        return None
    for block in content:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            continue
    return None


# ---------------------------------------------------------------------------
# FakeMCPClient — the test double.
# ---------------------------------------------------------------------------


class FakeMCPClient:
    """In-memory MCP client for tests.

    Construct with a dict keyed by entity name whose values are the records
    that should come back from :meth:`list_entities` / :meth:`get_entity`.
    """

    def __init__(
        self,
        entities: Mapping[str, list[dict[str, Any]]] | None = None,
        *,
        cursors: Mapping[str, str] | None = None,
    ) -> None:
        self._entities: dict[str, list[dict[str, Any]]] = {
            k: list(v) for k, v in (entities or {}).items()
        }
        self._cursors: dict[str, str] = dict(cursors or {})
        self._connected = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.list_failures: dict[str, Exception] = {}

    def set_entities(self, entity: str, rows: list[dict[str, Any]]) -> None:
        self._entities[entity] = list(rows)

    def fail_on_list(self, entity: str, exc: Exception) -> None:
        """Make :meth:`list_entities` raise for ``entity`` — used in isolation tests."""
        self.list_failures[entity] = exc

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def list_entities(
        self,
        entity: EntityName,
        *,
        page_size: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(("list_entities", {"entity": entity, "page_size": page_size}))
        if entity in self.list_failures:
            raise self.list_failures[entity]
        for row in self._entities.get(entity, []):
            yield row

    async def get_entity(self, entity: EntityName, entity_id: str) -> dict[str, Any] | None:
        self.calls.append(("get_entity", {"entity": entity, "id": entity_id}))
        for row in self._entities.get(entity, []):
            if str(row.get("Id")) == str(entity_id):
                return row
        return None

    async def query(self, qbo_sql: str) -> list[dict[str, Any]]:
        self.calls.append(("query", {"sql": qbo_sql}))
        # Trivial implementation — tests that need this should set entities.
        return []

    async def get_changes_since(
        self,
        entity: EntityName,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        self.calls.append(("get_changes_since", {"entity": entity, "cursor": cursor}))
        rows = list(self._entities.get(entity, []))
        new_cursor = self._cursors.get(entity)
        return rows, new_cursor
