"""Task 3 (Workflow Orchestration) request and response models."""

from __future__ import annotations

from typing import Any
from typing import Literal

from ms.common.models.base import FrozenBaseModel
from pydantic import ConfigDict

from arufa.shared.models import ErrorEntry


class ToolParameter(FrozenBaseModel):
    """A single parameter declared by an available tool."""

    name: str
    type: str
    description: str = ""
    required: bool | None = None


class ToolDefinition(FrozenBaseModel):
    """One entry of ``available_tools`` from the T3 input.

    ``parameters`` is normally a list of :class:`ToolParameter`, but some
    scenarios ship it as a dict; accept both.
    """

    name: str
    description: str = ""
    endpoint: str
    parameters: list[ToolParameter] | dict[str, Any] = []


class OrchestrateRequest(FrozenBaseModel):
    """Workflow scenario: goal, tools, constraints."""

    task_id: str
    goal: str
    available_tools: list[ToolDefinition]
    constraints: list[str] = []
    mock_service_url: str | None = None
    """Injected by the platform / local eval harness at scoring time."""


class StepExecuted(FrozenBaseModel):
    """Trace entry for a single tool call."""

    step: int
    tool: str
    parameters: dict[str, Any] = {}
    result_summary: str = ""
    success: bool = True


class OrchestrateResponse(FrozenBaseModel):
    """Response envelope; scored fields are ``status`` +
    ``steps_executed`` + ``constraints_satisfied`` + task-specific counters.

    Optional counters are ``None`` when the scenario does not use them.
    ``extra="allow"`` covers any scenario-specific fields we may want to
    surface without failing validation.
    """

    task_id: str
    status: Literal["completed", "partial", "failed"] = "completed"
    steps_executed: list[StepExecuted] = []
    constraints_satisfied: list[str] = []

    accounts_processed: int | None = None
    emails_sent: int | None = None
    emails_skipped: int | None = None
    skip_reasons: dict[str, int] | None = None

    errors: list[ErrorEntry] = []

    model_config = ConfigDict(extra="allow")
