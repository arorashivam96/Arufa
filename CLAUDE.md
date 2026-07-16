# CLAUDE.md — Arufa engineering non-negotiables

> **Purpose.** This file is the standing brief for any AI coding agent (Claude
> Code, GitHub Copilot, subagents) working in this repo, and doubles as
> orientation for the Tier 2 review agents. It captures what changes when we
> merge, and what does not. Keep it tight; update it when a principle changes.
>
> **Companion docs**
> - [README.md](README.md) — user-facing overview and install
> - [PLAN.md](PLAN.md) — ordered implementation milestones
> - [docs/architecture.md](docs/architecture.md) — full system design
> - [docs/methodology.md](docs/methodology.md) — approach + iteration log
> - [docs/evals.md](docs/evals.md) — actual FDEBench numbers

---

## 1. What Arufa is (and isn't)

Arufa is a single async FastAPI service that exposes four endpoints
(`/health`, `/triage`, `/extract`, `/orchestrate`) scored by the FDEBench
benchmark. Its job is to turn messy customer inputs (mission signals,
document images, workflow goals with tool APIs and constraints) into
schema-locked JSON that a machine-driven ops platform can route, alarm on,
and audit. It is **not** a chatbot, a UI, a workflow *designer*, or a
data store — every response is a deterministic-shaped envelope, no
free-form prose, no persistent state between requests. When accuracy and
speed conflict, we favour a *balanced* score across all three tasks (the
FDEBench composite is the mean, so 80 / 80 / 80 beats 95 / 60 / 60).

---

## 2. Tech stack (locked)

| Layer | Choice | Why locked |
|---|---|---|
| Language | Python 3.12 | Challenge requirement + `uv`-managed via `Arufa/py/.venv` |
| Web framework | FastAPI + uvicorn (async) | Matches architect's stack in [`docs/challenge/task1/`](docs/challenge/task1/); async is required for probe 6 concurrency |
| Package/env manager | `uv` (workspace mode) | Challenge repo standard; `uv sync --all-packages` is the only supported bootstrap |
| Data models | Pydantic v2 | Every HTTP boundary + LLM output validated |
| Config | `pydantic-settings` from env / `.env` | Twelve-factor; secrets never in source |
| LLM provider | Azure OpenAI (single vendor) | Decision O3 — single retry loop, single quota, single header format |
| Primary model | `gpt-5-nano` (deployment `gpt-5-nano`) | FDEBench Nano tier (cost score 1.0) |
| Fallback model | `gpt-5-mini` (deployment `gpt-5-mini`) | Mini tier (cost score 0.9) — use only when a task can't hit accuracy floor on nano |
| Logging | `structlog` → stdout JSON | ACA ships stdout to Log Analytics automatically |
| HTTP client (T3 tools) | `httpx.AsyncClient` | Async, timeout-aware, plays well with `asyncio.gather` |
| Container runtime | Docker (non-root, multi-stage) | Deployment target is Azure Container Apps |
| Cloud | Azure — `shivamarora` RG, `westus` (ACA) + `eastus2` (AOAI) | Sub `92791f07-83ac-4f07-b2e6-51096ee0994d` |
| IaC | `azd` + Bicep in `infra/` | One-command deploy |
| CI | (deferred; not shipped in initial commit due to token-scope) | — |

**Do not** silently swap models, HTTP clients, or config libraries without
updating this file and `docs/methodology.md`.

---

## 3. Repository layout

