"""Deterministic category → team mapping for T1.

The valid-labels spec (`docs/challenge/task1/README.md`) gives a 1:1
mapping between the 8 categories and the 7 teams (`Not a Mission Signal`
and `Mission Briefing Request` share `None` / `Mission Software
Operations` respectively; the remaining 6 categories each pair with a
single team).

Since the classifier head sometimes emits a valid team that doesn't
match its own emitted category, this module supplies the authoritative
mapping and a helper that clamps team to the canonical partner for the
given category.

This is a routing-F1 safety net, not a category safety net — category
comes first, team follows deterministically.
"""

from __future__ import annotations

from arufa.shared.models.triage import Category
from arufa.shared.models.triage import Team

CATEGORY_TO_TEAM: dict[Category, Team] = {
    "Crew Access & Biometrics": "Crew Identity & Airlock Control",
    "Hull & Structural Systems": "Spacecraft Systems Engineering",
    "Communications & Navigation": "Deep Space Communications",
    "Flight Software & Instruments": "Mission Software Operations",
    "Threat Detection & Containment": "Threat Response Command",
    "Telemetry & Data Banks": "Telemetry & Data Core",
    "Mission Briefing Request": "Mission Software Operations",
    "Not a Mission Signal": "None",
}


def team_for_category(category: Category) -> Team:
    """Return the canonical team for ``category`` (1:1 mapping)."""
    return CATEGORY_TO_TEAM[category]
