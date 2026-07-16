"""Exception handlers implementing the 200-vs-4xx contract.

FDEBench treats any non-200 on a valid request as ``items_errored`` — 0.0
across every dimension of that record. So:

* Malformed HTTP / JSON (probes 1, 2, 3) → 4xx (FastAPI default).
* Populated-body-that-fails-Pydantic → **200 + task envelope with
  ``errors[]``**. This is handled per-route in M2 (each pipeline needs
  its own envelope shape). For now, ``RequestValidationError`` returns
  the FastAPI default (422) — safe until scored endpoints exist.
* Unhandled server exceptions bubble past this layer to Starlette's
  built-in ``ServerErrorMiddleware`` and produce a bare 500. Pipelines
  must catch their own exceptions at the route boundary (M2 pattern) —
  do **not** rely on a global ``Exception`` handler: Starlette's
  ``ServerErrorMiddleware`` intercepts before ours can run.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from arufa.shared.observability import get_logger

logger = get_logger(__name__)


async def request_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Return 422 with FastAPI's standard error payload.

    Uses :func:`jsonable_encoder` because Pydantic's ``.errors()`` may
    include raw ``bytes`` in the ``input`` field (e.g. when a request
    arrives with a non-JSON ``Content-Type``); ``json.dumps`` cannot
    serialise bytes and would otherwise crash the handler → 500.

    Populated-body-with-invalid-fields will be re-routed to
    ``HTTP 200 + envelope`` in M2 when we own the task response shapes.
    """
    errors = jsonable_encoder(exc.errors())
    logger.info(
        "request_validation_error",
        path=request.url.path,
        errors=[{"loc": e.get("loc"), "msg": e.get("msg")} for e in errors[:5]],
    )
    return JSONResponse(
        status_code=422,
        content={"detail": errors},
    )


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    """Pass HTTP exceptions through with their status code."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


def register(app: FastAPI) -> None:
    """Wire the handlers onto ``app``.

    We deliberately do **not** register a handler for bare ``Exception``:
    Starlette's ``ServerErrorMiddleware`` intercepts unhandled exceptions
    outside our middleware stack, so a global ``Exception`` handler would
    be silently unreachable. M2 pipelines catch their own errors and
    return 200 + task envelope instead.
    """
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
