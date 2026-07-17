"""Smoke tests for the three scored endpoints.

Verifies that the routes exist, accept the input schema, and return
schema-valid envelopes. Value-level assertions live in per-pipeline
test files (``test_triage_pipeline.py`` etc.) with mocked LLM clients —
those are the deterministic ones. This file only checks the HTTP layer.
"""

from __future__ import annotations

from typing import get_args

from fastapi.testclient import TestClient

from arufa.main import app
from arufa.shared.models.triage import Category
from arufa.shared.models.triage import Priority
from arufa.shared.models.triage import Team


def _triage_payload() -> dict:
    return {
        "ticket_id": "SIG-TEST-001",
        "subject": "Test signal",
        "description": "just a probe",
        "reporter": {
            "name": "Test User",
            "email": "test@example.com",
            "department": "Test",
        },
        "created_at": "2026-01-01T00:00:00Z",
        "channel": "bridge_terminal",
        "attachments": [],
    }


def _extract_payload() -> dict:
    # 1x1 PNG (transparent) base64; content shape must match input_schema
    tiny_png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVQIW2P4//8/AAX+Av7"
        "czFnnAAAAAElFTkSuQmCC"
    )
    return {
        "document_id": "DOC-TEST-001",
        "content_format": "image_base64",
        "content": tiny_png,
        "json_schema": {"type": "object", "properties": {"foo": {"type": "string"}}},
    }


def _orchestrate_payload() -> dict:
    return {
        "task_id": "TASK-TEST-001",
        "goal": "test goal",
        "available_tools": [
            {
                "name": "noop",
                "description": "does nothing",
                "endpoint": "http://localhost:9090/noop",
                "parameters": [],
            }
        ],
        "constraints": [],
        "mock_service_url": "http://localhost:9090",
    }


def test_triage_endpoint_returns_valid_envelope() -> None:
    """/triage returns a schema-valid envelope; concrete values are
    tested in ``test_triage_pipeline.py`` with mocked LLMs."""
    with TestClient(app) as client:
        r = client.post("/triage", json=_triage_payload())
    assert r.status_code == 200
    body = r.json()
    for k in (
        "ticket_id",
        "category",
        "priority",
        "assigned_team",
        "needs_escalation",
        "missing_information",
        "next_best_action",
        "remediation_steps",
    ):
        assert k in body, f"missing {k}"
    assert body["ticket_id"] == "SIG-TEST-001"
    assert body["category"] in get_args(Category)
    assert body["priority"] in get_args(Priority)
    assert body["assigned_team"] in get_args(Team)
    assert isinstance(body["missing_information"], list)


def test_extract_endpoint_returns_valid_envelope() -> None:
    with TestClient(app) as client:
        r = client.post("/extract", json=_extract_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["document_id"] == "DOC-TEST-001"


def test_orchestrate_endpoint_returns_valid_envelope() -> None:
    with TestClient(app) as client:
        r = client.post("/orchestrate", json=_orchestrate_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == "TASK-TEST-001"
    assert body["status"] in ("completed", "partial", "failed")
    assert isinstance(body["steps_executed"], list)


def test_middleware_headers_present_on_all_scored_endpoints() -> None:
    with TestClient(app) as client:
        for path, payload in (
            ("/triage", _triage_payload()),
            ("/extract", _extract_payload()),
            ("/orchestrate", _orchestrate_payload()),
        ):
            r = client.post(path, json=payload)
            assert r.status_code == 200
            assert r.headers.get("x-request-id"), f"no x-request-id on {path}"
            assert r.headers.get("x-latency-ms"), f"no x-latency-ms on {path}"
