"""Task 1 pipeline: LLM-driven signal triage + deterministic safety rules.

Flow:

1. Load the versioned system prompt from ``prompts/triage_system.md``.
2. Format the signal into a compact user message.
3. Call the LLM in JSON mode. Reasoning effort is ``minimal`` because
   this is a classification, not a reasoning task, and gpt-5-* models
   otherwise burn completion budget on internal thought.
4. Parse the JSON. On failure → schema-safe default with an ``errors[]``
   entry.
5. Apply the deterministic safety layer (hull/atmosphere/zone → P1 +
   escalate) so the LLM cannot down-rank a "quiet emergency".
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel
from pydantic import ValidationError

from arufa.shared.config import Settings
from arufa.shared.llm import LLMClient
from arufa.shared.llm import LLMUnavailable
from arufa.shared.models import ErrorEntry
from arufa.shared.models.triage import Category
from arufa.shared.models.triage import MissingInfo
from arufa.shared.models.triage import Priority
from arufa.shared.models.triage import Team
from arufa.shared.models.triage import TriageRequest
from arufa.shared.models.triage import TriageResponse
from arufa.shared.observability import get_logger
from arufa.shared.prompts import load as load_prompt
from arufa.triage import safety_rules

logger = get_logger(__name__)


class _TriageLLMOutput(BaseModel):
    """LLM output schema (everything except ``ticket_id`` which we echo)."""

    category: Category
    priority: Priority
    assigned_team: Team
    needs_escalation: bool
    missing_information: list[MissingInfo]
    next_best_action: str
    remediation_steps: list[str]


def _format_ticket(req: TriageRequest) -> str:
    """Render the signal as a compact user message for the LLM."""
    lines = [
        f"Ticket ID: {req.ticket_id}",
        f"Subject: {req.subject}",
        f"Description: {req.description}",
        f"Reporter: {req.reporter.name} <{req.reporter.email}> ({req.reporter.department})",
        f"Channel: {req.channel}",
        f"Created: {req.created_at}",
    ]
    if req.attachments:
        lines.append(f"Attachments: {len(req.attachments)} item(s)")
    return "\n".join(lines)


def _default_response(ticket_id: str, errors: list[ErrorEntry]) -> TriageResponse:
    """Safe-default envelope for engine-failure paths (200 + errors[])."""
    return TriageResponse(
        ticket_id=ticket_id,
        category="Not a Mission Signal",
        priority="P4",
        assigned_team="None",
        needs_escalation=False,
        missing_information=[],
        next_best_action="",
        remediation_steps=[],
        errors=errors,
    )


def _extract_json(content: str) -> dict[str, Any]:
    """Coerce ``content`` into a dict.

    Handles the two common LLM output patterns despite instructions:
    bare JSON, and JSON wrapped in ```json fences. Falls back to the
    largest ``{...}`` substring on any parse failure.
    """
    text = content.strip()
    # Strip ```json ... ``` fences if present.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Last resort: find outermost braces.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


async def run(
    request: TriageRequest,
    llm: LLMClient,
    settings: Settings,
) -> TriageResponse:
    """Classify a mission signal into the T1 response envelope."""
    system_prompt = load_prompt("triage_system")
    user_message = _format_ticket(request)

    try:
        result = await llm.chat(
            deployment=settings.aoai_deployment_nano,
            model_name=settings.aoai_model_name_nano,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=2048,
            reasoning_effort="minimal",
        )
    except LLMUnavailable as exc:
        logger.warning("triage_llm_unavailable", ticket_id=request.ticket_id, detail=exc.detail)
        return _default_response(
            request.ticket_id,
            errors=[ErrorEntry(code="llm_unavailable", detail=exc.detail)],
        )

    try:
        payload = _extract_json(result.content)
        parsed = _TriageLLMOutput.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        preview = result.content[:200].replace("\n", " ")
        logger.warning(
            "triage_parse_error",
            ticket_id=request.ticket_id,
            error_type=type(exc).__name__,
            preview=preview,
        )
        return _default_response(
            request.ticket_id,
            errors=[ErrorEntry(code="llm_parse_error", detail=str(exc)[:400])],
        )

    response = TriageResponse(
        ticket_id=request.ticket_id,
        category=parsed.category,
        priority=parsed.priority,
        assigned_team=parsed.assigned_team,
        needs_escalation=parsed.needs_escalation,
        missing_information=parsed.missing_information,
        next_best_action=parsed.next_best_action,
        remediation_steps=parsed.remediation_steps,
    )
    return safety_rules.apply(request, response)
