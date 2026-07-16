"""Task 1 pipeline entry point.

M2 stub: returns a schema-valid envelope with safe defaults. The real
LLM-driven pipeline lands in M4 (see ``PLAN.md``).
"""

from __future__ import annotations

from arufa.shared.models.triage import TriageRequest
from arufa.shared.models.triage import TriageResponse


async def run(request: TriageRequest) -> TriageResponse:
    """Return a stub triage decision.

    Defaults are chosen to never *hallucinate* an owning team on an
    unclassified signal: ``Not a Mission Signal`` + ``None`` team +
    ``P4`` gives the scorer a coherent (if low-scoring) answer while we
    build the real classifier.
    """
    return TriageResponse(
        ticket_id=request.ticket_id,
        category="Not a Mission Signal",
        priority="P4",
        assigned_team="None",
        needs_escalation=False,
        missing_information=[],
        next_best_action="",
        remediation_steps=[],
    )
