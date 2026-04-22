"""Tests for the top-level CLI surface."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from qbagent import __version__
from qbagent.cli import app
from qbagent.config import get_settings


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


@pytest.fixture(autouse=True)
def _reset_settings_singleton() -> None:
    get_settings(refresh=True)


def test_help_lists_every_command(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("sync", "ask", "chat", "doctor"):
        assert cmd in result.stdout


def test_version_flag(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_sync_fails_clearly_when_mcp_unconfigured(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in (
        "QBAGENT_MCP_SERVER_COMMAND",
        "QUICKBOOKS_CLIENT_ID",
        "QUICKBOOKS_CLIENT_SECRET",
        "QUICKBOOKS_REALM_ID",
        "QUICKBOOKS_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 2  # ConfigError path
    assert "MCP" in result.stderr or "QuickBooks" in result.stderr


def test_sync_rejects_unknown_entity(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QBAGENT_MCP_SERVER_COMMAND", "dummy")
    monkeypatch.setenv("QUICKBOOKS_CLIENT_ID", "id")
    monkeypatch.setenv("QUICKBOOKS_CLIENT_SECRET", "s")
    monkeypatch.setenv("QUICKBOOKS_REALM_ID", "r")
    monkeypatch.setenv("QUICKBOOKS_REFRESH_TOKEN", "t")
    result = runner.invoke(app, ["sync", "--entity", "widgets"])
    assert result.exit_code != 0
    assert "unknown entity" in (result.stdout + result.stderr).lower()


def test_ask_is_a_stub(runner: CliRunner) -> None:
    result = runner.invoke(app, ["ask", "what was revenue?"])
    assert result.exit_code == 0
    assert "(stub)" in result.stdout.lower()
    assert "revenue" in result.stdout


def test_chat_is_a_stub(runner: CliRunner) -> None:
    result = runner.invoke(app, ["chat"])
    assert result.exit_code == 0
    assert "(stub)" in result.stdout.lower()


def test_doctor_exits_nonzero_when_llm_missing(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("QBAGENT_LLM_MODEL", "QBAGENT_LLM_API_KEY", "QBAGENT_MCP_SERVER_COMMAND"):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "MISSING" in result.stdout


def test_doctor_exits_zero_when_everything_configured(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QBAGENT_LLM_MODEL", "anthropic/claude-opus-4-7")
    monkeypatch.setenv("QBAGENT_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("QBAGENT_MCP_SERVER_COMMAND", "npx")
    monkeypatch.setenv("QBAGENT_MCP_SERVER_ARGS", "-y @intuit/mcp-server-quickbooks")
    monkeypatch.setenv("QUICKBOOKS_CLIENT_ID", "id")
    monkeypatch.setenv("QUICKBOOKS_CLIENT_SECRET", "secret")
    monkeypatch.setenv("QUICKBOOKS_REALM_ID", "123")
    monkeypatch.setenv("QUICKBOOKS_REFRESH_TOKEN", "rt")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "MISSING" not in result.stdout
