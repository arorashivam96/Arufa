"""Task 1 (Signal Triage) request and response models.

Vocabularies match ``docs/challenge/task1/README.md`` exactly. Enum
strings are what the scorer compares against — do not rename these.
"""

from __future__ import annotations

from typing import Literal

from ms.common.models.base import FrozenBaseModel
from pydantic import ConfigDict
from pydantic import EmailStr

from arufa.shared.models import ErrorEntry

# Fixed vocabularies from docs/challenge/task1/README.md#valid-labels

Category = Literal[
    "Crew Access & Biometrics",
    "Hull & Structural Systems",
    "Communications & Navigation",
    "Flight Software & Instruments",
    "Threat Detection & Containment",
    "Telemetry & Data Banks",
    "Mission Briefing Request",
    "Not a Mission Signal",
]

Team = Literal[
    "Crew Identity & Airlock Control",
    "Spacecraft Systems Engineering",
    "Deep Space Communications",
    "Mission Software Operations",
    "Threat Response Command",
    "Telemetry & Data Core",
    "None",
]

Priority = Literal["P1", "P2", "P3", "P4"]

MissingInfo = Literal[
    "affected_subsystem",
    "anomaly_readout",
    "sequence_to_reproduce",
    "affected_crew",
    "habitat_conditions",
    "stardate",
    "previous_signal_id",
    "crew_contact",
    "module_specs",
    "software_version",
    "sector_coordinates",
    "mission_impact",
    "recurrence_pattern",
    "sensor_log_or_capture",
    "biometric_method",
    "system_configuration",
]

Channel = Literal[
    "subspace_relay",
    "holodeck_comm",
    "bridge_terminal",
    "emergency_beacon",
]


class Reporter(FrozenBaseModel):
    """Reporter block from the T1 input schema."""

    name: str
    email: EmailStr
    department: str


class TriageRequest(FrozenBaseModel):
    """Incoming mission signal to triage."""

    ticket_id: str
    subject: str
    description: str
    reporter: Reporter
    created_at: str
    channel: Channel
    attachments: list[str] = []


class TriageResponse(FrozenBaseModel):
    """Response envelope; every field is required by the scorer."""

    ticket_id: str
    category: Category
    priority: Priority
    assigned_team: Team
    needs_escalation: bool
    missing_information: list[MissingInfo]
    next_best_action: str
    remediation_steps: list[str]
    errors: list[ErrorEntry] = []

    # Response is Pydantic-frozen elsewhere, but we allow extra=ignore so
    # older/newer optional fields on the wire don't blow up validation.
    model_config = ConfigDict(extra="ignore")
