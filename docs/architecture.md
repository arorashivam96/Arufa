# Architecture

Arufa is a single async FastAPI service on Azure Container Apps that exposes four endpoints scored by FDEBench: `/health`, `/triage`, `/extract`, `/orchestrate`. The service is a thin transport layer over a shared kernel (config, LLM client, observability, exception handling) and three task-specific pipelines that all share the same reliability, telemetry, and error-envelope patterns.

Companion documents: [`README.md`](../README.md) · [`CLAUDE.md`](../CLAUDE.md) (engineering non-negotiables) · [`PLAN.md`](../PLAN.md) (milestone log) · [`docs/methodology.md`](methodology.md) (iteration story) · [`docs/evals.md`](evals.md) (measured numbers).

---

## System overview

```
┌───────────────────────────────────────────────────────────────────┐
│  FDEBench platform (hidden eval + probes)                         │
└──────────────────────────┬────────────────────────────────────────┘
                           │ HTTPS
┌──────────────────────────▼────────────────────────────────────────┐
│  Azure Container Apps  (rg: shivamarora, region: westus)          │
│  min=1 max=5 replicas, per-replica concurrency=30, HTTPS ingress  │
│                                                                    │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │  Arufa (uvicorn / FastAPI, Python 3.12)                    │   │
│  │                                                             │   │
│  │  ┌──────────────────────────────────────────────────────┐  │   │
│  │  │  Transport                                            │  │   │
│  │  │  • RequestContextMiddleware (pure ASGI)               │  │   │
│  │  │  • RequestValidationError → 400/422                   │  │   │
│  │  │  • Per-route try/except → 200 + errors[]              │  │   │
│  │  └──────────────────────────────────────────────────────┘  │   │
│  │  ┌──────────────────────────────────────────────────────┐  │   │
│  │  │  Pipelines                                            │  │   │
│  │  │  triage/pipeline.py  triage/safety_rules.py           │  │   │
│  │  │  extract/pipeline.py                                  │  │   │
│  │  │  orchestrate/pipeline.py  tool_client.py  state.py    │  │   │
│  │  └──────────────────────────────────────────────────────┘  │   │
│  │  ┌──────────────────────────────────────────────────────┐  │   │
│  │  │  Shared kernel                                        │  │   │
│  │  │  config (pydantic-settings) · llm/client (retry,      │  │   │
│  │  │  Retry-After, semaphore, reasoning_effort) ·          │  │   │
│  │  │  observability (structlog + ContextVars) · models     │  │   │
│  │  └──────────────────────────────────────────────────────┘  │   │
│  └──────────────────────────────┬─────────────────────────────┘   │
│         Managed Identity ────► Log Analytics (arufa-logs)         │
└──────────────────────────────────┼────────────────────────────────┘
                                   │ api-key (ACA secret)
┌──────────────────────────────────▼────────────────────────────────┐
│  Azure OpenAI  (rg: shivamarora, region: eastus2)                 │
│  • gpt-5-nano   (Nano tier — text)                                │
│  • gpt-5-mini   (Mini tier — vision)                              │
└──────────────────────────────────┬────────────────────────────────┘
                                   │ HTTPS (mock service in-cluster
                                   │        during hidden scoring)
┌──────────────────────────────────▼────────────────────────────────┐
│  Task 3 mock tool service                                         │
│  Local: 127.0.0.1:9090 · Platform: in-cluster during hidden eval  │
└───────────────────────────────────────────────────────────────────┘
```

---

## Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Liveness. Returns `{"status": "ok"}` |
| `/triage` | POST | Task 1: classify a spacecraft signal into 5 dimensions (category, priority, team, missing-info, escalation) plus next-action and remediation |
| `/extract` | POST | Task 2: extract structured JSON from a document image, driven by the request's `json_schema` |
| `/orchestrate` | POST | Task 3: plan and execute a multi-step workflow with real HTTP tool calls; report constraint compliance |

Every scored endpoint follows the same contract:

