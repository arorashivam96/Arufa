"""HTTP middleware: request ID, latency, and cost-tracking response headers.

Implemented as a **pure ASGI middleware** (not ``BaseHTTPMiddleware``)
so ``ContextVar`` writes made inside the endpoint remain visible when
the endpoint calls ``send``. ``BaseHTTPMiddleware`` spawns the endpoint
in a child task with a copied context and silently drops
``llm_call_var`` writes made by the LLM client.

Every request (successful or failed) picks up:

* ``X-Request-Id``   correlation ID for logs
* ``X-Latency-Ms``   integer milliseconds spent in the handler
* ``X-Model-Name``   canonical model name for FDEBench cost tier
  scoring — only set when the pipeline actually invoked the LLM client
* ``X-Token-Count``  prompt + completion tokens on the last LLM call
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

from arufa.shared.observability import LLMCallInfo
from arufa.shared.observability import get_logger
from arufa.shared.observability import llm_call_var
from arufa.shared.observability import request_id_var

logger = get_logger(__name__)


class RequestContextMiddleware:
    """Pure ASGI middleware for request context + telemetry headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        req_id = str(uuid.uuid4())
        request_id_var.set(req_id)
        # Reset per-request LLM info; a stale value from an earlier request
        # in the same ambient context must not leak in.
        llm_call_var.set(LLMCallInfo())

        start = time.perf_counter()
        status_holder: dict[str, Any] = {"status": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                latency_ms = int((time.perf_counter() - start) * 1000)
                info = llm_call_var.get()
                status_holder["status"] = message["status"]

                headers = MutableHeaders(scope=message)
                headers["x-request-id"] = req_id
                headers["x-latency-ms"] = str(latency_ms)
                if info.model_name:
                    headers["x-model-name"] = info.model_name
                total_tokens = info.prompt_tokens + info.completion_tokens
                if total_tokens > 0:
                    headers["x-token-count"] = str(total_tokens)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            info = llm_call_var.get()
            logger.info(
                "request_complete",
                req_id=req_id,
                method=scope.get("method"),
                path=scope.get("path"),
                status=status_holder["status"],
                latency_ms=int((time.perf_counter() - start) * 1000),
                model_name=info.model_name or None,
                prompt_tokens=info.prompt_tokens or None,
                completion_tokens=info.completion_tokens or None,
            )
