"""Task 1 pipeline: split-head signal triage + deterministic safety rules.

M13 architecture — three focused LLM heads fired in parallel:

1. **Classify head** — category + assigned_team + needs_escalation
   (prompt: ``triage_classify``).
2. **Priority head** — priority only, with four per-priority anchor
   examples (prompt: ``triage_priority``).
3. **Missing-info head** — the 16 canonical concepts with per-concept
   "absent when …" criteria and category-shaped priors
   (prompt: ``triage_missing_info``).

Each head has its own dedicated system prompt so the model can spend
its whole attention budget on one narrow task instead of juggling all
seven output fields at once (the M12 single-head architecture
correlated with a 22-pp gap between local N=50 and hidden N=1000).

Post-processing:

* Category → team via ``arufa.triage.routing.team_for_category`` — the
  labels spec is 1:1, so if the classify head picks a valid category
  the team is deterministic. Clamps team to the canonical partner even
  if the classify head returned a valid-but-mismatched pair.
* ``missing_information`` is validated against the ``MissingInfo``
  literal, dropping any invented keys.
* ``safety_rules.apply`` runs last as the P1-escalation catch-net for
  hull / atmosphere / restricted-zone events.

Failure isolation:

* Each head fails independently. A failure in one head yields a safe
  per-dimension default and an ``errors[]`` entry, but the other two
  heads' outputs are preserved.
* A total failure (all three heads down or the whole gather crashes)
  falls back to the full-default envelope.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from pydantic import ValidationError

from arufa.shared.config import Settings
from arufa.shared.llm import LLMClient
from arufa.shared.llm import LLMResult
from arufa.shared.llm import LLMUnavailable
from arufa.shared.models import ErrorEntry
from arufa.shared.models.triage import Category
from arufa.shared.models.triage import MissingInfo
from arufa.shared.models.triage import Priority
from arufa.shared.models.triage import Team
from arufa.shared.models.triage import TriageRequest
from arufa.shared.models.triage import TriageResponse
from arufa.shared.observability import get_logger
from arufa.shared.observability import record_llm_call
from arufa.shared.prompts import load as load_prompt
from arufa.triage import routing
from arufa.triage import safety_rules

logger = get_logger(__name__)


# ---- head output schemas --------------------------------------------


class _ClassifyOutput(BaseModel):
    category: Category
    assigned_team: Team
    needs_escalation: bool


class _PriorityOutput(BaseModel):
    priority: Priority


class _MissingInfoOutput(BaseModel):
    missing_information: list[MissingInfo] = []


# ---- per-head result envelope ---------------------------------------


@dataclass
class _HeadResult:
    """Merged output from one classification head + any error entry.

    ``model_name`` / ``prompt_tokens`` / ``completion_tokens`` are
    captured from the successful ``LLMResult`` so the parent request
    task can re-set :data:`llm_call_var` after ``asyncio.gather``
    (each child task's ContextVar writes stay in its own context copy).
    """

    payload: BaseModel | None
    error: ErrorEntry | None
    model_name: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


# ---- shared helpers -------------------------------------------------


def _format_ticket(req: TriageRequest) -> str:
    """Render the signal as a compact user message for the LLM heads.

    Wrapped in ``--- signal ---`` markers so each head's security clause
    can treat inner content as untrusted data.
    """
    lines = [
        "--- signal ---",
        f"Ticket ID: {req.ticket_id}",
        f"Subject: {req.subject}",
        f"Description: {req.description}",
        f"Reporter: {req.reporter.name} <{req.reporter.email}> ({req.reporter.department})",
        f"Channel: {req.channel}",
        f"Created: {req.created_at}",
    ]
    if req.attachments:
        lines.append(f"Attachments: {len(req.attachments)} item(s)")
    lines.append("--- end signal ---")
    return "\n".join(lines)


def _extract_json(content: str) -> dict[str, Any]:
    """Coerce ``content`` into a dict.

    Handles bare JSON and JSON wrapped in ```json fences; falls back to
    the largest ``{...}`` substring on any parse failure.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


async def _call_head(
    prompt_name: str,
    user_message: str,
    llm: LLMClient,
    settings: Settings,
) -> LLMResult:
    """Fire one classification head against the mini deployment."""
    system_prompt = load_prompt(prompt_name)
    return await llm.chat(
        deployment=settings.aoai_deployment_mini,
        model_name=settings.aoai_model_name_mini,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        response_format={"type": "json_object"},
        max_completion_tokens=2048,
        reasoning_effort="low",
    )


async def _run_classify_head(
    request: TriageRequest,
    user_message: str,
    llm: LLMClient,
    settings: Settings,
) -> _HeadResult:
    try:
        result = await _call_head("triage_classify", user_message, llm, settings)
    except LLMUnavailable as exc:
        logger.warning(
            "triage_classify_unavailable",
            ticket_id=request.ticket_id,
            detail=exc.detail,
        )
        return _HeadResult(
            payload=None,
            error=ErrorEntry(code="classify_unavailable", detail=exc.detail),
        )
    try:
        payload = _extract_json(result.content)
        parsed = _ClassifyOutput.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        preview = result.content[:200].replace("\n", " ")
        logger.warning(
            "triage_classify_parse_error",
            ticket_id=request.ticket_id,
            error_type=type(exc).__name__,
            preview=preview,
        )
        return _HeadResult(
            payload=None,
            error=ErrorEntry(code="classify_parse_error", detail=str(exc)[:400]),
            model_name=result.model_name,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
    return _HeadResult(
        payload=parsed,
        error=None,
        model_name=result.model_name,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )


async def _run_priority_head(
    request: TriageRequest,
    user_message: str,
    llm: LLMClient,
    settings: Settings,
) -> _HeadResult:
    try:
        result = await _call_head("triage_priority", user_message, llm, settings)
    except LLMUnavailable as exc:
        logger.warning(
            "triage_priority_unavailable",
            ticket_id=request.ticket_id,
            detail=exc.detail,
        )
        return _HeadResult(
            payload=None,
            error=ErrorEntry(code="priority_unavailable", detail=exc.detail),
        )
    try:
        payload = _extract_json(result.content)
        parsed = _PriorityOutput.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        preview = result.content[:200].replace("\n", " ")
        logger.warning(
            "triage_priority_parse_error",
            ticket_id=request.ticket_id,
            error_type=type(exc).__name__,
            preview=preview,
        )
        return _HeadResult(
            payload=None,
            error=ErrorEntry(code="priority_parse_error", detail=str(exc)[:400]),
            model_name=result.model_name,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
    return _HeadResult(
        payload=parsed,
        error=None,
        model_name=result.model_name,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )


async def _run_missing_info_head(
    request: TriageRequest,
    user_message: str,
    llm: LLMClient,
    settings: Settings,
) -> _HeadResult:
    try:
        result = await _call_head("triage_missing_info", user_message, llm, settings)
    except LLMUnavailable as exc:
        logger.warning(
            "triage_missing_info_unavailable",
            ticket_id=request.ticket_id,
            detail=exc.detail,
        )
        return _HeadResult(
            payload=None,
            error=ErrorEntry(code="missing_info_unavailable", detail=exc.detail),
        )
    try:
        payload = _extract_json(result.content)
        parsed = _MissingInfoOutput.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        preview = result.content[:200].replace("\n", " ")
        logger.warning(
            "triage_missing_info_parse_error",
            ticket_id=request.ticket_id,
            error_type=type(exc).__name__,
            preview=preview,
        )
        return _HeadResult(
            payload=None,
            error=ErrorEntry(code="missing_info_parse_error", detail=str(exc)[:400]),
            model_name=result.model_name,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
    return _HeadResult(
        payload=parsed,
        error=None,
        model_name=result.model_name,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )


async def run(
    request: TriageRequest,
    llm: LLMClient,
    settings: Settings,
) -> TriageResponse:
    """Classify a mission signal via three parallel LLM heads."""
    user_message = _format_ticket(request)

    classify_result, priority_result, missing_info_result = await asyncio.gather(
        _run_classify_head(request, user_message, llm, settings),
        _run_priority_head(request, user_message, llm, settings),
        _run_missing_info_head(request, user_message, llm, settings),
    )

    # Re-set the request-scoped LLM ContextVar with aggregate metadata.
    # Each child task in ``asyncio.gather`` gets its own context copy, so
    # writes made by :func:`record_llm_call` inside a head don't
    # propagate back to the middleware that reads the parent context.
    # Aggregate the three heads' tokens here and set once so
    # ``X-Model-Name`` and ``X-Token-Count`` reflect the whole request.
    total_prompt_tokens = (
        classify_result.prompt_tokens
        + priority_result.prompt_tokens
        + missing_info_result.prompt_tokens
    )
    total_completion_tokens = (
        classify_result.completion_tokens
        + priority_result.completion_tokens
        + missing_info_result.completion_tokens
    )
    model_name = (
        classify_result.model_name
        or priority_result.model_name
        or missing_info_result.model_name
    )
    if model_name:
        record_llm_call(
            model_name=model_name,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
        )

    errors: list[ErrorEntry] = []

    # ---- classify head ----------------------------------------------
    if classify_result.payload is not None:
        classify: _ClassifyOutput = classify_result.payload  # type: ignore[assignment]
        category: Category = classify.category
        needs_escalation: bool = classify.needs_escalation
    else:
        category = "Not a Mission Signal"
        needs_escalation = False
        if classify_result.error is not None:
            errors.append(classify_result.error)

    # Team is deterministic from category. The labels spec is 1:1;
    # ignore whatever the LLM emitted for `assigned_team` and clamp to
    # the canonical partner.
    assigned_team: Team = routing.team_for_category(category)

    # ---- priority head ----------------------------------------------
    if priority_result.payload is not None:
        priority_payload: _PriorityOutput = priority_result.payload  # type: ignore[assignment]
        priority: Priority = priority_payload.priority
    else:
        priority = "P4"
        if priority_result.error is not None:
            errors.append(priority_result.error)

    # ---- missing-info head ------------------------------------------
    if missing_info_result.payload is not None:
        mi_payload: _MissingInfoOutput = missing_info_result.payload  # type: ignore[assignment]
        # Deduplicate while preserving order.
        seen: set[str] = set()
        missing_information: list[MissingInfo] = []
        for key in mi_payload.missing_information:
            if key not in seen:
                seen.add(key)
                missing_information.append(key)
    else:
        missing_information = []
        if missing_info_result.error is not None:
            errors.append(missing_info_result.error)

    # Cross-head rule: ``Not a Mission Signal`` never has missing info.
    # The parallel heads can't see each other's output; enforce here so
    # the missing-info head can't inflate the set-F1 on spam/off-topic
    # items (where gold is almost always ``[]``).
    if category == "Not a Mission Signal":
        missing_information = []

    response = TriageResponse(
        ticket_id=request.ticket_id,
        category=category,
        priority=priority,
        assigned_team=assigned_team,
        needs_escalation=needs_escalation,
        missing_information=missing_information,
        next_best_action="",
        remediation_steps=[],
        errors=errors,
    )
    return safety_rules.apply(request, response)
