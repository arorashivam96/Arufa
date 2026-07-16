"""Tests for :class:`arufa.shared.middleware.RequestContextMiddleware`.

Verifies that response headers are populated for both:

* success paths that make an LLM call (via manually setting the contextvar
  from a fake route);
* success paths that do not make an LLM call (``X-Model-Name`` absent).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arufa.shared.middleware import RequestContextMiddleware
from arufa.shared.observability import record_llm_call


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/no-llm")
    async def _no_llm() -> dict[str, str]:
        return {"ok": "true"}

    @app.get("/with-llm")
    async def _with_llm() -> dict[str, str]:
        # Simulate a pipeline that called the LLM client.
        record_llm_call(model_name="gpt-5-nano", prompt_tokens=10, completion_tokens=20)
        return {"ok": "true"}

    return app


def test_no_llm_call_headers() -> None:
    client = TestClient(_make_app())
    r = client.get("/no-llm")
    assert r.status_code == 200
    assert r.headers.get("x-request-id")
    assert r.headers.get("x-latency-ms")
    assert "x-model-name" not in r.headers
    assert "x-token-count" not in r.headers


def test_with_llm_call_headers() -> None:
    client = TestClient(_make_app())
    r = client.get("/with-llm")
    assert r.status_code == 200
    assert r.headers["x-model-name"] == "gpt-5-nano"
    assert r.headers["x-token-count"] == "30"
    assert r.headers.get("x-request-id")


def test_context_reset_between_requests() -> None:
    """Second request must not inherit the first's LLM call info."""
    client = TestClient(_make_app())
    r1 = client.get("/with-llm")
    assert r1.headers["x-model-name"] == "gpt-5-nano"
    r2 = client.get("/no-llm")
    assert "x-model-name" not in r2.headers


def test_request_id_is_unique_per_request() -> None:
    client = TestClient(_make_app())
    r1 = client.get("/no-llm")
    r2 = client.get("/no-llm")
    assert r1.headers["x-request-id"] != r2.headers["x-request-id"]