```
Arufa/
├── README.md              # User-facing overview
├── CLAUDE.md              # This file
├── PLAN.md                # Milestones
├── LICENSE, SECURITY.md
├── Dockerfile             # Multi-stage, non-root, 8000
├── azure.yaml             # azd config
├── .env.example           # Non-secret template (root or per-app)
├── .vscode/
│   └── mcp.json           # Azure MCP for local dev
├── docs/
│   ├── architecture.md    # Shipped design (Tier 2 read)
│   ├── methodology.md     # Approach + iteration log (Tier 2 read)
│   ├── evals.md           # Actual numbers (Tier 2 read)
│   ├── challenge/         # Upstream task briefs — DO NOT EDIT
│   ├── eval/              # Upstream eval docs — DO NOT EDIT
│   └── submission/        # Upstream submission docs — DO NOT EDIT
├── py/
│   ├── pyproject.toml     # uv workspace root
│   ├── Makefile           # setup / run / eval targets
│   ├── apps/
│   │   ├── arufa/         # ← OUR SERVICE
│   │   │   ├── pyproject.toml
│   │   │   ├── arufa/                     # package (flat layout, matches apps/sample)
│   │   │   │   ├── main.py                # FastAPI app + route try/except → 200 + envelope
│   │   │   │   ├── shared/                # Kernel: llm client, obs, config, exception handlers, models
│   │   │   │   │   ├── config.py          # pydantic-settings Settings + get_settings()
│   │   │   │   │   ├── observability.py   # structlog + ContextVars (llm_call_var, request_id_var)
│   │   │   │   │   ├── middleware.py      # Pure ASGI: X-Request-Id / X-Latency-Ms / X-Model-Name headers
│   │   │   │   │   ├── exception_handlers.py  # 422 for malformed HTTP/JSON (probes 1–3, 5)
│   │   │   │   │   ├── llm/
│   │   │   │   │   │   ├── client.py      # LLMClient (Retry-After, semaphore, reasoning_effort)
│   │   │   │   │   │   ├── errors.py      # LLMUnavailable
│   │   │   │   │   │   └── result.py      # LLMResult dataclass
│   │   │   │   │   └── models/            # ErrorEntry + per-task Pydantic envelopes
│   │   │   │   ├── triage/                # Task 1
│   │   │   │   │   ├── pipeline.py
│   │   │   │   │   └── safety_rules.py    # (M4)
│   │   │   │   ├── extract/               # Task 2
│   │   │   │   │   ├── pipeline.py
│   │   │   │   │   └── normalizer.py      # (M5, optional)
│   │   │   │   └── orchestrate/           # Task 3
│   │   │   │       ├── pipeline.py
│   │   │   │       ├── tool_client.py     # (M6)
│   │   │   │       └── state.py           # (M6)
│   │   │   ├── prompts/                   # (M4+) system prompts as versioned .md files
│   │   │   ├── tests/                     # pytest, mocked LLM client via httpx.MockTransport
│   │   │   └── .env.example
│   │   ├── sample/        # Upstream reference stub — reference only, DO NOT DEPLOY
│   │   └── eval/          # Local eval harness — DO NOT EDIT (upstream)
│   ├── common/libs/       # Upstream libraries (fdebenchkit, fastapi helpers, models) — DO NOT EDIT
│   └── data/              # Upstream public eval data — DO NOT EDIT
├── Dockerfile             # Multi-stage, non-root, at repo root
├── .dockerignore
├── infra/                 # Bicep + azd (deferred: currently imperative az CLI, see PLAN.md T2)
└── ts/                    # TypeScript workspace — unused
```

**DO NOT EDIT** anything under `docs/challenge/`, `docs/eval/`, `docs/submission/`,
`py/common/libs/`, `py/data/`, or `py/apps/eval/`. They are upstream; edits create
merge conflicts on `git merge upstream/main`. If you find a bug, open an issue
upstream — do not patch locally.

