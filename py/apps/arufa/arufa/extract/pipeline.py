"""Task 2 pipeline entry point.

M2 stub: echoes ``document_id`` and no extracted fields. The real
vision-driven extractor lands in M5.
"""

from __future__ import annotations

from arufa.shared.models.extract import ExtractRequest
from arufa.shared.models.extract import ExtractResponse


async def run(request: ExtractRequest) -> ExtractResponse:
    """Return a stub extraction result."""
    return ExtractResponse(document_id=request.document_id)
