"""Tests for :mod:`arufa.extract.pipeline`.

Mocks the LLM client. Verifies:

* Happy path: LLM returns valid JSON per requested schema → response
  merges fields via ``ExtractResponse(extra="allow")``.
* Base64 image + schema flow through to the LLM call correctly
  (vision content part, ``detail: high``, schema in text part).
* Malformed JSON → default with ``errors[]``.
* ``LLMUnavailable`` → default with ``errors[]``.
* The pipeline never overwrites ``document_id`` even if the LLM returns
  a different one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from arufa.extract import pipeline
from arufa.shared.config import Settings
from arufa.shared.llm import LLMResult
from arufa.shared.llm import LLMUnavailable
from arufa.shared.models.extract import ExtractRequest


@dataclass
class _StubLLM:
    response: Any

    async def chat(self, **kwargs: Any) -> LLMResult:
        self.last_kwargs = kwargs
        if isinstance(self.response, Exception):
            raise self.response
        return self.response  # type: ignore[return-value]


def _req(document_id: str = "DOC-1", schema: Any = None, b64: str = "AAA=") -> ExtractRequest:
    return ExtractRequest(
        document_id=document_id,
        content_format="image_base64",
        content=b64,
        json_schema=schema or {"type": "object", "properties": {"total": {"type": "number"}}},
    )


def _result(content: str) -> LLMResult:
    return LLMResult(
        content=content,
        model_name="gpt-5-mini",
        prompt_tokens=500,
        completion_tokens=100,
        raw={},
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        aoai_endpoint="https://test.example.com/",
        aoai_deployment_mini="gpt-5-mini",
        aoai_model_name_mini="gpt-5-mini",
        aoai_auth_mode="key",
        aoai_api_key="test",
    )


# ---- happy path ------------------------------------------------------


async def test_happy_path_merges_extracted_fields(settings: Settings) -> None:
    payload = {"total": 42.5, "vendor": "Acme"}
    llm = _StubLLM(response=_result(json.dumps(payload)))
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    dumped = out.model_dump()
    assert dumped["document_id"] == "DOC-1"
    assert dumped["total"] == 42.5
    assert dumped["vendor"] == "Acme"
    assert dumped["errors"] == []


async def test_llm_receives_vision_content_with_high_detail(settings: Settings) -> None:
    llm = _StubLLM(response=_result('{"total": 1}'))
    await pipeline.run(_req(b64="XYZ_B64"), llm=llm, settings=settings)  # type: ignore[arg-type]

    kwargs = llm.last_kwargs
    assert kwargs["deployment"] == "gpt-5-mini"
    assert kwargs["model_name"] == "gpt-5-mini"
    messages = kwargs["messages"]
    # System + one user message
    assert messages[0]["role"] == "system"
    user_content = messages[1]["content"]
    assert isinstance(user_content, list)
    # One text part with the schema and one image part with detail=high
    kinds = {part["type"] for part in user_content}
    assert kinds == {"text", "image_url"}
    image_part = next(p for p in user_content if p["type"] == "image_url")
    assert image_part["image_url"]["detail"] == "high"
    assert image_part["image_url"]["url"] == "data:image/png;base64,XYZ_B64"


async def test_code_fenced_json_is_unwrapped(settings: Settings) -> None:
    fenced = '```json\n{"total": 1, "vendor": "X"}\n```'
    llm = _StubLLM(response=_result(fenced))
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.model_dump()["vendor"] == "X"


async def test_document_id_never_overwritten(settings: Settings) -> None:
    """LLM outputs a different document_id → pipeline still echoes the request's."""
    llm = _StubLLM(response=_result('{"document_id": "WRONG", "total": 1}'))
    out = await pipeline.run(_req(document_id="DOC-42"), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.document_id == "DOC-42"


async def test_string_schema_is_accepted(settings: Settings) -> None:
    """Some scenarios ship json_schema as a string, not a dict."""
    schema_str = '{"type":"object","properties":{"foo":{"type":"string"}}}'
    llm = _StubLLM(response=_result('{"foo": "bar"}'))
    out = await pipeline.run(_req(schema=schema_str), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.model_dump()["foo"] == "bar"


# ---- failure paths ---------------------------------------------------


async def test_malformed_json_returns_errored_envelope(settings: Settings) -> None:
    llm = _StubLLM(response=_result("this is not json"))
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.document_id == "DOC-1"
    assert out.errors and out.errors[0].code == "llm_parse_error"


async def test_llm_unavailable_returns_errored_envelope(settings: Settings) -> None:
    llm = _StubLLM(response=LLMUnavailable("aoai down", attempts=3))
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.errors and out.errors[0].code == "llm_unavailable"


async def test_non_object_top_level_returns_errored_envelope(settings: Settings) -> None:
    llm = _StubLLM(response=_result("[1, 2, 3]"))
    out = await pipeline.run(_req(), llm=llm, settings=settings)  # type: ignore[arg-type]
    assert out.errors and out.errors[0].code == "llm_parse_error"
