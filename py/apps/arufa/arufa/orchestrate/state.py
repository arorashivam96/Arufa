"""In-memory step trace and simple state accessors for T3 execution.

The scorer only reads ``steps_executed[]`` (plus the counter fields and
``constraints_satisfied``). We keep this module small and unopinionated:
build ``StepExecuted`` entries as we go, no cross-cutting state machine.
"""

from __future__ import annotations

from typing import Any

from arufa.shared.models.orchestrate import StepExecuted


def summarise(payload: Any, limit: int = 200) -> str:
    """One-line summary of a tool response for the trace.

    Keeps the response envelope small (the scorer doesn't need the full
    payload; big responses just bloat logs and network).
    """
    if payload is None:
        return ""
    if isinstance(payload, (dict, list)):
        keys = ",".join(list(payload.keys())[:5]) if isinstance(payload, dict) else f"len={len(payload)}"
        return f"{type(payload).__name__}({keys})"[:limit]
    return str(payload)[:limit]


def record_step(
    step_num: int,
    tool: str,
    parameters: dict[str, Any],
    success: bool,
    payload: Any,
    error: str | None = None,
) -> StepExecuted:
    """Return a :class:`StepExecuted` entry from a tool call outcome."""
    if success:
        return StepExecuted(
            step=step_num,
            tool=tool,
            parameters=parameters,
            result_summary=summarise(payload),
            success=True,
        )
    return StepExecuted(
        step=step_num,
        tool=tool,
        parameters=parameters,
        result_summary=f"error: {error}" if error else "error",
        success=False,
    )
