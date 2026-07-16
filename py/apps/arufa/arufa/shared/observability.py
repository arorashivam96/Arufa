"""Observability primitives: structlog config + per-request headers.

The LLM client writes model name and token counts into :data:`llm_call_var`
after every successful attempt. The request middleware reads that
``ContextVar`` and copies the values into response headers before the
response is flushed. This keeps the failure path observable too: if the
handler crashes before setting headers, the middleware sees the empty
default and still emits a well-formed response.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field

import structlog


@dataclass
class LLMCallInfo:
    """The last LLM call made during a single request. Empty by default."""

    model_name: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


llm_call_var: ContextVar[LLMCallInfo] = ContextVar("arufa_llm_call", default=LLMCallInfo())
"""Populated by :class:`arufa.shared.llm.client.LLMClient` after each call."""

request_id_var: ContextVar[str] = ContextVar("arufa_request_id", default="")
"""Populated by :class:`arufa.shared.middleware.RequestContextMiddleware`."""


def record_llm_call(model_name: str, prompt_tokens: int, completion_tokens: int) -> None:
    """Record the most recent LLM call for the current request context.

    Later calls in the same request overwrite earlier ones. If a pipeline
    makes multiple LLM calls, only the last one's model + tokens appear in
    response headers. That is intentional: the platform reads one
    ``X-Model-Name`` per response and totals cost from the last model
    seen. Log the fine-grained trace instead.
    """
    llm_call_var.set(
        LLMCallInfo(
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    )


def configure_logging(log_level: str) -> None:
    """Configure structlog to emit JSON to stdout at ``log_level``.

    Called once at application startup. Idempotent: repeated calls simply
    re-apply configuration.
    """
    level = logging.getLevelName(log_level.upper())
    if not isinstance(level, int):
        level = logging.INFO

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
            structlog.processors.EventRenamer("event"),
            structlog.processors.JSONRenderer(),
        ],
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to ``name``."""
    return structlog.get_logger(name)
