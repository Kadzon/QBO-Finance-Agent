"""Tests for qbagent.config."""

from __future__ import annotations

import pytest

from qbagent.config import Backend, ConfigError, QBOEnvironment, Settings, get_settings


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    # Force re-read of environment in each test.
    get_settings(refresh=True)


def test_defaults_load_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "QBAGENT_LLM_MODEL",
        "QBAGENT_LLM_API_KEY",
        "QBAGENT_BACKEND",
        "QBAGENT_DB_URL",
        "QBAGENT_MCP_SERVER_COMMAND",
        "QUICKBOOKS_CLIENT_ID",
        "QUICKBOOKS_CLIENT_SECRET",
        "QUICKBOOKS_REALM_ID",
        "QUICKBOOKS_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.backend is Backend.DUCKDB
    assert settings.qbo_environment is QBOEnvironment.SANDBOX
    assert settings.llm_model is None
    assert settings.llm_api_key is None


def test_require_llm_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QBAGENT_LLM_MODEL", raising=False)
    monkeypatch.delenv("QBAGENT_LLM_API_KEY", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    with pytest.raises(ConfigError) as exc:
        settings.require_llm()
    assert "QBAGENT_LLM_MODEL" in str(exc.value)
    assert "QBAGENT_LLM_API_KEY" in str(exc.value)


def test_require_llm_passes_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QBAGENT_LLM_MODEL", "anthropic/claude-opus-4-7")
    monkeypatch.setenv("QBAGENT_LLM_API_KEY", "sk-test")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    settings.require_llm()  # no exception
    assert settings.llm_model == "anthropic/claude-opus-4-7"


def test_postgres_backend_requires_db_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QBAGENT_BACKEND", "postgres")
    monkeypatch.delenv("QBAGENT_DB_URL", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    with pytest.raises(ConfigError) as exc:
        settings.require_backend()
    assert "QBAGENT_DB_URL" in str(exc.value)


def test_duckdb_backend_does_not_require_db_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QBAGENT_BACKEND", "duckdb")
    monkeypatch.delenv("QBAGENT_DB_URL", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    settings.require_backend()  # no exception


def test_mcp_server_argv_splits_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QBAGENT_MCP_SERVER_COMMAND", "npx")
    monkeypatch.setenv("QBAGENT_MCP_SERVER_ARGS", "-y @intuit/mcp-server-quickbooks")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.mcp_server_argv == ["npx", "-y", "@intuit/mcp-server-quickbooks"]


def test_require_qbo_lists_all_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "QUICKBOOKS_CLIENT_ID",
        "QUICKBOOKS_CLIENT_SECRET",
        "QUICKBOOKS_REALM_ID",
        "QUICKBOOKS_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    with pytest.raises(ConfigError) as exc:
        settings.require_qbo()
    msg = str(exc.value)
    for var in (
        "QUICKBOOKS_CLIENT_ID",
        "QUICKBOOKS_CLIENT_SECRET",
        "QUICKBOOKS_REALM_ID",
        "QUICKBOOKS_REFRESH_TOKEN",
    ):
        assert var in msg


def test_effective_db_target_for_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QBAGENT_BACKEND", "postgres")
    monkeypatch.setenv("QBAGENT_DB_URL", "postgresql://user:pw@localhost:5432/qbagent")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert "postgresql://" in settings.effective_db_target
