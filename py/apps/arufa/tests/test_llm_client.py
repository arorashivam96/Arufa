"""Tests for :class:`arufa.shared.llm.client.LLMClient`.

Uses :class:`httpx.MockTransport` so no real HTTP happens. Sleep is
patched to a no-op so retry loops complete instantly.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from arufa.shared.config import Settings
from arufa.shared.llm import LLMClient
from arufa.shared.llm import LLMUnavailable
from arufa.shared.observability import LLMCallInfo
from arufa.shared.observability import llm_call_var

# Recorded sleeps let each test assert on backoff behaviour.
Sleeps = list[float]


def _make_client(
    settings: Settings,
    handler: httpx.MockTransport,
    sleeps: Sleeps,
) -> LLMClient:
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    http = httpx.AsyncClient(transport=handler)
    return LLMClient(settings=settings, http_client=http, sleep_fn=fake_sleep)


def _success_payload(model: str = "gpt-5-nano", content: str = "ok") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46},
    }


async def test_success_first_try_writes_contextvar(settings: Settings) -> None:
    llm_call_var.set(LLMCallInfo())  # reset

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_success_payload())

    sleeps: Sleeps = []
    client = _make_client(settings, httpx.MockTransport(handler), sleeps)
    result = await client.chat(
        deployment="gpt-5-nano",
        model_name="gpt-5-nano",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert result.content == "ok"
    assert result.model_name == "gpt-5-nano"
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 34
    assert sleeps == []  # no retries

    info = llm_call_var.get()
    assert info.model_name == "gpt-5-nano"
    assert info.prompt_tokens == 12
    assert info.completion_tokens == 34

    await client.aclose()


async def test_success_after_429_with_retry_after_header(settings: Settings) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, text="throttled")
        return httpx.Response(200, json=_success_payload())

    sleeps: Sleeps = []
    client = _make_client(settings, httpx.MockTransport(handler), sleeps)
    result = await client.chat(
        deployment="gpt-5-nano",
        model_name="gpt-5-nano",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert result.content == "ok"
    assert calls["n"] == 2
    assert sleeps == [2.0]  # honoured the header exactly
    await client.aclose()


async def test_retry_after_ms_header_honoured(settings: Settings) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after-ms": "500"})
        return httpx.Response(200, json=_success_payload())

    sleeps: Sleeps = []
    client = _make_client(settings, httpx.MockTransport(handler), sleeps)
    await client.chat(
        deployment="gpt-5-nano",
        model_name="gpt-5-nano",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert sleeps == [0.5]
    await client.aclose()


async def test_retry_after_capped_at_10s(settings: Settings) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"retry-after": "999"})
        return httpx.Response(200, json=_success_payload())

    sleeps: Sleeps = []
    client = _make_client(settings, httpx.MockTransport(handler), sleeps)
    await client.chat(
        deployment="gpt-5-nano",
        model_name="gpt-5-nano",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert sleeps == [10.0]  # capped
    await client.aclose()


async def test_exponential_backoff_when_no_retry_after(settings: Settings) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json=_success_payload())

    sleeps: Sleeps = []
    client = _make_client(settings, httpx.MockTransport(handler), sleeps)
    await client.chat(
        deployment="gpt-5-nano",
        model_name="gpt-5-nano",
        messages=[{"role": "user", "content": "hi"}],
    )
    # 2^0 = 1s, then 2^1 = 2s
    assert sleeps == [1.0, 2.0]
    await client.aclose()


async def test_raises_llm_unavailable_after_max_retries(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="still down")

    sleeps: Sleeps = []
    client = _make_client(settings, httpx.MockTransport(handler), sleeps)
    with pytest.raises(LLMUnavailable) as excinfo:
        await client.chat(
            deployment="gpt-5-nano",
            model_name="gpt-5-nano",
            messages=[{"role": "user", "content": "hi"}],
        )
    # 3 attempts, 2 sleeps between them (no sleep after the last failing attempt).
    assert excinfo.value.attempts == 3
    assert len(sleeps) == 2
    await client.aclose()


async def test_timeout_counts_as_retryable(settings: Settings) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.TimeoutException("timed out", request=request)
        return httpx.Response(200, json=_success_payload())

    sleeps: Sleeps = []
    client = _make_client(settings, httpx.MockTransport(handler), sleeps)
    result = await client.chat(
        deployment="gpt-5-nano",
        model_name="gpt-5-nano",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert result.content == "ok"
    assert calls["n"] == 2
    assert sleeps == [1.0]  # exponential fallback (no Retry-After on timeout)
    await client.aclose()


async def test_non_retryable_4xx_bails_immediately(settings: Settings) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    sleeps: Sleeps = []
    client = _make_client(settings, httpx.MockTransport(handler), sleeps)
    with pytest.raises(LLMUnavailable) as excinfo:
        await client.chat(
            deployment="gpt-5-nano",
            model_name="gpt-5-nano",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert calls["n"] == 1
    assert excinfo.value.attempts == 1
    assert sleeps == []
    await client.aclose()


async def test_missing_api_key_raises_before_http(settings: Settings) -> None:
    settings_no_key = settings.model_copy(update={"aoai_api_key": None})

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not be reached — no HTTP should happen")

    sleeps: Sleeps = []
    client = _make_client(settings_no_key, httpx.MockTransport(handler), sleeps)
    with pytest.raises(LLMUnavailable):
        await client.chat(
            deployment="gpt-5-nano",
            model_name="gpt-5-nano",
            messages=[{"role": "user", "content": "hi"}],
        )
    await client.aclose()


async def test_request_url_and_headers(settings: Settings) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=_success_payload())

    sleeps: Sleeps = []
    client = _make_client(settings, httpx.MockTransport(handler), sleeps)
    await client.chat(
        deployment="gpt-5-nano",
        model_name="gpt-5-nano",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert captured["url"] == (
        "https://test-aoai.example.com/openai/deployments/gpt-5-nano/chat/completions"
        "?api-version=2024-10-21"
    )
    assert captured["headers"]["api-key"] == "test-key"
    await client.aclose()


async def test_reasoning_effort_and_response_format_included(settings: Settings) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_success_payload())

    sleeps: Sleeps = []
    client = _make_client(settings, httpx.MockTransport(handler), sleeps)
    await client.chat(
        deployment="gpt-5-nano",
        model_name="gpt-5-nano",
        messages=[{"role": "user", "content": "hi"}],
        response_format={"type": "json_object"},
        reasoning_effort="minimal",
        max_completion_tokens=512,
    )

    body = captured["body"]
    assert body["reasoning_effort"] == "minimal"
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_completion_tokens"] == 512
    await client.aclose()
