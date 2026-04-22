"""Backend factory.

Reads :class:`qbagent.config.Settings` and returns the correct backend
instance. Centralized so the CLI, server, and tests construct backends the
same way.
"""

from __future__ import annotations

from qbagent.config import Backend as BackendChoice
from qbagent.config import Settings
from qbagent.db.backend import Backend


def create_backend(settings: Settings) -> Backend:
    """Build a backend from settings.

    Validates required fields up front — callers don't need to inspect the
    settings object for backend-specific env vars.
    """
    settings.require_backend()
    if settings.backend is BackendChoice.DUCKDB:
        from qbagent.db.duckdb_backend import DuckDBBackend

        return DuckDBBackend(settings.db_path)
    if settings.backend is BackendChoice.SQLITE:
        from qbagent.db.sqlite_backend import SQLiteBackend

        return SQLiteBackend(settings.db_path)
    if settings.backend is BackendChoice.POSTGRES:
        from qbagent.db.postgres_backend import PostgresBackend

        assert settings.db_url is not None  # require_backend checked this
        return PostgresBackend(settings.db_url)
    raise ValueError(f"Unknown backend: {settings.backend!r}")
