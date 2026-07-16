"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from arufa.shared.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings that don't touch the real environment."""
    return Settings(
        aoai_endpoint="https://test-aoai.example.com/",
        aoai_api_version="2024-10-21",
        aoai_deployment_nano="gpt-5-nano",
        aoai_deployment_mini="gpt-5-mini",
        aoai_model_name_nano="gpt-5-nano",
        aoai_model_name_mini="gpt-5-mini",
        aoai_auth_mode="key",
        aoai_api_key="test-key",
        llm_timeout_s=5.0,
        llm_max_concurrency=4,
        llm_max_retries=3,
        log_level="WARNING",
    )
