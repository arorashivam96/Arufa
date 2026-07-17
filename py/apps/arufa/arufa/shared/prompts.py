"""Load system prompts from versioned ``prompts/*.md`` files.

Prompts are data, not code. Editing a prompt file is a *content* change
that doesn't require Python edits, which keeps `docs/methodology.md`
iteration cycles tidy: one prompt tweak = one file diff.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

# arufa/shared/prompts.py → parent=shared, parent.parent=arufa, parent.parent.parent=apps/arufa/
_APP_ROOT = Path(__file__).parent.parent.parent
_PROMPT_DIR = _APP_ROOT / "prompts"


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """Return the ``.md`` prompt text for ``name`` (e.g. ``"triage_system"``).

    Cached: the first call reads from disk, subsequent calls are ``O(1)``.
    """
    path = _PROMPT_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8")