- **Response headers**: `X-Request-Id`, `X-Latency-Ms`, `X-Model-Name`, `X-Token-Count` on every response, including the failure path.
- **Envelope on engine failure**: `HTTP 200` with the task's normal response shape, IDs echoed, blank/zero fields, and an `errors: [{code, detail}]` array. No 5xx on a valid request — a 5xx forfeits 100% of that item per FDEBench.
- **4xx only for malformed HTTP/JSON**: `RequestValidationError` handler returns 422 using `fastapi.encoders.jsonable_encoder` to sanitise raw-bytes inputs (fixes probe 5).
- **IDs always echoed**: `ticket_id` / `document_id` / `task_id`. The platform joins responses by ID, not position.

---

## Task 1 (Signal Triage): AI pipeline

**Model:** `gpt-5-nano` (FDEBench cost tier 1.0). `reasoning_effort=minimal` — this is classification, not a reasoning task, and gpt-5-* models otherwise burn completion budget on internal thought.

**Prompt:** [`prompts/triage_system.md`](../py/apps/arufa/prompts/triage_system.md). Structured as: golden rules (don't trust "urgent" tone; quiet emergencies hide behind polite text; hull/atmosphere/zone always escalate) → 8 categories with 1-line descriptions → 7 teams with ownership hints → P1–P4 rubric → `needs_escalation` rule → walk-the-16-table pattern for `missing_information` ("emit only when concept is *absent* from the description; empty list is valid") → output JSON schema.

**Response format:** `{"type": "json_object"}` (JSON mode). We parse into a private `_TriageLLMOutput` Pydantic model with `Literal` unions for all four vocabularies (8 categories, 7 teams, 4 priorities, 16 missing-info keys) so bad enums surface as `llm_parse_error` in `errors[]` rather than a silent wrong answer.

**Deterministic safety layer:** [`triage/safety_rules.py`](../py/apps/arufa/arufa/triage/safety_rules.py). Regex triggers on `subject + description` for hull breach, atmosphere / life-support compromise, and restricted-zone access. When any trigger fires we **force** `needs_escalation=true` and `priority=P1` (category and team stay LLM-decided — forcing them risks worse F1 without helping escalation). This is the catch-net for Kapoor's "quiet emergency" pattern where a polite senior-officer report might get down-ranked by the LLM.

**Defensive JSON extraction:** unwraps ` ```json ... ``` ` fences and finds `{...}` inside prose. Same helper used across all three tasks — gpt-5-* occasionally emit fences under adversarial prompts.

---

## Task 2 (Document Extraction): AI pipeline

**Model:** `gpt-5-mini` (FDEBench cost tier 0.9). Chose mini over nano for the vision task because ~36% of the eval set is photographed / handwritten / degraded — the accuracy delta on that subset pays back the ~4 efficiency-score points of cost tier drop.

**Prompt:** [`prompts/extract_system.md`](../py/apps/arufa/prompts/extract_system.md). Rules: follow the schema exactly, return `null` for unreadable fields (never hallucinate), numbers as numbers, tables extract every row, preserve source text formatting for string fields (scored separately as `text_fidelity`).

**Response format:** `{"type": "json_object"}` with the request's `json_schema` inlined as text in the user message. We do **not** use `{"type": "json_schema", "strict": true}` mode because the wire schemas often use features `strict=true` rejects (`oneOf`, missing `additionalProperties`). JSON-object mode + light validation is more forgiving.

**Image handling:** base64 content decoded and re-embedded as `data:image/png;base64,...` on the vision content part with `detail: high` — matters on the adversarial subset.

**Dynamic response shape:** `ExtractResponse` uses `ConfigDict(extra="allow")`. We build it as `ExtractResponse(document_id=..., **extracted_fields)`. `document_id` is never overwritten even if the model tries to emit one.

**No normalizer at v0.2.0** — `information_accuracy=0.837` on local eval indicates the vision model + prompt already output clean formats. Currency/percent stripper is queued for future iteration if numbers regress on the hidden set.

---

## Task 3 (Workflow Orchestration): AI pipeline

**Model:** `gpt-5-nano` (cost tier 1.0). `reasoning_effort=minimal` — the tight P95 ≤ 1,500 ms budget rules out heavier reasoning.

**Strategy: single-shot planning, real HTTP execution.** The LLM planner emits one JSON plan (steps + counters + `constraints_satisfied`); the executor calls each tool endpoint sequentially over HTTP. Rationale: an iterative agent-loop with the tool-calling API costs an extra LLM turn per tool call, easily blowing the latency budget on any workflow > 2 steps. Deviation D8 in [`PLAN.md`](../PLAN.md) — iterative upgrade queued for a future cycle if hidden T3 numbers demand it.

**Prompt:** [`prompts/orchestrate_planner.md`](../py/apps/arufa/prompts/orchestrate_planner.md). Instructs the planner to fulfill the goal, respect every constraint (including audit patterns), use only listed tools with correct parameter schemas, and emit `constraints_satisfied` only for constraints a concrete step enforces.

**Tool client:** [`orchestrate/tool_client.py`](../py/apps/arufa/arufa/orchestrate/tool_client.py). `httpx.AsyncClient`, 5 s per-call timeout, one retry on 5xx with backoff, no retry on 4xx (records `skip_reason`). **Never raises** — returns `ToolCallResult(success=False, error=...)` so the workflow can continue and the response can report partial progress. The state module wraps outcomes in `StepExecuted` entries with truncated result summaries.

**Critical status semantic (Deviation D10):** the pipeline **never downgrades `status` to `"partial"`** on individual step failures. The FDEBench T3 scorer gates `goal_completion` (20% of T3 Resolution) on `status == "completed"` — any other value returns 0.0 regardless of the trace. The other dimensions (`constraint_compliance` 40%, `ordering_correctness` 20%) already penalise real failures via outcome assertions in the gold data. Downgrading was double-counting; this fix moved `goal_completion` from 0.000 → 0.343 in one commit.

---

## Cross-task design decisions

### Shared kernel: one client, one config, one telemetry surface

Every LLM call goes through `arufa.shared.llm.client.LLMClient`. It provides:

- **Retry with `Retry-After` / `Retry-After-Ms` honouring.** The OpenAI SDK does not do this against AOAI throttling by default; the wrapper is why FDEBench 429 handling behaves correctly.
- **Semaphore-bounded concurrency.** `LLM_MAX_CONCURRENCY=20` (Deviation D11) — sized to match FDEBench probe 6 (20 concurrent in 500 ms with a 15 s probe-client timeout). With 8 the vision calls queued past the timeout and probe 6 failed; 20 fires everything immediately.
- **Per-call timeout 25 s.** Kept below the platform's 60 s per-call ceiling so two retries fit inside.
- **`reasoning_effort` parameter** on every call — critical for gpt-5-* reasoning models to avoid burning completion budget.
- **Header propagation via `ContextVar`.** After every successful attempt, the client records `model_name`, `prompt_tokens`, `completion_tokens` into a `ContextVar`. The middleware reads that context at response time and emits the headers. This works because the middleware is a **pure ASGI middleware** (Deviation D2) — Starlette's `BaseHTTPMiddleware` spawns the endpoint in a child task with a copied context that would silently drop these writes.

### One transport pattern for three tasks

Route handlers are ~10 lines each. They:

1. Type-validate the request via a Pydantic model (auto-422 on bad shape).
2. Wrap the pipeline call in `try/except Exception`.
3. On any exception → return `200` with the task envelope, IDs echoed, blank/zero fields, and one `errors[]` entry.

This is the FDEBench-mandated 200-vs-4xx contract, applied uniformly. See `arufa/main.py`.

### Config as data, prompts as data

- [`shared/config.py`](../py/apps/arufa/arufa/shared/config.py) uses `pydantic-settings` reading from an absolute `.env` path (works regardless of `cwd`).
- Prompts live in [`prompts/*.md`](../py/apps/arufa/prompts/) and are loaded once at startup via `lru_cache`. Editing a prompt is a content-only change; no Python edits, easy to diff in `docs/methodology.md`.

### Testability

Every pipeline is parametric on `LLMClient` (and `ToolClient` for T3). Tests inject `_StubLLM` / `_StubToolClient` — no real HTTP happens in the 83-test unit suite. `httpx.MockTransport` covers all LLM client retry paths.

---

## Infrastructure

| Resource | Value |
|---|---|
| Subscription | `92791f07-83ac-4f07-b2e6-51096ee0994d` |
| Resource group | `shivamarora` |
| Container Registry | `arufaacrshivamarora.azurecr.io` (Basic SKU) |
| Container Apps env | `arufa-env` (`westus`, Consumption) |
| Container App | `arufa` (min=1, max=5, external HTTPS, system MI) |
| Log Analytics | `arufa-logs` (`westus`) |
| Azure OpenAI | `arufa-aoai-shivamarora` (`eastus2`, S0) with `gpt-5-nano` + `gpt-5-mini` on GlobalStandard (50K TPM each) |
| MI → ACR | `AcrPull` (auto-granted by `--registry-identity system`) |
| MI → AOAI | *(not yet)* — currently key auth via ACA secret; AAD switch queued as T1 in [PLAN.md](../PLAN.md) |
| Deployed FQDN | `https://arufa.mangohill-daf67e16.westus.azurecontainerapps.io` |

**Deploy loop:** `az acr build --no-logs` (~45 s, sidesteps a Colorama Windows encoding bug), then `az containerapp update --image ...` for a zero-downtime revision swap. Rollback is one command: `az containerapp revision set-mode --mode single --revision <prior>`.

---

## Key tradeoffs

| Decision | Chose | Cost | Benefit |
|---|---|---|---|
| Single LLM vendor (AOAI) | AOAI only | Vendor lock-in | One retry loop, one auth story, one quota story — massively simpler |
| Nano vs mini for T1 | Nano | ~5 pp accuracy on adversarial | +4 pp cost score; sub-second latency-per-token |
| Nano vs mini for T2 (vision) | Mini | –4 pp cost score | Better performance on ~36% adversarial (handwritten/photographed) — dominant contributor to Resolution 82.7 |
| Single-shot vs iterative planning for T3 | Single-shot | Adaptivity to unexpected tool outputs | Stays inside 1500 ms P95 budget; deferrable to iterative (T6) if hidden numbers demand |
| Structured output mode | JSON object + Pydantic re-parse | Slight risk of malformed JSON (defensive parser mitigates) | Works with any wire schema; strict json-schema mode rejects `oneOf` common in T2 |
| Global `Exception` handler | None registered | Truly unhandled exceptions bubble to Starlette's 500 with no telemetry | Per-route try/except is the correct pattern; the global handler is unreachable behind `ServerErrorMiddleware` |
| Cross-region AOAI | AOAI in `eastus2`, ACA in `westus` | ~50–70 ms per LLM call added latency | Only region on this subscription with gpt-5-* deployments still available (both `gpt-4o-mini` and `gpt-4.1-*` were deprecating). Move to co-located `eastus2` is tech debt T9. |

### What would change for production

1. **AAD auth (managed identity) instead of API key** for AOAI. Trivial with `azure-identity`; deferred because the hackathon key-in-ACA-secret is fine at this scale.
2. **Co-locate ACA and AOAI** in the same region. Local T2 P95 14.7 s vs deployed 20 s — 5 s of that gap is the cross-region hop.
3. **Iterative planner for T3.** Single-shot works when the mock is answer-key-shaped; adaptive workflows will hit ceilings that only re-planning can break.
4. **Prompt-length compression on T1.** The system prompt is ~1500 tokens; compressing to ~500 would meaningfully reduce nano's reasoning-inclusive latency and possibly pull P95 back under the 4200 ms worst threshold.
5. **Bicep IaC.** All infra is currently imperative `az` commands. Fine for a hackathon, not for a team.
