"""Tests for :mod:`arufa.triage.pipeline` (M13 split-head architecture).

Mocks the LLM client to avoid network. Each call to :meth:`_StubLLM.chat`
is routed to a fake response keyed off the system prompt filename so
each of the three heads (classify / priority / missing_info) can be
tested in isolation. Verifies:

* Happy path: three heads return valid JSON → pipeline merges into
  ``TriageResponse``.
* JSON-fenced output is unwrapped (defensive parsing on each head).
* Deterministic category → team override runs.
* Malformed JSON from any single head → per-head default + one
  ``errors[]`` entry, other heads' outputs are preserved.
* LLM validation error (bad enum) → per-head default + ``errors[]``.
* :class:`LLMUnavailable` from all three heads → full default envelope
  with three ``errors[]`` entries.
* Safety layer fires end-to-end (hull breach → forced P1).
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
    """Route each ``chat`` call to a per-head response.

    ``by_head`` maps head-name (``"classify" | "priority" | "missing_info"``)
    to either an :class:`LLMResult` or an :class:`Exception` (raised on
    the matching call). We detect the head from a marker string in the
    system prompt so the tests don't have to depend on the exact prompt
    text.
    """

    by_head: dict[str, Any]

    async def chat(self, **kwargs: Any) -> LLMResult:
        messages = kwargs.get("messages", [])
        system_prompt = messages[0]["content"] if messages else ""
        head = _detect_head(system_prompt)
        response = self.by_head.get(head)
        if response is None:
            raise AssertionError(f"unexpected head call: {head}")
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


def _detect_head(system_prompt: str) -> str:
    """Recover the head name from a distinguishing phrase in each prompt."""
    if "classification head" in system_prompt.lower():
        return "classify"
    if "priority head" in system_prompt.lower():
        return "priority"
    if "missing-information head" in system_prompt.lower():
        return "missing_info"
    return "unknown"


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


def _classify_json(**overrides: Any) -> dict[str, Any]:
    base = {
        "category": "Communications & Navigation",
        "assigned_team": "Deep Space Communications",
        "needs_escalation": False,
    }
    base.update(overrides)
    return base


def _priority_json(priority: str = "P3") -> dict[str, Any]:
    return {"priority": priority}


def _missing_info_json(*keys: str) -> dict[str, Any]:
    return {"missing_information": list(keys)}


def _result(content: str) -> LLMResult:
    return LLMResult(
        content=content,
        model_name="gpt-5-mini",
        prompt_tokens=100,
        completion_tokens=50,
        raw={},
    )


def _all_happy(
    classify: dict[str, Any] | None = None,
    priority: str = "P3",
    missing: tuple[str, ...] = ("sequence_to_reproduce", "recurrence_pattern"),
) -> dict[str, Any]:
    """Convenience: build a full three-head happy-path stub map."""
    return {
        "classify": _result(json.dumps(classify or _classify_json())),
        "priority": _result(json.dumps(_priority_json(priority))),
        "missing_info": _result(json.dumps(_missing_info_json(*missing))),
    }


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


async def test_happy_path_merges_three_heads(settings: Settings) -> None:
    llm = _StubLLM(by_head=_all_happy())
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.ticket_id == "T-42"
    assert out.category == "Communications & Navigation"
    assert out.priority == "P3"
    assert out.assigned_team == "Deep Space Communications"
    assert out.needs_escalation is False
    assert "sequence_to_reproduce" in out.missing_information
    assert out.errors == []


async def test_code_fenced_json_is_unwrapped_on_each_head(settings: Settings) -> None:
    fenced_classify = f"```json\n{json.dumps(_classify_json())}\n```"
    fenced_priority = f"```json\n{json.dumps(_priority_json('P2'))}\n```"
    fenced_missing = f"```json\n{json.dumps(_missing_info_json('anomaly_readout'))}\n```"
    llm = _StubLLM(
        by_head={
            "classify": _result(fenced_classify),
            "priority": _result(fenced_priority),
            "missing_info": _result(fenced_missing),
        }
    )
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.category == "Communications & Navigation"
    assert out.priority == "P2"
    assert out.missing_information == ["anomaly_readout"]
    assert out.errors == []


async def test_json_embedded_in_prose_is_extracted(settings: Settings) -> None:
    prose = "Here is the classification:\n\n" + json.dumps(_classify_json()) + "\n\nEnd."
    happy = _all_happy()
    happy["classify"] = _result(prose)
    llm = _StubLLM(by_head=happy)
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.category == "Communications & Navigation"
    assert out.errors == []


# ---- deterministic category → team override -------------------------


async def test_category_team_override_clamps_mismatched_team(settings: Settings) -> None:
    """LLM emits a valid team but the wrong one → override to canonical."""
    happy = _all_happy(
        classify=_classify_json(
            category="Hull & Structural Systems",
            assigned_team="Deep Space Communications",  # wrong on purpose
        )
    )
    llm = _StubLLM(by_head=happy)
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.category == "Hull & Structural Systems"
    assert out.assigned_team == "Spacecraft Systems Engineering"


async def test_not_a_mission_signal_forces_empty_missing_info(settings: Settings) -> None:
    """Category=NAMS + missing_info head over-emits → cross-head rule clears it."""
    happy = _all_happy(
        classify=_classify_json(
            category="Not a Mission Signal",
            assigned_team="None",
            needs_escalation=False,
        ),
        missing=("anomaly_readout", "affected_subsystem"),
    )
    llm = _StubLLM(by_head=happy)
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.category == "Not a Mission Signal"
    assert out.missing_information == []


# ---- safety layer end-to-end ----------------------------------------


async def test_safety_layer_forces_p1_on_hull_breach(settings: Settings) -> None:
    """Priority head emits P3 but the request describes a hull breach →
    safety catch-net promotes to P1 + escalation."""
    happy = _all_happy(priority="P3")
    llm = _StubLLM(by_head=happy)
    request = _req(subject="Hull breach on deck 7", description="Micro-fracture detected.")
    out = await pipeline.run(request, llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.priority == "P1"
    assert out.needs_escalation is True


# ---- failure paths (per-head isolation) -----------------------------


async def test_malformed_json_on_classify_head_uses_safe_default(settings: Settings) -> None:
    happy = _all_happy()
    happy["classify"] = _result("this is not json at all")
    llm = _StubLLM(by_head=happy)
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    # classify head defaulted; priority survived; missing_info is cleared
    # by the NAMS cross-head rule (default category is NAMS).
    assert out.category == "Not a Mission Signal"
    assert out.assigned_team == "None"
    assert out.priority == "P3"
    assert out.missing_information == []
    assert out.errors and out.errors[0].code == "classify_parse_error"


async def test_invalid_enum_on_priority_head_uses_safe_default(settings: Settings) -> None:
    happy = _all_happy()
    happy["priority"] = _result(json.dumps({"priority": "P9"}))  # invalid enum
    llm = _StubLLM(by_head=happy)
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.priority == "P4"  # safe default
    assert out.category == "Communications & Navigation"  # classify survived
    assert out.errors and out.errors[0].code == "priority_parse_error"


async def test_invalid_key_on_missing_info_head_uses_safe_default(settings: Settings) -> None:
    happy = _all_happy()
    happy["missing_info"] = _result(json.dumps({"missing_information": ["not_a_real_key"]}))
    llm = _StubLLM(by_head=happy)
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.missing_information == []
    assert out.category == "Communications & Navigation"  # classify survived
    assert out.errors and out.errors[0].code == "missing_info_parse_error"


async def test_all_heads_unavailable_returns_full_default(settings: Settings) -> None:
    down = LLMUnavailable("aoai down", attempts=3)
    llm = _StubLLM(
        by_head={
            "classify": down,
            "priority": down,
            "missing_info": down,
        }
    )
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.category == "Not a Mission Signal"
    assert out.priority == "P4"
    assert out.assigned_team == "None"
    assert out.missing_information == []
    codes = {e.code for e in out.errors}
    assert codes == {"classify_unavailable", "priority_unavailable", "missing_info_unavailable"}
