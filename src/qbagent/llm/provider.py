"""LLM provider — the only place in qbagent that makes LLM calls.

Everything in the agent graph and elsewhere goes through :class:`LLMProvider`.
The concrete :class:`LiteLLMProvider` wraps LiteLLM so qbagent never binds to
a specific vendor; tests use :class:`FakeLLMProvider` and never touch the
network.

Retries are handled with ``tenacity`` on transient errors (rate limits,
connection issues, timeouts). Authentication and validation errors fail fast
with no retry, since retrying would just waste tokens.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import structlog
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from qbagent.config import Settings

log = structlog.get_logger(__name__)

MessageRole = Literal["system", "user", "assistant", "tool"]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class LLMMessage:
    role: MessageRole
    content: str
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(slots=True)
class LLMResponse:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    retries: int = 0
    finish_reason: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: Mapping[str, Any] | None = None


class LLMError(RuntimeError):
    """Raised when an LLM call fails after all retries."""


@runtime_checkable
class LLMProvider(Protocol):
    """Interface every provider (real or fake) must satisfy."""

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: Mapping[str, Any] | None = None,
    ) -> LLMResponse: ...

    async def chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: Mapping[str, Any] | None = None,
    ) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# LiteLLM concrete provider
# ---------------------------------------------------------------------------


# Imported lazily inside _call to avoid paying the (heavy) litellm import cost
# for unit tests that only use the fake provider.
_TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] | None = None


def _transient_exceptions() -> tuple[type[BaseException], ...]:
    """Return the set of litellm exception classes we should retry on.

    Cached after first call. Falls back to ``(TimeoutError, ConnectionError)``
    if litellm isn't installed.
    """
    global _TRANSIENT_EXCEPTIONS
    if _TRANSIENT_EXCEPTIONS is not None:
        return _TRANSIENT_EXCEPTIONS
    try:
        import litellm.exceptions as le

        _TRANSIENT_EXCEPTIONS = tuple(
            cls
            for cls in (
                getattr(le, "APIConnectionError", None),
                getattr(le, "APIError", None),
                getattr(le, "Timeout", None),
                getattr(le, "RateLimitError", None),
                getattr(le, "ServiceUnavailableError", None),
                getattr(le, "InternalServerError", None),
            )
            if isinstance(cls, type)
        )
    except ImportError:
        _TRANSIENT_EXCEPTIONS = (TimeoutError, ConnectionError)
    return _TRANSIENT_EXCEPTIONS


class LiteLLMProvider:
    """LiteLLM-backed provider.

    All kwargs passed to :meth:`complete` and :meth:`chat` override the
    per-call defaults pulled from :class:`Settings`.
    """

    def __init__(self, settings: Settings) -> None:
        settings.require_llm()
        self._settings = settings
        assert settings.llm_model is not None
        self._model: str = settings.llm_model
        self._api_key: str = settings.llm_api_key or ""
        self._api_base: str | None = settings.llm_api_base
        self._default_temperature = settings.llm_temperature
        self._default_max_tokens = settings.llm_max_tokens
        self._timeout = settings.llm_request_timeout
        self._max_retries = settings.llm_max_retries

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: Mapping[str, Any] | None = None,
    ) -> LLMResponse:
        messages: list[LLMMessage] = []
        if system:
            messages.append(LLMMessage(role="system", content=system))
        messages.append(LLMMessage(role="user", content=prompt))
        return await self.chat(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
        )

    async def chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: Mapping[str, Any] | None = None,
    ) -> LLMResponse:
        return await self._call(
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
        )

    async def _call(
        self,
        *,
        messages: Sequence[LLMMessage],
        tools: Sequence[Mapping[str, Any]] | None,
        max_tokens: int | None,
        temperature: float | None,
        response_format: Mapping[str, Any] | None,
    ) -> LLMResponse:
        import litellm

        kwargs: dict[str, Any] = {
            "model": self._model,
            "api_key": self._api_key,
            "messages": [_message_to_dict(m) for m in messages],
            "temperature": self._default_temperature if temperature is None else temperature,
            "max_tokens": self._default_max_tokens if max_tokens is None else max_tokens,
            "timeout": self._timeout,
        }
        if self._api_base:
            kwargs["api_base"] = self._api_base
        if tools:
            kwargs["tools"] = list(tools)
        if response_format:
            kwargs["response_format"] = dict(response_format)

        attempt_tracker: dict[str, int] = {"count": 0}

        def _log_retry(state: RetryCallState) -> None:
            attempt_tracker["count"] = state.attempt_number
            log.info(
                "llm.retry",
                model=self._model,
                attempt=state.attempt_number,
                exc_type=type(state.outcome.exception()).__name__
                if state.outcome and state.outcome.failed
                else None,
            )

        retryer = retry(
            reraise=True,
            retry=retry_if_exception_type(_transient_exceptions()),
            # tenacity's ``stop_after_attempt(N)`` allows N retries before
            # giving up (total calls = N + 1).
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
            before_sleep=_log_retry,
        )

        start = time.perf_counter()
        try:
            raw = await retryer(litellm.acompletion)(**kwargs)
        except Exception as exc:
            log.warning(
                "llm.failed",
                model=self._model,
                exc_type=type(exc).__name__,
                retries=attempt_tracker["count"],
            )
            raise LLMError(str(exc)) from exc
        latency_ms = int((time.perf_counter() - start) * 1000)
        response = _parse_litellm_response(raw, model=self._model, latency_ms=latency_ms)
        response.retries = attempt_tracker["count"]
        log.info(
            "llm.call",
            model=response.model,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens,
            latency_ms=response.latency_ms,
            retries=response.retries,
            finish_reason=response.finish_reason,
        )
        log.debug(
            "llm.payload",
            messages=kwargs["messages"],
            response=response.content,
        )
        return response


def _message_to_dict(msg: LLMMessage) -> dict[str, Any]:
    d: dict[str, Any] = {"role": msg.role, "content": msg.content}
    if msg.tool_call_id:
        d["tool_call_id"] = msg.tool_call_id
    if msg.name:
        d["name"] = msg.name
    return d


def _parse_litellm_response(raw: Any, *, model: str, latency_ms: int) -> LLMResponse:
    """Extract the useful bits from a LiteLLM response object."""
    choice = raw.choices[0] if getattr(raw, "choices", None) else None
    message = getattr(choice, "message", None) if choice else None
    content = getattr(message, "content", None) or ""
    finish_reason = getattr(choice, "finish_reason", None) if choice else None

    usage = getattr(raw, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    total_tokens = int(getattr(usage, "total_tokens", 0) or (prompt_tokens + completion_tokens))

    tool_calls_raw = getattr(message, "tool_calls", None) or []
    tool_calls: list[dict[str, Any]] = []
    for tc in tool_calls_raw:
        tc_dict = tc.model_dump() if hasattr(tc, "model_dump") else dict(tc)
        tool_calls.append(tc_dict)

    return LLMResponse(
        content=content,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
        finish_reason=finish_reason,
        tool_calls=tool_calls,
    )


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


class FakeLLMProvider:
    """Scripted provider for tests.

    Pass a list of responses; each :meth:`complete` / :meth:`chat` call pops
    the next one. Bare strings are wrapped into :class:`LLMResponse` objects.
    Every call is recorded on :attr:`calls` for later assertion.
    """

    def __init__(self, responses: Sequence[str | LLMResponse] | None = None) -> None:
        self._queue: list[LLMResponse] = [_coerce(r) for r in (responses or [])]
        self.calls: list[dict[str, Any]] = []

    def enqueue(self, response: str | LLMResponse) -> None:
        self._queue.append(_coerce(response))

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: Mapping[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "kind": "complete",
                "prompt": prompt,
                "system": system,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "response_format": response_format,
            }
        )
        return self._pop()

    async def chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: Mapping[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "kind": "chat",
                "messages": list(messages),
                "tools": list(tools) if tools else None,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "response_format": response_format,
            }
        )
        return self._pop()

    def _pop(self) -> LLMResponse:
        if not self._queue:
            raise AssertionError("FakeLLMProvider script exhausted — enqueue more responses")
        return self._queue.pop(0)


def _coerce(response: str | LLMResponse) -> LLMResponse:
    if isinstance(response, LLMResponse):
        return response
    return LLMResponse(content=response, model="fake")
