"""Return type for :meth:`arufa.shared.llm.client.LLMClient.chat`."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LLMResult:
    """Successful chat-completion result."""

    content: str
    """Assistant message text, as returned by the model."""

    model_name: str
    """Canonical model name (for the ``X-Model-Name`` response header)."""

    prompt_tokens: int
    completion_tokens: int

    raw: dict[str, Any]
    """The full JSON payload returned by AOAI. Kept for downstream parsing."""
