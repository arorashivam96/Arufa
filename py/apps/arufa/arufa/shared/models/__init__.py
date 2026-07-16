"""Common Pydantic types shared across task envelopes."""

from ms.common.models.base import FrozenBaseModel


class ErrorEntry(FrozenBaseModel):
    """A single failure entry that ships inside a task response envelope.

    The scored endpoints return ``HTTP 200`` with the task's normal
    response envelope even when the engine fails on a valid request. The
    ``errors`` list on the response describes what went wrong so the
    scorer can attribute the misclassification and so operators can debug.
    """

    code: str
    detail: str
