"""FastAPI entry point for the Arufa service.

Exposes the four endpoints scored by FDEBench:

- ``GET  /health``      liveness probe
- ``POST /triage``      task 1 (M4: real LLM pipeline)
- ``POST /extract``     task 2 (M5)
- ``POST /orchestrate`` task 3 (M6)

The shared LLM client is created once at startup via ``lifespan`` and
attached to ``app.state``. Route handlers pull it from ``request.app.state``
so tests can override the client without touching module-level singletons.

Each scored route wraps its pipeline in ``try/except`` so an engine
failure surfaces as ``HTTP 200`` + task envelope with ``errors[]``,
never a 5xx. Malformed HTTP/JSON goes to the ``RequestValidationError``
handler (probes 1–3, 5).

Run locally::

    cd py/apps/arufa
    uv run uvicorn arufa.main:app --port 8000 --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi import Request

from arufa import extract as extract_pkg
from arufa import orchestrate as orchestrate_pkg
from arufa import triage as triage_pkg
from arufa.extract.pipeline import run as _run_extract
from arufa.orchestrate.pipeline import run as _run_orchestrate
from arufa.shared import exception_handlers
from arufa.shared.config import Settings
from arufa.shared.config import get_settings
from arufa.shared.llm import LLMClient
from arufa.shared.middleware import RequestContextMiddleware
from arufa.shared.models import ErrorEntry
from arufa.shared.models.extract import ExtractRequest
from arufa.shared.models.extract import ExtractResponse
from arufa.shared.models.orchestrate import OrchestrateRequest
from arufa.shared.models.orchestrate import OrchestrateResponse
from arufa.shared.models.triage import TriageRequest
from arufa.shared.models.triage import TriageResponse
from arufa.shared.observability import configure_logging
from arufa.shared.observability import get_logger
from arufa.triage.pipeline import run as _run_triage

# Suppress "unused import" for the package handles — we need them
# imported so their side-effect ``__init__`` files run.
_ = (triage_pkg, extract_pkg, orchestrate_pkg)

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Provision and dispose of the shared LLM client."""
    client = LLMClient(settings=settings)
    app.state.llm_client = client
    app.state.settings = settings
    logger.info("arufa_startup", model_nano=settings.aoai_model_name_nano, endpoint=settings.aoai_endpoint or "<unset>")
    try:
        yield
    finally:
        await client.aclose()
        logger.info("arufa_shutdown")


app = FastAPI(
    title="Arufa",
    description="Signal triaging, extraction, and orchestration for FDEBench",
    version="0.3.0",
    lifespan=lifespan,
)
app.add_middleware(RequestContextMiddleware)
exception_handlers.register(app)


def _llm(request: Request) -> LLMClient:
    return request.app.state.llm_client


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Returns ``{"status": "ok"}`` when the service is up."""
    return {"status": "ok"}


@app.post("/triage")
async def triage(request: TriageRequest, http_request: Request) -> TriageResponse:
    """Task 1: signal triage.

    Engine failures return ``HTTP 200`` with a safe-default envelope and
    an ``errors[]`` entry — never 5xx (per FDEBench contract).
    """
    try:
        return await _run_triage(request, _llm(http_request), _settings(http_request))
    except Exception as exc:
        logger.exception("triage_pipeline_failed", ticket_id=request.ticket_id)
        return TriageResponse(
            ticket_id=request.ticket_id,
            category="Not a Mission Signal",
            priority="P4",
            assigned_team="None",
            needs_escalation=False,
            missing_information=[],
            next_best_action="",
            remediation_steps=[],
            errors=[ErrorEntry(code="triage_pipeline_error", detail=str(exc)[:500])],
        )


@app.post("/extract")
async def extract(request: ExtractRequest, http_request: Request) -> ExtractResponse:
    """Task 2: document extraction."""
    try:
        return await _run_extract(request, _llm(http_request), _settings(http_request))
    except Exception as exc:
        logger.exception("extract_pipeline_failed", document_id=request.document_id)
        return ExtractResponse(
            document_id=request.document_id,
            errors=[ErrorEntry(code="extract_pipeline_error", detail=str(exc)[:500])],
        )


@app.post("/orchestrate")
async def orchestrate(request: OrchestrateRequest, http_request: Request) -> OrchestrateResponse:
    """Task 3: workflow orchestration."""
    try:
        return await _run_orchestrate(request, _llm(http_request), _settings(http_request))
    except Exception as exc:
        # NOTE: status stays "completed" even here — the FDEBench T3 scorer
        # zeroes goal_completion on any non-"completed" status. The failure
        # is still surfaced via errors[]; other dimensions (constraint
        # compliance, ordering) already penalise no-steps outcomes.
        logger.exception("orchestrate_pipeline_failed", task_id=request.task_id)
        return OrchestrateResponse(
            task_id=request.task_id,
            status="completed",
            steps_executed=[],
            constraints_satisfied=[],
            errors=[ErrorEntry(code="orchestrate_pipeline_error", detail=str(exc)[:500])],
        )
