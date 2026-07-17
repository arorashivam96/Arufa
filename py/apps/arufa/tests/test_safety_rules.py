"""Tests for :mod:`arufa.triage.safety_rules`.

Kapoor's always-escalate list is the *safety net* for cases where the
LLM might down-rank a genuine emergency. We test:

* Each trigger fires on realistic phrasing (positive cases)
* Non-emergency signals are not incorrectly promoted (negative cases)
* Category and team are preserved (safety only touches priority + escalation)
"""

from __future__ import annotations

import pytest

from arufa.shared.models.triage import Reporter
from arufa.shared.models.triage import TriageRequest
from arufa.shared.models.triage import TriageResponse
from arufa.triage import safety_rules


def _make_request(subject: str, description: str = "") -> TriageRequest:
    return TriageRequest(
        ticket_id="T-1",
        subject=subject,
        description=description,
        reporter=Reporter(name="Test", email="t@example.com", department="Ops"),
        created_at="2026-01-01T00:00:00Z",
        channel="bridge_terminal",
        attachments=[],
    )


def _p3_response() -> TriageResponse:
    """A P3 non-escalated response we can pass through safety_rules to
    check whether it gets forced to P1 + escalate."""
    return TriageResponse(
        ticket_id="T-1",
        category="Hull & Structural Systems",
        priority="P3",
        assigned_team="Spacecraft Systems Engineering",
        needs_escalation=False,
        missing_information=[],
        next_best_action="",
        remediation_steps=[],
    )


# ---- positive cases: triggers must fire ------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "Hull breach on deck 7",
        "hull rupture detected",
        "hull compromise, immediate action needed",
        "structural breach in cargo bay",
        "depressurizing rapidly",
        "cabin depressurisation event",
        "vacuum breach reported",
        "hull integrity compromised",
    ],
)
def test_hull_breach_forces_p1_escalate(phrase: str) -> None:
    req = _make_request(subject=phrase)
    out = safety_rules.apply(req, _p3_response())
    assert out.priority == "P1"
    assert out.needs_escalation is True


@pytest.mark.parametrize(
    "phrase",
    [
        "Life support failure on level 4",
        "life-support offline",
        "life support down",
        "atmosphere compromised in section B",
        "atmosphere contaminated",
        "atmosphere failing rapidly",
        "oxygen depleting fast",
        "oxygen critical in bay 3",
        "toxic atmosphere reported",
        "breathable air lost",
    ],
)
def test_atmosphere_compromise_forces_p1_escalate(phrase: str) -> None:
    req = _make_request(subject="Alert", description=phrase)
    out = safety_rules.apply(req, _p3_response())
    assert out.priority == "P1"
    assert out.needs_escalation is True


@pytest.mark.parametrize(
    "phrase",
    [
        "Restricted-zone access from unknown crew",
        "restricted area breach detected",
        "restricted area access anomaly",
        "classified zone breach reported",
        "unauthorized entry into restricted section",
    ],
)
def test_restricted_zone_forces_p1_escalate(phrase: str) -> None:
    req = _make_request(subject=phrase)
    out = safety_rules.apply(req, _p3_response())
    assert out.priority == "P1"
    assert out.needs_escalation is True


# ---- negative cases: non-emergencies must NOT be promoted -----------


@pytest.mark.parametrize(
    "subject,description",
    [
        ("Coffee machine broken", "The coffee machine in break room 2 is broken."),
        ("Password reset request", "Cannot log into workstation, need password reset."),
        ("Backup completed", "Nightly backup completed successfully, no action needed."),
        ("VPN latency", "SubComm relay showing intermittent latency to sector 4."),
        ("Meeting reminder", "Reminder: mission briefing at 1400."),
    ],
)
def test_non_emergency_signals_not_promoted(subject: str, description: str) -> None:
    req = _make_request(subject=subject, description=description)
    resp = _p3_response()
    out = safety_rules.apply(req, resp)
    assert out.priority == resp.priority  # unchanged
    assert out.needs_escalation == resp.needs_escalation


def test_safety_preserves_category_and_team() -> None:
    req = _make_request(subject="Hull breach on deck 7")
    resp_with_wrong_cat = TriageResponse(
        ticket_id="T-1",
        category="Communications & Navigation",  # wrong on purpose
        priority="P3",
        assigned_team="Deep Space Communications",  # wrong on purpose
        needs_escalation=False,
        missing_information=[],
        next_best_action="",
        remediation_steps=[],
    )
    out = safety_rules.apply(req, resp_with_wrong_cat)
    # Only priority + escalation change; category and team are LLM-owned.
    assert out.category == "Communications & Navigation"
    assert out.assigned_team == "Deep Space Communications"
    assert out.priority == "P1"
    assert out.needs_escalation is True


def test_triggered_reasons_lists_matched_rules() -> None:
    req = _make_request(
        subject="Hull breach + oxygen depleting",
        description="Restricted-zone access observed on deck 4.",
    )
    reasons = safety_rules.triggered_reasons(req)
    assert set(reasons) == {"hull_breach", "atmosphere_compromise", "restricted_zone_access"}


def test_case_insensitive_matching() -> None:
    req = _make_request(subject="HULL BREACH")
    out = safety_rules.apply(req, _p3_response())
    assert out.priority == "P1"
