"""Deterministic post-LLM safety rules for Task 1.

Cmdr. Kapoor's non-negotiable overrides (V1):

* Hull breach → always escalate + ``P1``
* Atmosphere / life-support compromise → always escalate + ``P1``
* Restricted-zone access → always escalate + ``P1``

These fire on keyword patterns in ``subject`` + ``description``. The LLM
prompt already teaches these rules, so the safety layer is a *catch-net*
for cases where the model down-ranks a genuine emergency (Kapoor's
"quiet, well-formatted senior officer" pattern). Category and team
remain LLM-decided — forcing them here risks worse F1 without helping
escalation.
"""

from __future__ import annotations

import re

from arufa.shared.models.triage import TriageRequest
from arufa.shared.models.triage import TriageResponse

# Compiled once; word-boundary anchored to reduce false positives.
# Partial-word stems use ``\w*`` so we match e.g. "depressurizing" and
# "compromised" — a trailing ``\b`` would require the stem itself to be
# a full word, which none of these are.
_HULL_BREACH = re.compile(
    r"\bhull\s+(?:breach|rupture|compromise|fracture|crack)"
    r"|\bstructural\s+breach"
    r"|\bdepressuri[sz]\w*"
    r"|\bvacuum\s+breach"
    r"|\bhull\s+integrity\s+(?:lost|failing|compromised)",
    re.IGNORECASE,
)

_ATMOSPHERE_COMPROMISE = re.compile(
    r"\batmosphere\s+(?:compromis\w*|failure|failing|breach|contamin\w*)"
    r"|\blife[\s-]?support\s+(?:fail\w*|down|offline|critical|compromised)"
    r"|\boxygen\s+(?:deplet\w*|critical|failing|dropping)"
    r"|\btoxic\s+atmosphere"
    r"|\bbreathable\s+air\s+(?:lost|gone|dropping)",
    re.IGNORECASE,
)

_RESTRICTED_ZONE = re.compile(
    r"\brestricted[\s-]zone[\s-]access"
    r"|\brestricted\s+area\s+(?:breach|access)"
    r"|\bclassified\s+(?:zone|area)\s+(?:breach|access)"
    r"|\bunauthori[sz]ed\s+entry\s+(?:into|to)\s+restricted",
    re.IGNORECASE,
)

_SAFETY_TRIGGERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hull_breach", _HULL_BREACH),
    ("atmosphere_compromise", _ATMOSPHERE_COMPROMISE),
    ("restricted_zone_access", _RESTRICTED_ZONE),
)


def triggered_reasons(request: TriageRequest) -> list[str]:
    """Return which safety triggers fired for this signal (may be empty).

    Runs on ``subject + description`` combined so triggers survive being
    mentioned in either field.
    """
    corpus = f"{request.subject}\n{request.description}"
    return [name for name, pattern in _SAFETY_TRIGGERS if pattern.search(corpus)]


def apply(request: TriageRequest, response: TriageResponse) -> TriageResponse:
    """Force ``P1`` + ``needs_escalation=true`` when any trigger fires.

    Category and team stay LLM-decided; forcing them here would eat F1
    without adding value.
    """
    reasons = triggered_reasons(request)
    if not reasons:
        return response

    # FrozenBaseModel is immutable — build a new instance.
    return response.model_copy(update={"priority": "P1", "needs_escalation": True})
