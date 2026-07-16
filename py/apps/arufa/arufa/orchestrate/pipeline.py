"""Task 3 pipeline entry point.

M2 stub: returns a ``completed`` status with an empty step trace. The
real planner + tool executor lands in M6.
"""

from __future__ import annotations

from arufa.shared.models.orchestrate import OrchestrateRequest
from arufa.shared.models.orchestrate import OrchestrateResponse


async def run(request: OrchestrateRequest) -> OrchestrateResponse:
    """Return a stub orchestration result."""
    return OrchestrateResponse(
        task_id=request.task_id,
        status="completed",
        steps_executed=[],
        constraints_satisfied=[],
    )
