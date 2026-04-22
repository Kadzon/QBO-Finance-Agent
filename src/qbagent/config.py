"""Environment-driven configuration.

All runtime configuration comes from environment variables (optionally loaded
from a ``.env`` file). Settings loading itself never fails so that ``--help``
and ``doctor`` remain usable on a fresh checkout; commands that need a given
credential set call the ``require_*`` helpers, which raise with an actionable
message when something is missing.
"""

from __future__ import annotations

import shlex
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Backend(StrEnum):
    DUCKDB = "duckdb"
    SQLITE = "sqlite"
    POSTGRES = "postgres"


class QBOEnvironment(StrEnum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


class ConfigError(RuntimeError):
    """Raised when a command is run without the config it needs."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM --------------------------------------------------------------
    llm_model: str | None = Field(default=None, alias="QBAGENT_LLM_MODEL")
    llm_api_key: str | None = Field(default=None, alias="QBAGENT_LLM_API_KEY")
    llm_api_base: str | None = Field(default=None, alias="QBAGENT_LLM_API_BASE")
    llm_temperature: float = Field(default=0.0, alias="QBAGENT_LLM_TEMPERATURE")
    llm_max_tokens: int = Field(default=2048, alias="QBAGENT_LLM_MAX_TOKENS")
    llm_request_timeout: float = Field(default=60.0, alias="QBAGENT_LLM_REQUEST_TIMEOUT")
    llm_max_retries: int = Field(default=3, alias="QBAGENT_LLM_MAX_RETRIES")

    # --- Backend ----------------------------------------------------------
    backend: Backend = Field(default=Backend.DUCKDB, alias="QBAGENT_BACKEND")
    db_path: Path = Field(default=Path("./qbagent.duckdb"), alias="QBAGENT_DB_PATH")
    db_url: str | None = Field(default=None, alias="QBAGENT_DB_URL")

    # --- QBO MCP server ---------------------------------------------------
    mcp_server_command: str | None = Field(default=None, alias="QBAGENT_MCP_SERVER_COMMAND")
    mcp_server_args: str = Field(default="", alias="QBAGENT_MCP_SERVER_ARGS")
    mcp_server_cwd: Path | None = Field(default=None, alias="QBAGENT_MCP_SERVER_CWD")

    # --- QuickBooks credentials (passed to MCP server env) ---------------
    qbo_client_id: str | None = Field(default=None, alias="QUICKBOOKS_CLIENT_ID")
    qbo_client_secret: str | None = Field(default=None, alias="QUICKBOOKS_CLIENT_SECRET")
    qbo_realm_id: str | None = Field(default=None, alias="QUICKBOOKS_REALM_ID")
    qbo_refresh_token: str | None = Field(default=None, alias="QUICKBOOKS_REFRESH_TOKEN")
    qbo_environment: QBOEnvironment = Field(
        default=QBOEnvironment.SANDBOX, alias="QUICKBOOKS_ENVIRONMENT"
    )

    # --- Observability ----------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", alias="QBAGENT_LOG_LEVEL"
    )
    log_format: Literal["console", "json"] = Field(default="console", alias="QBAGENT_LOG_FORMAT")
    langsmith_api_key: str | None = Field(default=None, alias="LANGSMITH_API_KEY")
    langsmith_project: str | None = Field(default=None, alias="LANGSMITH_PROJECT")

    # --- Agent behavior ---------------------------------------------------
    max_generate_attempts: int = Field(default=3, alias="QBAGENT_MAX_GENERATE_ATTEMPTS")
    query_result_row_cap: int = Field(default=10_000, alias="QBAGENT_QUERY_ROW_CAP")

    @field_validator("mcp_server_args", mode="before")
    @classmethod
    def _coerce_args(cls, v: object) -> str:
        # Accept list-style env (JSON) or shell-style string; normalize to string.
        if v is None:
            return ""
        if isinstance(v, list):
            return " ".join(shlex.quote(str(item)) for item in v)
        return str(v)

    @property
    def mcp_server_argv(self) -> list[str]:
        """The MCP server command split into argv tokens."""
        if not self.mcp_server_command:
            return []
        return [self.mcp_server_command, *shlex.split(self.mcp_server_args)]

    @property
    def effective_db_target(self) -> str:
        """Human-readable description of where the DB lives."""
        if self.backend is Backend.POSTGRES:
            return self.db_url or "<unset postgres url>"
        return str(self.db_path)

    # --- Guards -----------------------------------------------------------
    def require_llm(self) -> None:
        missing = [
            name
            for name, value in (
                ("QBAGENT_LLM_MODEL", self.llm_model),
                ("QBAGENT_LLM_API_KEY", self.llm_api_key),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"LLM is not configured. Set {', '.join(missing)} in your environment or .env file."
            )

    def require_mcp(self) -> None:
        if not self.mcp_server_command:
            raise ConfigError(
                "QBO MCP server is not configured. Set QBAGENT_MCP_SERVER_COMMAND (and optionally "
                "QBAGENT_MCP_SERVER_ARGS) to point at an MCP server binary."
            )

    def require_qbo(self) -> None:
        missing = [
            name
            for name, value in (
                ("QUICKBOOKS_CLIENT_ID", self.qbo_client_id),
                ("QUICKBOOKS_CLIENT_SECRET", self.qbo_client_secret),
                ("QUICKBOOKS_REALM_ID", self.qbo_realm_id),
                ("QUICKBOOKS_REFRESH_TOKEN", self.qbo_refresh_token),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"QuickBooks credentials missing: {', '.join(missing)}. "
                "Register a developer app at https://developer.intuit.com and set these env vars."
            )

    def require_backend(self) -> None:
        if self.backend is Backend.POSTGRES and not self.db_url:
            raise ConfigError(
                "QBAGENT_BACKEND=postgres requires QBAGENT_DB_URL "
                "(e.g. postgresql://user:pass@host:5432/qbagent)."
            )


_SETTINGS: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """Return the process-wide Settings singleton.

    Pass ``refresh=True`` in tests that mutate the environment mid-run.
    """
    global _SETTINGS
    if _SETTINGS is None or refresh:
        _SETTINGS = Settings()
    return _SETTINGS
