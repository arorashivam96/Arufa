"""Task 2 pipeline: vision-driven document extraction.

Flow:

1. Load the versioned system prompt from ``prompts/extract_system.md``.
2. Build a vision-enabled user message: the ``json_schema`` (as text) plus
   the base64 image as a ``data:image/png;base64,`` URL. Uses
   ``detail: high`` so text and tables render at full resolution — matters
   on the ~36% photographed/handwritten adversarial subset.
3. Call the LLM (``gpt-5-mini``: vision-capable, mini tier). JSON object
   mode; the schema is described in the prompt rather than enforced via
   strict JSON-schema mode because the wire schemas often use features
   ``strict=true`` rejects (``oneOf``, missing ``additionalProperties``).
4. Parse the JSON. On failure → return the document_id echoed with
   ``errors[]`` (200 semantics).
"""

from __future__ import annotations

import json
from typing import Any

from arufa.shared.config import Settings
from arufa.shared.llm import LLMClient
from arufa.shared.llm import LLMUnavailable
from arufa.shared.models import ErrorEntry
from arufa.shared.models.extract import ExtractRequest
from arufa.shared.models.extract import ExtractResponse
from arufa.shared.observability import get_logger
from arufa.shared.prompts import load as load_prompt

logger = get_logger(__name__)


def _schema_as_text(json_schema: Any) -> str:
    """Render the request's ``json_schema`` as JSON text for the prompt."""
    if json_schema is None:
        return "{}"
    if isinstance(json_schema, str):
        return json_schema
    return json.dumps(json_schema, ensure_ascii=False)


def _build_messages(system_prompt: str, request: ExtractRequest) -> list[dict[str, Any]]:
    """Compose the chat messages, with the image inlined as a data URL."""
    schema_text = _schema_as_text(request.json_schema)
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Extract this document per the following JSON schema. "
                        "Return only the JSON object.\n\n"
                        f"Schema:\n{schema_text}"
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{request.content}",
                        "detail": "high",
                    },
                },
            ],
        },
    ]


def _extract_json(content: str) -> dict[str, Any]:
    """Coerce ``content`` into a dict; handle ```json fences and prose wrap."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _errored(document_id: str, code: str, detail: str) -> ExtractResponse:
    return ExtractResponse(
        document_id=document_id,
        errors=[ErrorEntry(code=code, detail=detail[:400])],
    )


async def run(
    request: ExtractRequest,
    llm: LLMClient,
    settings: Settings,
) -> ExtractResponse:
    """Extract structured fields from a document image."""
    system_prompt = load_prompt("extract_system")
    messages = _build_messages(system_prompt, request)

    try:
        result = await llm.chat(
            deployment=settings.aoai_deployment_mini,
            model_name=settings.aoai_model_name_mini,
            messages=messages,
            response_format={"type": "json_object"},
            max_completion_tokens=4096,
            reasoning_effort="minimal",
        )
    except LLMUnavailable as exc:
        logger.warning("extract_llm_unavailable", document_id=request.document_id, detail=exc.detail)
        return _errored(request.document_id, "llm_unavailable", exc.detail)

    try:
        payload = _extract_json(result.content)
    except json.JSONDecodeError as exc:
        preview = result.content[:200].replace("\n", " ")
        logger.warning(
            "extract_parse_error", document_id=request.document_id, preview=preview
        )
        return _errored(request.document_id, "llm_parse_error", str(exc))

    if not isinstance(payload, dict):
        return _errored(request.document_id, "llm_parse_error", "top-level JSON was not an object")

    # Build the response envelope. ``document_id`` overrides anything the
    # model returned; the rest of the fields flow via extra="allow".
    payload_without_id = {k: v for k, v in payload.items() if k != "document_id"}
    return ExtractResponse(document_id=request.document_id, **payload_without_id)
