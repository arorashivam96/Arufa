"""Tests for :mod:`arufa.triage.pipeline`.

Mocks the LLM client to avoid network. Verifies:

* Happy path: LLM returns valid JSON → pipeline builds ``TriageResponse``
* JSON-fenced output is unwrapped (defensive parsing)
* Malformed JSON → default envelope + ``errors[]`` (200 semantics)
* LLM validation error (bad enum value) → default envelope + ``errors[]``
* ``LLMUnavailable`` from client → default envelope + ``errors[]``
* Safety layer fires end-to-end (hull breach in request → forced P1)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from arufa.shared.config import Settings
from arufa.shared.llm import LLMResult
from arufa.shared.llm import LLMUnavailable
from arufa.shared.models.triage import Reporter
from arufa.shared.models.triage import TriageRequest
from arufa.triage import pipeline


@dataclass
class _StubLLM:
    """Minimal stand-in for :class:`LLMClient`.

    ``response`` may be an :class:`LLMResult` or an :class:`Exception`
    (raised on call).
    """

    response: Any

    async def chat(self, **kwargs: Any) -> LLMResult:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response  # type: ignore[return-value]


def _req(subject: str = "Nav console flicker", description: str = "Console flickers every 20 min.") -> TriageRequest:
    return TriageRequest(
        ticket_id="T-42",
        subject=subject,
        description=description,
        reporter=Reporter(name="Lt. Chen", email="chen@example.com", department="Comms"),
        created_at="2026-01-01T00:00:00Z",
        channel="bridge_terminal",
        attachments=[],
    )


def _valid_llm_json() -> dict[str, Any]:
    return {
        "category": "Communications & Navigation",
        "priority": "P3",
        "assigned_team": "Deep Space Communications",
        "needs_escalation": False,
        "missing_information": ["sequence_to_reproduce", "recurrence_pattern"],
        "next_best_action": "Poll relay and confirm channel stability.",
        "remediation_steps": [
            "Query subspace relay for recent link errors.",
            "Reboot nav console to clear buffer state.",
        ],
    }


def _result(content: str) -> LLMResult:
    return LLMResult(
        content=content,
        model_name="gpt-5-nano",
        prompt_tokens=100,
        completion_tokens=50,
        raw={},
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        aoai_endpoint="https://test.example.com/",
        aoai_deployment_nano="gpt-5-nano",
        aoai_deployment_mini="gpt-5-mini",
        aoai_model_name_nano="gpt-5-nano",
        aoai_model_name_mini="gpt-5-mini",
        aoai_auth_mode="key",
        aoai_api_key="test",
    )


# ---- happy path ------------------------------------------------------


async def test_happy_path_returns_llm_classification(settings: Settings) -> None:
    llm = _StubLLM(response=_result(json.dumps(_valid_llm_json())))
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.ticket_id == "T-42"
    assert out.category == "Communications & Navigation"
    assert out.priority == "P3"
    assert out.assigned_team == "Deep Space Communications"
    assert "sequence_to_reproduce" in out.missing_information
    assert out.errors == []


async def test_code_fenced_json_is_unwrapped(settings: Settings) -> None:
    fenced = f"```json\n{json.dumps(_valid_llm_json())}\n```"
    llm = _StubLLM(response=_result(fenced))
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.category == "Communications & Navigation"
    assert out.errors == []


async def test_json_embedded_in_prose_is_extracted(settings: Settings) -> None:
    prose = "Here is the classification:\n\n" + json.dumps(_valid_llm_json()) + "\n\nEnd."
    llm = _StubLLM(response=_result(prose))
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.category == "Communications & Navigation"
    assert out.errors == []


# ---- safety layer end-to-end ----------------------------------------


async def test_safety_layer_forces_p1_on_hull_breach(settings: Settings) -> None:
    """LLM emits P3 but the request says hull breach → safety promotes to P1."""
    llm_json = _valid_llm_json() | {"priority": "P3", "needs_escalation": False}
    llm = _StubLLM(response=_result(json.dumps(llm_json)))
    request = _req(subject="Hull breach on deck 7", description="Micro-fracture detected.")
    out = await pipeline.run(request, llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.priority == "P1"
    assert out.needs_escalation is True


# ---- failure paths ---------------------------------------------------


async def test_malformed_json_returns_default_with_errors(settings: Settings) -> None:
    llm = _StubLLM(response=_result("this is not json at all"))
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.category == "Not a Mission Signal"
    assert out.priority == "P4"
    assert out.assigned_team == "None"
    assert out.errors and out.errors[0].code == "llm_parse_error"


async def test_invalid_enum_value_returns_default_with_errors(settings: Settings) -> None:
    bad = _valid_llm_json() | {"category": "Made Up Category"}
    llm = _StubLLM(response=_result(json.dumps(bad)))
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.errors and out.errors[0].code == "llm_parse_error"


async def test_llm_unavailable_returns_default_with_errors(settings: Settings) -> None:
    llm = _StubLLM(response=LLMUnavailable("aoai down", attempts=3))
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.category == "Not a Mission Signal"
    assert out.errors and out.errors[0].code == "llm_unavailable"
