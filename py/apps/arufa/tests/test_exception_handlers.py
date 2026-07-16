"""Tests for the M1 exception handlers.

Populated-body-with-invalid-fields → HTTP 200 + envelope will be added in
M2 (per-task). For now, we assert the FastAPI default 422 remains
functional and that response headers still flow through the middleware
on the error path (probe 1/2/3 requirement).

We do **not** test the 500 fallback because Starlette's
``ServerErrorMiddleware`` handles unhandled exceptions outside our
middleware stack; a global ``Exception`` handler would be silently
unreachable. Pipelines catch their own exceptions at the route boundary
starting in M2 (see ``docs/challenge/README.md`` HTTP semantics section).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI
from fastapi import Query
from fastapi.testclient import TestClient
from pydantic import BaseModel

from arufa.shared import exception_handlers
from arufa.shared.middleware import RequestContextMiddleware


class _Payload(BaseModel):
    name: str
    count: int


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    exception_handlers.register(app)

    @app.post("/echo")
    async def _echo(payload: _Payload) -> dict[str, str | int]:
        return {"name": payload.name, "count": payload.count}

    @app.get("/require-query")
    async def _require_query(size: Annotated[int, Query()]) -> dict[str, int]:
        return {"size": size}

    return app


def test_malformed_json_body_returns_4xx() -> None:
    """Probe 1: malformed JSON → 400 or 422 acceptable per FDEBench."""
    client = TestClient(_make_app())
    r = client.post("/echo", content=b'{"broken', headers={"content-type": "application/json"})
    assert r.status_code in (400, 422)
    # Middleware headers still applied on error path
    assert r.headers.get("x-request-id")
    assert r.headers.get("x-latency-ms")


def test_empty_body_returns_4xx() -> None:
    """Probe 2: empty ``{}`` body → 400 or 422."""
    client = TestClient(_make_app())
    r = client.post("/echo", json={})
    assert r.status_code in (400, 422)
    assert r.headers.get("x-request-id")


def test_missing_required_query_returns_4xx() -> None:
    """Probe 3: missing required field → 400 or 422 or valid response w/ defaults."""
    client = TestClient(_make_app())
    r = client.get("/require-query")
    assert r.status_code in (400, 422)
    assert r.headers.get("x-request-id")


def test_wrong_content_type_does_not_crash() -> None:
    """Probe 5 regression: text/plain body must not surface as HTTP 500.

    The Pydantic validation error's ``input`` field contains raw bytes
    when Content-Type isn't JSON. Naïve ``json.dumps`` on that crashes;
    ``jsonable_encoder`` fixes it so we return a clean 4xx.
    """
    client = TestClient(_make_app())
    body = '{"name": "alice", "count": 3}'  # valid JSON payload
    r = client.post("/echo", content=body, headers={"content-type": "text/plain"})
    # Probe 5 accepts 415, 200 w/ valid JSON, 400, or 422.
    assert r.status_code in (200, 400, 415, 422), f"got {r.status_code}"
    # Must be parseable JSON in all cases
    r.json()


def test_valid_body_passes_through() -> None:
    client = TestClient(_make_app())
    r = client.post("/echo", json={"name": "alice", "count": 3})
    assert r.status_code == 200
    assert r.json() == {"name": "alice", "count": 3}
