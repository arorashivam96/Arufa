"""Async wrapper around Azure OpenAI chat completions.

Every LLM call in Arufa flows through this module. The client owns:

* Retry with ``Retry-After`` / ``Retry-After-Ms`` honouring (the OpenAI
  SDK does not do this by default against AOAI throttling).
* A per-attempt timeout kept below the platform's 60 s per-call ceiling.
* An asyncio semaphore that caps concurrent AOAI calls per replica.
* Recording of the model name + token counts into
  :data:`arufa.shared.observability.llm_call_var` so that response
  headers pick them up regardless of success or failure.

Auth modes:
* ``key``: reads ``AOAI_API_KEY``. Used for local dev when Entra ID is
  unavailable; also the historical default before the M15 MI migration.
* ``aad``: acquires a bearer token via ``DefaultAzureCredential``. In the
  Container App this resolves to the system-assigned managed identity;
  locally it falls through to Azure CLI credential (``az login``).
  Tokens are cached by the credential and refreshed automatically.
  Required by the S360 Safe-Secrets standard (SFI-ID4.3.1) for
  Cognitive Services / AOAI resources.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

import httpx

from arufa.shared.config import Settings
from arufa.shared.llm.errors import LLMUnavailable
from arufa.shared.llm.result import LLMResult
from arufa.shared.observability import get_logger
from arufa.shared.observability import record_llm_call

logger = get_logger(__name__)

# HTTP status codes that get retried. Everything else is terminal.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Max sleep between retries. Prevents any single wait from blowing the
# 60 s platform deadline; also caps AOAI Retry-After suggestions.
_MAX_SLEEP_S = 10.0

# Cognitive Services token scope for AOAI. Same for all regions.
_AOAI_SCOPE = "https://cognitiveservices.azure.com/.default"

# Refresh a cached token this many seconds before its stated expiry to
# avoid racing an expiry between header build and HTTP send.
_TOKEN_REFRESH_LEEWAY_S = 60.0


class LLMClient:
    """Reusable async chat-completion client.

    A single instance is created at startup, shared across pipelines,
    and closed at shutdown. Tests pass in a mocked
    :class:`httpx.AsyncClient` (typically with an :class:`httpx.MockTransport`).
    """

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient()
        self._sem = asyncio.Semaphore(settings.llm_max_concurrency)
        self._sleep: Callable[[float], Awaitable[None]] = sleep_fn or asyncio.sleep
        # Lazily-constructed Entra ID credential for the ``aad`` auth mode.
        # The credential itself caches tokens; we cache the raw string +
        # expiry so header builds don't need an await round-trip on the
        # hot path. Guarded by a lock because ``chat()`` is fan-in from
        # many coroutines within one event loop but the credential's
        # internal state is fine either way.
        self._aad_credential: Any = None
        self._cached_token: str | None = None
        self._cached_token_expires_at: float = 0.0
        self._token_lock = threading.Lock()

    async def aclose(self) -> None:
        """Close the underlying HTTP client if we own it."""
        if self._owns_client:
            await self._http.aclose()
        # Best-effort close of the sync Entra credential if it was
        # created. ``DefaultAzureCredential`` from ``azure.identity``
        # (sync) exposes ``close()`` on most sub-credentials; no-op if
        # already closed.
        cred = self._aad_credential
        if cred is not None:
            close = getattr(cred, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # pragma: no cover - defensive
                    pass

    async def chat(
        self,
        *,
        deployment: str,
        model_name: str,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None = None,
        max_completion_tokens: int = 2048,
        reasoning_effort: str | None = None,
        timeout_s: float | None = None,
    ) -> LLMResult:
        """Call an AOAI chat-completion deployment with retry + backoff.

        Parameters
        ----------
        deployment:
            AOAI deployment name (e.g. ``gpt-5-nano``).
        model_name:
            Canonical model name written to the ``X-Model-Name`` response
            header. This is what FDEBench keys cost tier off — it must
            match one of the names in the cost table (see
            ``docs/eval/fdebench.md``).
        messages:
            OpenAI-format chat messages (``[{"role": ..., "content": ...}]``).
        response_format:
            Optional ``response_format`` payload; for JSON mode pass
            ``{"type": "json_object"}``; for schema mode pass
            ``{"type": "json_schema", "json_schema": {...}}``.
        max_completion_tokens:
            Upper bound on completion tokens *including* reasoning tokens
            for reasoning models. Default 2048.
        reasoning_effort:
            For gpt-5-* reasoning models. Pass ``"minimal"`` on classifier
            calls where we don't want the model to spend budget thinking.
        timeout_s:
            Per-attempt HTTP timeout. Falls back to
            :attr:`Settings.llm_timeout_s`.

        Raises
        ------
        LLMUnavailable
            Retry budget exhausted, or non-retryable HTTP error, or config
            missing (e.g. API key not set).
        """
        url = self._build_url(deployment)
        headers = self._build_headers()

        body: dict[str, Any] = {
            "messages": messages,
            "max_completion_tokens": max_completion_tokens,
        }
        if response_format is not None:
            body["response_format"] = response_format
        if reasoning_effort is not None:
            body["reasoning_effort"] = reasoning_effort

        timeout = timeout_s if timeout_s is not None else self._settings.llm_timeout_s
        max_retries = self._settings.llm_max_retries

        async with self._sem:
            for attempt in range(1, max_retries + 1):
                try:
                    response = await self._http.post(
                        url, json=body, headers=headers, timeout=timeout
                    )
                except httpx.TimeoutException:
                    logger.warning("llm_timeout", attempt=attempt, deployment=deployment)
                    await self._backoff_or_raise(None, attempt, max_retries, "timeout")
                    continue
                except httpx.HTTPError as exc:
                    logger.warning(
                        "llm_transport_error", attempt=attempt, error=str(exc), deployment=deployment
                    )
                    await self._backoff_or_raise(None, attempt, max_retries, "transport_error")
                    continue

                if response.status_code == 200:
                    payload = response.json()
                    return self._to_result(payload, model_name)

                if response.status_code in _RETRYABLE_STATUSES:
                    logger.warning(
                        "llm_retryable_status",
                        attempt=attempt,
                        status=response.status_code,
                        deployment=deployment,
                    )
                    await self._backoff_or_raise(
                        response, attempt, max_retries, f"http_{response.status_code}"
                    )
                    continue

                # Non-retryable HTTP error — bail out immediately.
                detail = f"aoai returned {response.status_code}: {response.text[:200]}"
                logger.error("llm_terminal_status", status=response.status_code, detail=detail)
                raise LLMUnavailable(detail, attempts=attempt)

        # Loop should always return or raise. Defensive fallback.
        raise LLMUnavailable("retry budget exhausted", attempts=max_retries)

    # ---- helpers ---------------------------------------------------

    async def _backoff_or_raise(
        self,
        response: httpx.Response | None,
        attempt: int,
        max_retries: int,
        reason: str,
    ) -> None:
        """Sleep for backoff, or raise :class:`LLMUnavailable` if budget spent."""
        if attempt >= max_retries:
            raise LLMUnavailable(reason, attempts=attempt)

        retry_after = self._retry_after_seconds(response)
        if retry_after is None:
            retry_after = float(min(2 ** (attempt - 1), _MAX_SLEEP_S))
        else:
            retry_after = min(retry_after, _MAX_SLEEP_S)

        await self._sleep(retry_after)

    @staticmethod
    def _retry_after_seconds(response: httpx.Response | None) -> float | None:
        """Parse ``Retry-After-Ms`` (Azure) or ``Retry-After`` (RFC 7231)."""
        if response is None:
            return None
        h = response.headers
        if "retry-after-ms" in h:
            try:
                return float(h["retry-after-ms"]) / 1000.0
            except ValueError:
                pass
        if "retry-after" in h:
            try:
                return float(h["retry-after"])
            except ValueError:
                pass
        return None

    def _build_url(self, deployment: str) -> str:
        base = self._settings.aoai_endpoint.rstrip("/")
        return (
            f"{base}/openai/deployments/{deployment}/chat/completions"
            f"?api-version={self._settings.aoai_api_version}"
        )

    def _build_headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._settings.aoai_auth_mode == "key":
            if not self._settings.aoai_api_key:
                raise LLMUnavailable("AOAI_API_KEY not set", attempts=0)
            headers["api-key"] = self._settings.aoai_api_key
            return headers
        # ``aad`` — bearer token via Entra ID. In-cluster this resolves
        # to the ACA system-assigned managed identity (must have the
        # ``Cognitive Services OpenAI User`` role on the AOAI account);
        # locally it falls through to Azure CLI credential.
        token = self._get_aad_token()
        headers["authorization"] = f"Bearer {token}"
        return headers

    def _get_aad_token(self) -> str:
        """Return a cached bearer token, refreshing near expiry.

        Uses the *sync* ``azure.identity.DefaultAzureCredential`` under a
        thread lock. The credential's ``get_token`` does its own HTTP
        under the hood, but token acquisition is not on the per-request
        hot path once cached (typical AOAI token TTL is ~1 hour), so a
        brief blocking call here is acceptable versus the extra async
        plumbing the async credential would require.
        """
        now = time.time()
        cached = self._cached_token
        if cached is not None and now < self._cached_token_expires_at - _TOKEN_REFRESH_LEEWAY_S:
            return cached
        with self._token_lock:
            # Re-check under the lock — another thread may have refreshed.
            cached = self._cached_token
            if cached is not None and now < self._cached_token_expires_at - _TOKEN_REFRESH_LEEWAY_S:
                return cached
            if self._aad_credential is None:
                try:
                    # Deferred import so that unit tests + the ``key``
                    # auth path do not pull in ``azure-identity`` on
                    # module import.
                    from azure.identity import DefaultAzureCredential
                except ImportError as exc:  # pragma: no cover
                    raise LLMUnavailable(
                        f"azure-identity not installed: {exc}",
                        attempts=0,
                    ) from exc
                self._aad_credential = DefaultAzureCredential(
                    exclude_interactive_browser_credential=True,
                )
            try:
                access = self._aad_credential.get_token(_AOAI_SCOPE)
            except Exception as exc:
                raise LLMUnavailable(
                    f"Entra token acquisition failed: {type(exc).__name__}: {exc}",
                    attempts=0,
                ) from exc
            self._cached_token = access.token
            self._cached_token_expires_at = float(access.expires_on)
            return access.token

    @staticmethod
    def _to_result(payload: dict[str, Any], model_name: str) -> LLMResult:
        choices = payload.get("choices") or []
        content = ""
        if choices:
            content = choices[0].get("message", {}).get("content") or ""
        usage = payload.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        record_llm_call(model_name, prompt_tokens, completion_tokens)
        return LLMResult(
            content=content,
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            raw=payload,
        )
