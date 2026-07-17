"""Typed application settings loaded from the environment.

All configuration comes through this module. No ``os.environ.get`` calls
elsewhere. Local development reads from ``.env`` in the app root; the
Container App overrides these via env vars.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict

# arufa/shared/config.py → parent=shared, parent.parent=arufa, parent.parent.parent=apps/arufa/
_APP_ROOT = Path(__file__).parent.parent.parent
_ENV_FILE = _APP_ROOT / ".env"


class Settings(BaseSettings):
    """Env-driven service configuration.

    Attributes trace back to ``.env.example``. Do not add unused fields
    here; add them when the code that reads them lands.
    """

    aoai_endpoint: str = Field(default="", description="AOAI resource endpoint, e.g. https://<name>.openai.azure.com/")
    aoai_api_version: str = Field(default="2024-10-21")
    aoai_deployment_nano: str = Field(default="gpt-5-nano")
    aoai_deployment_mini: str = Field(default="gpt-5-mini")
    aoai_model_name_nano: str = Field(default="gpt-5-nano", description="Value written to X-Model-Name for nano-tier calls")
    aoai_model_name_mini: str = Field(default="gpt-5-mini", description="Value written to X-Model-Name for mini-tier calls")
    aoai_auth_mode: Literal["key", "aad"] = Field(default="key")
    aoai_api_key: str | None = Field(default=None)

    llm_timeout_s: float = Field(default=25.0, description="Per-attempt timeout; kept below platform 60 s ceiling")
    llm_max_concurrency: int = Field(default=8, description="Async semaphore ceiling protecting AOAI TPM/RPM quota")
    llm_max_retries: int = Field(default=3, description="Total attempts, incl. first try")

    log_level: str = Field(default="INFO")

    model_config = SettingsConfigDict(
        # Resolve .env relative to the app root so uvicorn works from any cwd.
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()
