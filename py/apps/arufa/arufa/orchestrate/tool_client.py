"""Async HTTP client for calling T3 tool endpoints.

The scorer inspects real execution: every ``steps_executed[]`` entry
must correspond to an HTTP call we actually made. So the client:

* POSTs the step's parameters as JSON to the tool endpoint
* Enforces a per-call timeout (much shorter than the LLM timeout — tools
  are HTTP calls to a mock service, expected to answer in tens of ms)
* Retries once on 5xx with a small backoff
* Never crashes: on any failure returns ``ToolCallResult(success=False,
  error="...")`` so the workflow can continue and record the skip
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from arufa.shared.observability import get_logger

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_S = 5.0
_RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """Outcome of a single tool call."""

    success: bool
    status_code: int | None
    payload: Any
    error: str | None = None


class ToolClient:
    """Thin async HTTP wrapper for T3 tool endpoints."""

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient()
        self._timeout_s = timeout_s

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def call(self, url: str, params: dict[str, Any]) -> ToolCallResult:
        """POST ``params`` as JSON to ``url``. Never raises."""
        for attempt in (1, 2):
            try:
                response = await self._http.post(url, json=params, timeout=self._timeout_s)
            except httpx.TimeoutException:
                logger.warning("tool_timeout", url=url, attempt=attempt)
                if attempt == 2:
                    return ToolCallResult(False, None, None, "timeout")
                await asyncio.sleep(0.2)
                continue
            except httpx.HTTPError as exc:
                logger.warning("tool_transport_error", url=url, attempt=attempt, error=str(exc))
                if attempt == 2:
                    return ToolCallResult(False, None, None, f"transport_error: {exc}")
                await asyncio.sleep(0.2)
                continue

            if 200 <= response.status_code < 300:
                try:
                    payload = response.json()
                except ValueError:
                    payload = response.text
                return ToolCallResult(True, response.status_code, payload)

            if response.status_code in _RETRYABLE_STATUSES and attempt == 1:
                logger.warning(
                    "tool_5xx_retryable", url=url, status=response.status_code
                )
                await asyncio.sleep(0.2)
                continue

            # Terminal error (4xx or exhausted retries)
            return ToolCallResult(
                False,
                response.status_code,
                None,
                f"http_{response.status_code}",
            )

        return ToolCallResult(False, None, None, "retry_budget_exhausted")
