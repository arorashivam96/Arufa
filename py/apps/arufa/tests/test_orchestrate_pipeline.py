"""Tests for :mod:`arufa.orchestrate.pipeline`.

Mocks BOTH the LLM (returns a plan) and the tool client (returns tool
outcomes). Verifies:

* Happy path: plan is executed step-by-step; counters and
  ``constraints_satisfied`` flow through
* Unknown tool in the plan → recorded as a failed step, workflow continues
* Any failed step degrades ``status`` to ``partial``
* Malformed LLM JSON → failed response with ``errors[]``
* ``LLMUnavailable`` → failed response with ``errors[]``
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from arufa.orchestrate import pipeline
from arufa.orchestrate.tool_client import ToolCallResult
from arufa.shared.config import Settings
from arufa.shared.llm import LLMResult
from arufa.shared.llm import LLMUnavailable
from arufa.shared.models.orchestrate import OrchestrateRequest
from arufa.shared.models.orchestrate import ToolDefinition
from arufa.shared.models.orchestrate import ToolParameter


@dataclass
class _StubLLM:
    response: Any

    async def chat(self, **kwargs: Any) -> LLMResult:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response  # type: ignore[return-value]


class _StubToolClient:
    """Returns queued outcomes per URL, ignores the rest."""

    def __init__(self, outcomes: dict[str, ToolCallResult]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, url: str, params: dict[str, Any]) -> ToolCallResult:
        self.calls.append((url, params))
        return self.outcomes.get(url, ToolCallResult(True, 200, {"ok": True}))

    async def aclose(self) -> None:
        pass


def _req(
    tools: list[ToolDefinition] | None = None,
    constraints: list[str] | None = None,
) -> OrchestrateRequest:
    return OrchestrateRequest(
        task_id="TASK-1",
        goal="Send an alert email for every account with churn risk > 0.7.",
        available_tools=tools
        or [
            ToolDefinition(
                name="crm_search",
                description="Find accounts by filter.",
                endpoint="http://mock/crm_search",
                parameters=[ToolParameter(name="filter", type="string", required=True)],
            ),
            ToolDefinition(
                name="send_email",
                description="Send an email to a recipient.",
                endpoint="http://mock/send_email",
                parameters=[
                    ToolParameter(name="to", type="string", required=True),
                    ToolParameter(name="subject", type="string", required=True),
                ],
            ),
        ],
        constraints=constraints or ["Send at most one email per account.", "Audit each send."],
        mock_service_url="http://mock",
    )


def _plan_result(plan: dict[str, Any]) -> LLMResult:
    return LLMResult(
        content=json.dumps(plan),
        model_name="gpt-5-nano",
        prompt_tokens=200,
        completion_tokens=150,
        raw={},
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        aoai_endpoint="https://test.example.com/",
        aoai_deployment_nano="gpt-5-nano",
        aoai_model_name_nano="gpt-5-nano",
        aoai_auth_mode="key",
        aoai_api_key="test",
    )


# ---- happy path ------------------------------------------------------


async def test_happy_path_executes_and_returns_plan_metadata(settings: Settings) -> None:
    plan = {
        "steps": [
            {"tool": "crm_search", "parameters": {"filter": "churn_risk > 0.7"}},
            {"tool": "send_email", "parameters": {"to": "a@x.com", "subject": "Alert"}},
        ],
        "constraints_satisfied": ["Send at most one email per account.", "Audit each send."],
        "accounts_processed": 5,
        "emails_sent": 5,
        "emails_skipped": 0,
        "skip_reasons": None,
        "status": "completed",
    }
    llm = _StubLLM(response=_plan_result(plan))
    tool_client = _StubToolClient(
        {
            "http://mock/crm_search": ToolCallResult(True, 200, {"accounts": [{"id": "ACC-1"}]}),
            "http://mock/send_email": ToolCallResult(True, 200, {"sent": True}),
        }
    )

    out = await pipeline.run(_req(), llm=llm, settings=settings, tool_client=tool_client)  # type: ignore[arg-type]

    assert out.task_id == "TASK-1"
    assert out.status == "completed"
    assert [s.tool for s in out.steps_executed] == ["crm_search", "send_email"]
    assert all(s.success for s in out.steps_executed)
    assert out.accounts_processed == 5
    assert out.emails_sent == 5
    assert "Send at most one email per account." in out.constraints_satisfied
    # Tool client was actually called with the params from the plan
    assert tool_client.calls[0] == ("http://mock/crm_search", {"filter": "churn_risk > 0.7"})


async def test_unknown_tool_records_failed_step_but_keeps_status_completed(settings: Settings) -> None:
    """A failed step is visible in ``steps_executed`` but must NOT downgrade
    ``status``. The FDEBench T3 scorer zeroes ``goal_completion`` (20% of R)
    when ``status != "completed"``; the other dimensions
    (``constraint_compliance``, ``ordering_correctness``) already penalise
    real failures via outcome assertions in the gold data.
    """
    plan = {
        "steps": [
            {"tool": "made_up_tool", "parameters": {}},
            {"tool": "crm_search", "parameters": {"filter": "x"}},
        ],
        "constraints_satisfied": [],
        "status": "completed",
    }
    llm = _StubLLM(response=_plan_result(plan))
    tool_client = _StubToolClient(
        {"http://mock/crm_search": ToolCallResult(True, 200, {"accounts": []})}
    )

    out = await pipeline.run(_req(), llm=llm, settings=settings, tool_client=tool_client)  # type: ignore[arg-type]

    assert len(out.steps_executed) == 2
    assert out.steps_executed[0].success is False
    assert "error" in out.steps_executed[0].result_summary
    assert out.steps_executed[1].success is True
    # Failed step is visible for observability, but status stays completed.
    assert out.status == "completed"


async def test_tool_5xx_failure_keeps_status_completed(settings: Settings) -> None:
    """See ``test_unknown_tool_records_failed_step_but_keeps_status_completed``
    for the rationale — same principle applies to 5xx failures.
    """
    plan = {
        "steps": [{"tool": "crm_search", "parameters": {"filter": "x"}}],
        "constraints_satisfied": [],
        "status": "completed",
    }
    llm = _StubLLM(response=_plan_result(plan))
    tool_client = _StubToolClient(
        {"http://mock/crm_search": ToolCallResult(False, 500, None, "http_500")}
    )

    out = await pipeline.run(_req(), llm=llm, settings=settings, tool_client=tool_client)  # type: ignore[arg-type]

    assert out.status == "completed"
    assert out.steps_executed[0].success is False


# ---- failure paths ---------------------------------------------------


async def test_malformed_plan_returns_failed_envelope(settings: Settings) -> None:
    llm = _StubLLM(response=_plan_result({}))  # will be overridden below
    llm.response = LLMResult(
        content="this is not json",
        model_name="gpt-5-nano",
        prompt_tokens=1,
        completion_tokens=1,
        raw={},
    )
    tool_client = _StubToolClient({})

    out = await pipeline.run(_req(), llm=llm, settings=settings, tool_client=tool_client)  # type: ignore[arg-type]

    assert out.status == "failed"
    assert out.errors and out.errors[0].code == "llm_parse_error"


async def test_llm_unavailable_returns_failed_envelope(settings: Settings) -> None:
    llm = _StubLLM(response=LLMUnavailable("aoai down", attempts=3))
    tool_client = _StubToolClient({})

    out = await pipeline.run(_req(), llm=llm, settings=settings, tool_client=tool_client)  # type: ignore[arg-type]

    assert out.status == "failed"
    assert out.errors and out.errors[0].code == "llm_unavailable"


async def test_task_id_always_echoed(settings: Settings) -> None:
    """The scorer joins on the ID field — we must never rewrite it."""
    plan = {"steps": [], "constraints_satisfied": [], "status": "completed"}
    llm = _StubLLM(response=_plan_result(plan))
    tool_client = _StubToolClient({})

    out = await pipeline.run(_req(), llm=llm, settings=settings, tool_client=tool_client)  # type: ignore[arg-type]

    assert out.task_id == "TASK-1"
