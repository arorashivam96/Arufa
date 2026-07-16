"""Exceptions raised by the LLM client."""


class LLMError(Exception):
    """Base class for LLM client failures."""


class LLMUnavailable(LLMError):
    """The LLM could not produce a response within the retry budget.

    Pipelines catch this and return their task's response envelope with
    an ``errors`` entry rather than surfacing an HTTP 5xx (see the
    200-vs-4xx rule).
    """

    def __init__(self, detail: str, attempts: int) -> None:
        super().__init__(f"{detail} (after {attempts} attempts)")
        self.detail = detail
        self.attempts = attempts
