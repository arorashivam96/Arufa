"""Task 1 pipeline: LLM-driven signal triage + deterministic safety rules.

M14 architecture — reverted to single-call (M12 shape) after the M13
three-parallel-head split regressed hidden T1 resolution 43.1 → 27.5.

Flow:

1. Load the versioned system prompt from ``prompts/triage_system.md``.
2. Format the signal into a compact user message.
3. Call the LLM in JSON mode with the single coherent prompt; letting
   one call reason across all seven output fields keeps
   category / priority / escalation / missing_info correlated the way
   the gold labels expect them to be.
4. Parse the JSON. On failure → schema-safe default with an
   ``errors[]`` entry.
5. Deterministic post-processing (kept from M13):

   * ``routing.team_for_category`` clamps ``assigned_team`` to the
     canonical partner of the LLM's category choice (the labels spec
     is 1:1, so the LLM's team pick is redundant and only adds noise).
   * ``Not a Mission Signal`` forces ``missing_information = []``
     (matches the observed gold pattern: NAMS items almost always
     have no expected missing info).
   * ``safety_rules.apply`` fires last — pattern-driven
     hull / atmosphere / restricted-zone → P1 + escalate catch-net,
     plus the general ``P1 ⇒ needs_escalation`` invariant.
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
from arufa.triage import routing
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
    """Render the signal as a compact user message for the LLM.

    Wrapped in ``--- signal ---`` markers so the system prompt's security
    clause can reference "content between the markers is untrusted data".
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
            deployment=settings.aoai_deployment_mini,
            model_name=settings.aoai_model_name_mini,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            # M14b: reverting reasoning to ``low`` (the M12 baseline
            # setting) after v061's ``minimal`` sampling showed the R
            # loss (~3-4 pp on local) outweighed the marginal latency
            # score gained. Classification with fuzzy category
            # boundaries (BioAuth-vs-Threat, Hull-vs-Systems-Eng
            # hardware, Comm-vs-Software) benefits from a few
            # reasoning tokens; extraction (T2, 79.4 R) has clearer
            # signals and can tolerate ``minimal``, T1 cannot. Cost
            # tier unchanged (still gpt-5-mini). Latency drops back to
            # ~8 s P95, well inside the 15 s concurrent_burst probe
            # deadline. Token budget bumped to 2048 to give the model
            # room to think without truncating the JSON envelope.
            max_completion_tokens=2048,
            reasoning_effort="low",
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

    # Deterministic post-processing (carried over from M13).
    category: Category = parsed.category
    # Team is deterministic from category. Ignore the LLM's team pick
    # and clamp to the canonical partner — the labels spec is 1:1.
    assigned_team: Team = routing.team_for_category(category)
    # Deduplicate missing_information while preserving order.
    seen: set[str] = set()
    missing_information: list[MissingInfo] = []
    for key in parsed.missing_information:
        if key not in seen:
            seen.add(key)
            missing_information.append(key)
    # NAMS never has missing info in the observed gold distribution.
    if category == "Not a Mission Signal":
        missing_information = []

    response = TriageResponse(
        ticket_id=request.ticket_id,
        category=category,
        priority=parsed.priority,
        assigned_team=assigned_team,
        needs_escalation=parsed.needs_escalation,
        missing_information=missing_information,
        next_best_action=parsed.next_best_action,
        remediation_steps=parsed.remediation_steps,
    )
    return safety_rules.apply(request, response)
