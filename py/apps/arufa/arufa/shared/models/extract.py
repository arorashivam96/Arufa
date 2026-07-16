"""Task 2 (Document Extraction) request and response models.

The response shape is **dynamic**: each request supplies a
``json_schema`` that defines the fields we must return. So the response
uses ``ConfigDict(extra="allow")`` and callers merge extracted fields
into a plain dict at construction time.
"""

from __future__ import annotations

from typing import Any
from typing import Literal

from ms.common.models.base import FrozenBaseModel
from pydantic import ConfigDict

from arufa.shared.models import ErrorEntry


class ExtractRequest(FrozenBaseModel):
    """Document image + target schema."""

    document_id: str
    content_format: Literal["image_base64"] = "image_base64"
    content: str
    """Base64-encoded PNG bytes."""
    json_schema: Any = None
    """JSON-schema describing the expected output. Shape may be a dict, a
    JSON-encoded string, or absent, depending on the request."""


class ExtractResponse(FrozenBaseModel):
    """Response envelope with a dynamic set of extracted fields.

    Consumers build this via ``ExtractResponse.model_validate({...})``
    with whatever field set matches the request's ``json_schema``.
    ``document_id`` is the only guaranteed key.
    """

    document_id: str
    errors: list[ErrorEntry] = []

    model_config = ConfigDict(extra="allow")