**Layout deviation:** the arch doc mentioned `src/arufa/`; we ship flat
(`arufa/arufa/`) because it matches `apps/sample/` and avoids introducing
a new pattern into the repo. See [PLAN.md D1](PLAN.md#deviations-from-docsarchitecturemd-log-as-they-happen).

---

## 4. Non-negotiable architecture principles

Condensed from [`docs/architecture.md` §1](docs/architecture.md). If a
change would violate one of these, escalate before merging.

| # | Principle | Consequence if violated |
|---|---|---|
| **P1** | **Contract-first, schema-locked.** Every HTTP boundary is a Pydantic model. Enums are `Literal` unions with exact strings from the task briefs. | Wrong label strings → Resolution score = 0 on that dimension |
| **P2** | **200-with-envelope on valid inputs; 4xx only for malformed HTTP/JSON.** Engine crash on a valid item returns `HTTP 200` with blank/zero fields + `errors[]`. | Any 5xx on a valid item forfeits 100% of that item's score |
| **P3** | **`X-Model-Name`, `X-Latency-Ms`, `X-Token-Count` on every scored response — including the failure path.** | Missing `X-Model-Name` = cost tier 0.0 (loses 8 pp of task score) |
| **P4** | **Model tier is a knob.** Default to `gpt-5-nano` (tier 1.0). Escalate to `gpt-5-mini` only where accuracy floor demands, and document the reason in `docs/methodology.md`. | Premium models silently added → cost score drops without an accuracy justification we can defend |
| **P5** | **Own dependency reliability.** All AOAI calls go through `shared/llm/client.py` which honours `Retry-After`, enforces 25 s per-call timeout (< platform 60 s), and semaphore-bounds concurrency. | Platform's courtesy retries exhaust → `items_errored` → 0.0 across all dimensions |
| **P6** | **Deterministic safety net around LLM judgment.** Hull breach / atmosphere / restricted zone always escalate — enforced by `triage/safety_rules.py` post-LLM. | LLM misses one adversarial case → escalation F1 drops |
| **P7** | **Prompts are data.** System prompts live in `prompts/*.md` — never inlined in Python. | Version drift + Tier 2 Code Quality loss (Readability + Structure) |
| **P8** | **Deploy with `minReplicas ≥ 1`.** Never scale to zero on the scored container. | Probe 7 (cold start after 5 s idle) fails |
| **P9** | **Single-vendor LLM (AOAI).** No mixing OpenAI direct + Anthropic + AOAI. | Retry / header / cost logic fragments across pipelines |
| **P10** | **Consistency > peak.** FDEBench composite is `mean(T1, T2, T3)`. Do not over-invest in one task at the expense of the other two. | 95 / 60 / 60 = 71.7; 80 / 80 / 80 = 80 — do the math before optimising |

---

## 5. Coding standards

- **Type-hint everything.** Public function signatures + return types are mandatory. Prefer `T | None` over `Optional[T]`. Use `Literal[...]` for closed vocabularies.
- **Pydantic v2 idioms.** `BaseModel` for HTTP/LLM boundaries; `pydantic-settings` for config; `Field(...)` for constraints; `model_validate_json` on inbound bodies (FastAPI does this for you).
- **Async by default** for anything touching HTTP or the LLM client. No `time.sleep` — always `asyncio.sleep`. Never mix `requests` with `httpx.AsyncClient`.
- **One reason to change per module.** Route handlers don't parse prompts; pipelines don't set headers; the LLM client doesn't decide what to prompt. See the layered tree in [`docs/architecture.md` §3](docs/architecture.md).
- **No `print()`.** Use the structlog logger from `shared/observability.py`. Every log line carries `req_id`, `task`, and the task's ID field.
- **No bare `except:`.** Catch specific exceptions. In pipelines the outermost catch is `Exception` — that's the entry point for the `200 + errors[]` envelope path.
- **Naming.** `snake_case` for modules, functions, variables; `PascalCase` for classes; `SCREAMING_SNAKE` for env-var-derived constants. Deployment / model name env vars are prefixed `AOAI_`.
- **Line length** 100. Formatter: `ruff format`. Lint: `ruff check`. Both configured via `ruff.toml` at repo root.
- **Docstrings.** One-line summary on every public function or class. No docstrings on trivial getters or `__init__`.
- **Imports.** Absolute within `arufa.*`; standard-lib first, third-party second, local third. `ruff` handles sort.
- **File length.** If a module exceeds ~300 lines, split it before adding more.
- **No hidden state.** No module-level mutable dicts, no singletons other than the FastAPI app and structlog config. Config is passed in.

---

## 6. Testing conventions

- **Unit tests** in `py/apps/arufa/tests/`, one test file per source module.
- **Mock the LLM client, not httpx.** Tests import `shared/llm/client.py` and monkey-patch its `chat` method to return canned `LLMResult`s. Do not stub httpx transports in unit tests.
- **Contract tests** live in `tests/contract/`: for each endpoint, one test per resilience probe (malformed JSON, empty body, missing fields, huge payload, wrong content-type). Concurrency & cold-start probes are tested at deploy time, not unit.
- **Golden tests** for T1 safety rules: canonical hull/atmosphere/zone descriptions → assert `needs_escalation=True` and `priority=P1`.
- **No live LLM in the test suite.** Live-integration tests live under `tests/live/` and are `@pytest.mark.live`, opt-in via `pytest -m live`.
- **Coverage target** ≥ 70% on `shared/` and safety_rules; ≥ 50% overall. Enforce in CI when we add it.

---

## 7. Config & secrets

- **Never commit secrets.** `.env` is git-ignored; `.env.example` ships as a template with placeholder values only.
- **Env-var pattern:**
  - Local dev: `AOAI_AUTH_MODE=key` + `AOAI_API_KEY=…` (from `az cognitiveservices account keys list`)
  - Cloud: `AOAI_AUTH_MODE=aad` + Container App's managed identity granted `Cognitive Services OpenAI User` at the AOAI account scope. No key set in ACA.
- **All settings load through `shared/config.py`.** No `os.environ.get` scattered through the code.
- **Timeouts, retries, and concurrency are env-tunable.** Defaults live in `Settings`; overrides via env only.

**Canonical env vars** (mirror `.env.example`):

```env
AOAI_ENDPOINT=https://arufa-aoai-shivamarora.openai.azure.com/
AOAI_API_VERSION=2024-10-21
AOAI_DEPLOYMENT_NANO=gpt-5-nano
AOAI_DEPLOYMENT_MINI=gpt-5-mini
AOAI_MODEL_NAME_NANO=gpt-5-nano   # value written to X-Model-Name
AOAI_MODEL_NAME_MINI=gpt-5-mini
AOAI_AUTH_MODE=key                # key | aad
AOAI_API_KEY=                     # local-dev only, never committed
LLM_TIMEOUT_S=25
LLM_MAX_CONCURRENCY=8
LLM_MAX_RETRIES=3
LOG_LEVEL=INFO
```

---

## 8. API contract non-negotiables

Extends the [Challenge README HTTP semantics section](docs/challenge/README.md#http-semantics--when-to-return-200-vs-4xx) — memorise it.

| Situation | Status | Body |
|---|---|---|
| Valid request → success | `200` | Full envelope, `errors: []` |
| Valid request → engine failure (LLM timeout, tool 5xx, normaliser bug) | `200` | Envelope with IDs echoed, blank/zero fields, `errors: [{code, detail}]` |
| Malformed JSON / bad `Content-Type` | `400` / `415` | FastAPI default |
| Empty or `null` body | `400` or `422` | FastAPI default |
| 50 KB+ body | `413` or valid response (both count) | — |
| Backpressure (AOAI 429 propagated, TPM saturated) | `429` + `Retry-After` header | Platform will honour up to 10 s |

**IDs are always echoed.** `ticket_id` on `/triage`, `document_id` on
`/extract`, `task_id` on `/orchestrate`. The platform joins responses by
`request_id_key`, not by position — never rewrite or omit IDs.

**Every scored response carries** `X-Model-Name`, `X-Latency-Ms`,
`X-Token-Count`, `X-Request-Id`. The middleware guarantees this on the
failure path too.

---

## 9. LLM usage rules

- **All AOAI calls go through `arufa.shared.llm.client`.** No pipeline imports the OpenAI SDK or `httpx` for AOAI directly.
- **Structured output.** Use `response_format={"type": "json_schema", ...}` where the model supports it; otherwise JSON mode + Pydantic re-parse. Never free-form + regex.
- **Reasoning models.** `gpt-5-*` models spend completion-token budget on reasoning. For classifier calls (T1) set `reasoning_effort=minimal`. For planning (T3) `reasoning_effort=low` unless a specific benchmark shows it helps.
- **Vision.** Task 2 passes the base64-decoded PNG bytes as an `image_url` content part with `data:image/png;base64,...`. Always instruct the model to return `null` for unreadable fields.
- **Prompt discipline.** One file per system prompt. No f-string interpolation of untrusted input into the *system* prompt — task input goes in the user message.
- **Token budgeting.** Set `max_completion_tokens` per pipeline; default 2048 for classification, 4096 for extraction, 4096 for planning. Reasoning models: add ~1024 for reasoning budget.

---

## 10. Observability contract

- **Logs are JSON, one event per line, stdout only.** Container Apps → Log Analytics workspace `arufa-logs`.
- **Every request** logs `req_id`, `task`, the ID field (`ticket_id`/`document_id`/`task_id`), `model`, `prompt_tokens`, `completion_tokens`, `latency_ms`, `outcome`.
- **No PII, no full request bodies, no full LLM responses.** Log field names and sizes, not contents. Truncate string previews to 120 chars.
- **Errors log with `error_code`** (a short machine-readable string like `llm_timeout`, `llm_unavailable`, `tool_5xx`, `schema_validation`) and `error_detail` (human-readable).
- **Response headers are the primary telemetry surface for the platform.** Logs are for us.

---

## 11. Deployment conventions

- **Dockerfile:** multi-stage (`python:3.12-slim` builder + runtime), non-root user (`appuser`), `EXPOSE 8000`, `HEALTHCHECK` calls `/health`. Build context is the repo root; `.dockerignore` keeps `.venv`, caches, `.git`, and `.env*` out.
- **Image build:** use `az acr build` (cloud-side, ~45 s). Do **not** stream logs from a Windows PowerShell terminal — Colorama crashes on cp1252. Use `--no-wait --no-logs` and poll `az acr task list-runs` instead. Local `docker build` still works if Docker Desktop is running.
- **Azure Container Apps:** `minReplicas=1`, `maxReplicas=5`, per-replica concurrency `30`, external HTTPS ingress, system-assigned MI. Registry pull via MI (`--registry-identity system` on create — auto-grants `AcrPull` on the ACR).
- **AOAI auth on ACA:** M4+ deployments must grant the ACA MI `Cognitive Services OpenAI User` at the AOAI account scope and set `AOAI_AUTH_MODE=aad`. M3 skeleton uses `AOAI_AUTH_MODE=key` with no key (stubs don't call the model).
- **Deployed FQDN (current):** `https://arufa.mangohill-daf67e16.westus.azurecontainerapps.io`
- **Rollback:** `az containerapp revision set-mode --resource-group shivamarora --name arufa --mode single --revision <prior>`.
- **IaC status:** infra currently provisioned imperatively via `az` CLI (see `PLAN.md` tech debt T2). Migrate to Bicep or `azd`-tracked before M8.
- **Cost defence:** ACA scale-out capped at 5, AOAI capacity 50K TPM per deployment; both sized for hackathon load, not sustained production.

---

## 12. When editing this file

Update `CLAUDE.md` when:
- A locked tech choice changes (§2)
- A non-negotiable is added, removed, or reworded (§4)
- A coding-standard rule is added or relaxed (§5)
- The API contract shape changes (§8)
- The deploy topology changes (§11)

Do **not** clutter this file with milestone-level status — that lives in [`PLAN.md`](PLAN.md). Do **not** log iteration decisions here — that lives in [`docs/methodology.md`](docs/methodology.md).

---

## 13. Quick commands

> **Windows note:** the tool shell here occasionally strips inline `cd` commands. Prefer absolute paths where possible. `uv run` from a directory without a `pyproject.toml` picks up the system Python; always run from within `py/` or `py/apps/arufa/`, or use `--directory`.

```powershell
# One-time setup
uv sync --all-packages --directory C:\Repos\Coding-Challenge\Arufa\py

# Run our service locally
C:\Repos\Coding-Challenge\Arufa\py\.venv\Scripts\uvicorn.exe arufa.main:app `
    --port 8000 --host 127.0.0.1 `
    --app-dir C:\Repos\Coding-Challenge\Arufa\py\apps\arufa

# Run the mock tool service for T3 local eval (separate terminal)
C:\Repos\Coding-Challenge\Arufa\py\.venv\Scripts\python.exe `
    C:\Repos\Coding-Challenge\Arufa\py\apps\eval\mock_tool_service.py

# Score locally
C:\Repos\Coding-Challenge\Arufa\py\.venv\Scripts\python.exe `
    C:\Repos\Coding-Challenge\Arufa\py\apps\eval\run_eval.py --endpoint http://localhost:8000
# Single task:
#   ... run_eval.py --endpoint http://localhost:8000 --task triage

# Tests
C:\Repos\Coding-Challenge\Arufa\py\.venv\Scripts\pytest.exe `
    C:\Repos\Coding-Challenge\Arufa\py\apps\arufa\tests -v

# Format & lint
uv run --directory C:\Repos\Coding-Challenge\Arufa\py ruff format .
uv run --directory C:\Repos\Coding-Challenge\Arufa\py ruff check --fix .

# Type check
uv run --directory C:\Repos\Coding-Challenge\Arufa\py pyright apps/arufa

# Deploy: build + push image, update revision
$env:NO_COLOR = "1"
az acr build --registry arufaacrshivamarora --resource-group shivamarora `
    --image arufa:latest --file C:\Repos\Coding-Challenge\Arufa\Dockerfile `
    --no-wait --no-logs C:\Repos\Coding-Challenge\Arufa
# ... poll az acr task list-runs, then:
az containerapp update --resource-group shivamarora --name arufa `
    --image arufaacrshivamarora.azurecr.io/arufa:latest

# Rebuild image and push a new revision
azd deploy

# Get AOAI key (do not commit)
az cognitiveservices account keys list -n arufa-aoai-shivamarora -g shivamarora --query key1 -o tsv

# Pull upstream challenge updates
git fetch upstream; git merge upstream/main
```

---

## 14. Reference — what judges will read

Tier 2 review agents will read the repo end-to-end. What we want them to see:

- **[`README.md`](README.md)** — clear install + run instructions
- **This file** — engineering principles + tech-stack rationale
- **[`docs/architecture.md`](docs/architecture.md)** — system design, per-task pipelines, tradeoffs
- **[`docs/methodology.md`](docs/methodology.md)** — approach, iteration log, honest failure notes
- **[`docs/evals.md`](docs/evals.md)** — actual `run_eval.py` numbers + limitations
- **[`PLAN.md`](PLAN.md)** — how the work was decomposed
- **`py/apps/arufa/src/`** — layered code with type hints and mocked-LLM tests
- **`prompts/`** — system prompts as versioned files, not glued into Python
- **`Dockerfile` + `azure.yaml`** — reproducible deploy
- **`.env.example`** — no secrets, template only
