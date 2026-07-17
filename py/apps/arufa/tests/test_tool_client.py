"""Tests for :mod:`arufa.orchestrate.tool_client` — pure HTTP behaviour.

Uses :class:`httpx.MockTransport` so no real network. Verifies:

* Successful 2xx JSON response → ``success=True``, payload parsed
* 5xx retried once, then either succeeds or reports terminal error
* 4xx not retried
* Timeout is caught, returns ``success=False`` (never raises)
* Non-JSON 200 body falls back to text
"""

from __future__ import annotations

import httpx

from arufa.orchestrate.tool_client import ToolClient


async def _client(handler: httpx.MockTransport) -> ToolClient:
    return ToolClient(http_client=httpx.AsyncClient(transport=handler))


async def test_success_returns_parsed_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accounts": [{"id": "ACC-1"}]})

    client = await _client(httpx.MockTransport(handler))
    result = await client.call("http://tool/x", {"filter": "y"})
    assert result.success is True
    assert result.status_code == 200
    assert result.payload == {"accounts": [{"id": "ACC-1"}]}
    assert result.error is None
    await client.aclose()


async def test_4xx_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    client = await _client(httpx.MockTransport(handler))
    result = await client.call("http://tool/x", {})
    assert result.success is False
    assert result.status_code == 400
    assert result.error == "http_400"
    assert calls["n"] == 1
    await client.aclose()


async def test_5xx_retried_once_then_success() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="down")
        return httpx.Response(200, json={"ok": True})

    client = await _client(httpx.MockTransport(handler))
    result = await client.call("http://tool/x", {})
    assert result.success is True
    assert calls["n"] == 2
    await client.aclose()


async def test_5xx_retried_once_then_still_failing() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="down")

    client = await _client(httpx.MockTransport(handler))
    result = await client.call("http://tool/x", {})
    assert result.success is False
    assert result.error == "http_500"
    assert calls["n"] == 2  # retried once
    await client.aclose()


async def test_timeout_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    client = await _client(httpx.MockTransport(handler))
    result = await client.call("http://tool/x", {})
    assert result.success is False
    assert result.error == "timeout"
    await client.aclose()


async def test_non_json_body_falls_back_to_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="plain text response")

    client = await _client(httpx.MockTransport(handler))
    result = await client.call("http://tool/x", {})
    assert result.success is True
    assert result.payload == "plain text response"
    await client.aclose()
