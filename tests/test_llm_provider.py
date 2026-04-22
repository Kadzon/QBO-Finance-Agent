"""Tests for the LLM provider layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from qbagent.config import Settings
from qbagent.llm.provider import (
    FakeLLMProvider,
    LiteLLMProvider,
    LLMError,
    LLMMessage,
    LLMResponse,
)

# ---------------------------------------------------------------------------
# FakeLLMProvider
# ---------------------------------------------------------------------------


async def test_fake_pops_responses_in_order() -> None:
    fake = FakeLLMProvider(["first", "second"])
    r1 = await fake.complete("q1")
    r2 = await fake.complete("q2")
    assert r1.content == "first"
    assert r2.content == "second"


async def test_fake_records_every_call() -> None:
    fake = FakeLLMProvider(["hi"])
    await fake.complete("hello?", system="be nice")
    assert fake.calls == [
        {
            "kind": "complete",
            "prompt": "hello?",
            "system": "be nice",
            "max_tokens": None,
            "temperature": None,
            "response_format": None,
        }
    ]


async def test_fake_chat_records_messages() -> None:
    fake = FakeLLMProvider([LLMResponse(content="ok", model="fake", total_tokens=3)])
    resp = await fake.chat([LLMMessage(role="user", content="hi")])
    assert resp.content == "ok"
    assert resp.total_tokens == 3
    assert fake.calls[0]["kind"] == "chat"
    assert fake.calls[0]["messages"] == [LLMMessage(role="user", content="hi")]


async def test_fake_raises_when_exhausted() -> None:
    fake = FakeLLMProvider([])
    with pytest.raises(AssertionError, match="script exhausted"):
        await fake.complete("anything")


async def test_fake_enqueue_appends_responses() -> None:
    fake = FakeLLMProvider()
    fake.enqueue("hello")
    fake.enqueue(LLMResponse(content="world", model="fake"))
    assert (await fake.complete("")).content == "hello"
    assert (await fake.complete("")).content == "world"


# ---------------------------------------------------------------------------
# LiteLLMProvider — mock the network, verify the glue
# ---------------------------------------------------------------------------


@dataclass
class _FakeUsage:
    prompt_tokens: int = 10
    completion_tokens: int = 5
    total_tokens: int = 15


@dataclass
class _FakeMessage:
    content: str = "SELECT 1"
    tool_calls: list[Any] | None = None


@dataclass
class _FakeChoice:
    message: _FakeMessage
    finish_reason: str = "stop"


@dataclass
class _FakeCompletion:
    choices: list[_FakeChoice]
    usage: _FakeUsage


def _build_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("QBAGENT_LLM_MODEL", "anthropic/claude-opus-4-7")
    monkeypatch.setenv("QBAGENT_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("QBAGENT_LLM_MAX_RETRIES", "3")
    return Settings(_env_file=None)  # type: ignore[call-arg]


async def test_litellm_provider_passes_messages_and_parses_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _build_settings(monkeypatch)
    provider = LiteLLMProvider(settings)

    fake_acompletion = AsyncMock(
        return_value=_FakeCompletion(
            choices=[_FakeChoice(message=_FakeMessage(content="SELECT 1"))],
            usage=_FakeUsage(),
        )
    )
    import litellm

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = await provider.complete("give me sql", system="you are a sql generator")

    assert resp.content == "SELECT 1"
    assert resp.prompt_tokens == 10
    assert resp.completion_tokens == 5
    assert resp.total_tokens == 15
    assert resp.model == "anthropic/claude-opus-4-7"
    assert resp.finish_reason == "stop"

    call_kwargs = fake_acompletion.await_args.kwargs
    assert call_kwargs["model"] == "anthropic/claude-opus-4-7"
    assert call_kwargs["api_key"] == "sk-test"
    assert call_kwargs["messages"] == [
        {"role": "system", "content": "you are a sql generator"},
        {"role": "user", "content": "give me sql"},
    ]


async def test_litellm_provider_retries_on_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm
    import litellm.exceptions as le

    settings = _build_settings(monkeypatch)
    provider = LiteLLMProvider(settings)

    transient = le.RateLimitError("rate limit", llm_provider="anthropic", model="claude")
    successful = _FakeCompletion(
        choices=[_FakeChoice(message=_FakeMessage(content="ok"))],
        usage=_FakeUsage(),
    )
    fake_acompletion = AsyncMock(side_effect=[transient, successful])
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = await provider.complete("q")
    assert resp.content == "ok"
    assert resp.retries == 1, "one retry happened after the initial failure"
    assert fake_acompletion.await_count == 2


async def test_litellm_provider_raises_llm_error_after_max_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm
    import litellm.exceptions as le

    monkeypatch.setenv("QBAGENT_LLM_MAX_RETRIES", "2")
    settings = _build_settings(monkeypatch)
    provider = LiteLLMProvider(settings)

    transient = le.APIConnectionError(message="nope", llm_provider="anthropic", model="claude")
    fake_acompletion = AsyncMock(side_effect=transient)
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    with pytest.raises(LLMError):
        await provider.complete("q")

    # max_retries=2 → initial attempt + 2 retries = 3 total calls
    assert fake_acompletion.await_count == 3


async def test_litellm_provider_does_not_retry_on_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm
    import litellm.exceptions as le

    settings = _build_settings(monkeypatch)
    provider = LiteLLMProvider(settings)

    auth = le.AuthenticationError(message="bad key", llm_provider="anthropic", model="claude")
    fake_acompletion = AsyncMock(side_effect=auth)
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    with pytest.raises(LLMError):
        await provider.complete("q")

    assert fake_acompletion.await_count == 1, "auth errors must not retry"


async def test_litellm_provider_rejects_missing_credentials() -> None:
    # No env vars set (conftest isolates us from .env)
    bare = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(Exception) as exc:
        LiteLLMProvider(bare)
    assert "LLM" in str(exc.value)
