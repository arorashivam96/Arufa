# PLAN.md — Arufa Implementation Milestones

> Ordered, independently testable milestones from empty repo to FDEBench submission.
> Each milestone has a **concrete acceptance test** and maps to **specific scoring
> criteria** it moves. Companion to
> [`docs/architecture.md`](docs/architecture.md) (design) and
> [`CLAUDE.md`](CLAUDE.md) (non-negotiables).

## Status

| Milestone | Status | Notes |
|---|---|---|
| **M0** — Scaffold `apps/arufa` + `/health` | ✅ Done | pytest + local uvicorn green |
| **M1** — Shared kernel | ✅ Done | 25/25 tests pass; ContextVar propagation required pure-ASGI middleware (see deviations) |
| **M2** — Stub endpoints | ✅ Done | All 7 probes PASS, `items_errored=0` on all 3 tasks, composite 27.9 |
| **M3** — Deploy skeleton to ACA | ✅ Done | HTTPS FQDN `arufa.mangohill-daf67e16.westus.azurecontainerapps.io`; deployed eval green |
| **M4** — Task 1 real pipeline | ✅ Code + tests | LLM triage + safety_rules + JSON parsing hardening. 22 new tests pass. **Live eval pending** — Azure auth endpoint (`login.microsoftonline.com`) unreachable from this network; user needs to fetch key manually to run `run_eval.py --task triage`. |
| **M5** — Task 2 real pipeline | ✅ Code + tests | Vision pipeline (gpt-5-mini, `detail: high`), dynamic schema via JSON object mode, base64 → data URL. 10 new tests pass. **Live eval pending** same as M4. |
| **M6** — Task 3 real pipeline | ✅ Code + tests | Single-shot planner + async tool_client + sequential executor + degrade-to-partial on tool failure. 18 new tests pass. Iterative agent-loop upgrade tracked as T6. **Live eval pending** same as M4. |
| M7 — Iteration cycles | ⏳ Next | Blocked on live eval numbers |
| M8 — Redeploy full + load test | ⏳ | |
| M9 — Submission docs | ⏳ | |
| M10 — Submit | ⏳ | |

## How to read this

- **T-shirt effort** — S (< ½ day), M (½–1 day), L (1–2 days).
- **Depends on** — milestones that must ship first.
- **Acceptance test** — a runnable check; if it doesn't pass, the milestone isn't done.
- **Eval impact** — which FDEBench dimension the milestone unlocks or improves.
- **Ship discipline** — every milestone must leave `main` green: local `run_eval.py` still runs end-to-end with the previous milestone's score or better.

Rough total effort: ~10–14 days for a solo engineer. The order is optimised so
we always have a scorable end-to-end system on `main`.

---

## Milestone map

```
M0 ─► M1 ─► M2 ─┬─► M3 (deploy skeleton)
                │
                ├─► M4 (T1)
                │     │
                │     └─► M7-a (T1 iteration)
                │
                ├─► M5 (T2)
                │     │
                │     └─► M7-b (T2 iteration)
                │
                └─► M6 (T3)
                      │
                      └─► M7-c (T3 iteration)

                                        M8 (deploy full)
                                        │
                                        ▼
                                       M9 (submission docs) ─► M10 (submit)
```

---

## M0 — Scaffold `apps/arufa` + `/health` [S]

**Objective.** Give the workspace a home for our code that runs locally with `uvicorn` and passes the health probe.

**Deliverables**
- `py/apps/arufa/pyproject.toml` — registered as a `uv.workspace` member alongside `apps/sample` and `apps/eval`
- `py/apps/arufa/src/arufa/__init__.py`, `main.py`
- `main.py` exposes a `FastAPI` app with `GET /health → {"status":"ok"}`
- `.env.example` at `py/apps/arufa/`, `.gitignore` entry for `.env`
- Repo `Dockerfile` at Arufa root (multi-stage, non-root)

**Acceptance test**
```powershell
cd py; uv sync --all-packages
cd apps/arufa; uv run uvicorn arufa.main:app --port 8000
# In a second terminal:
curl http://localhost:8000/health  # -> {"status":"ok"}
```

**Eval impact**
- Tier 2 · Engineering Maturity · Deployment (Dockerfile exists)
- Tier 1 · Robustness · Probe 7 (cold-start baseline — trivially passes on a stub)

**Depends on.** —

---

## M1 — Shared kernel [M]

**Objective.** Every task pipeline uses the same LLM client, config, middleware, and exception handlers. Doing this once early is what makes T1/T2/T3 consistent (Tier 2 signal) and stops us re-implementing retry logic three times.

**Deliverables**
- `shared/config.py` — `pydantic-settings` `Settings` loading AOAI endpoint, deployments, timeouts, concurrency, retry, log level; supports `AOAI_AUTH_MODE=key|aad`
- `shared/llm/client.py` — async wrapper around AOAI:
  - `Retry-After` honouring (OpenAI SDK does NOT do this for AOAI throttling — see [`docs/eval/fdebench.md`](docs/eval/fdebench.md#platform-behaviour-to-know-about))
  - Semaphore-bounded concurrency (`LLM_MAX_CONCURRENCY`)
  - Per-call `timeout_s` (default 25 s < platform 60 s ceiling)
  - `reasoning_effort=minimal` toggle for gpt-5-* classifier calls
  - `LLMResult` returns parsed body + `model_name` + token counts
  - `LLMUnavailable` raised on exhausted retries
- `shared/observability.py` — structlog config, `add_model_name_header()` helper
- `shared/middleware.py` — request-id, latency timer, sets `X-Model-Name` / `X-Latency-Ms` / `X-Token-Count` from a per-request `ContextVar` the LLM client writes
- `shared/exception_handlers.py`:
  - `RequestValidationError` handler → `400/422` only for malformed HTTP/JSON
  - Base `Exception` catch on scored routes → `HTTP 200 + errors[]` envelope (per [`docs/challenge/README.md`](docs/challenge/README.md#http-semantics--when-to-return-200-vs-4xx))
- `shared/models/common.py` — `ErrorEntry`, base envelope

**Acceptance test**
```powershell
cd py/apps/arufa
uv run pytest tests/test_llm_client.py -v
# Cases:
#   test_success_first_try
#   test_success_after_429_with_retry_after
#   test_exponential_backoff_when_no_retry_after
#   test_raises_llm_unavailable_after_max_retries
#   test_semaphore_serialises_calls_over_cap
#   test_writes_model_name_to_contextvar
```

**Eval impact**
- Tier 1 · Efficiency · Cost tier (`X-Model-Name` header will be present on every response from M2 onwards)
- Tier 1 · Robustness · API resilience probes 6 (concurrency) and 7 (cold start) will be structurally sound
- Tier 2 · Code Quality · Structure (25% of CQ), Type Safety (20%), Error Handling (15%), Testing (25%)
- Tier 2 · AI Problem Solving · Model & Cost Awareness (25% of AIPS)

**Depends on.** M0.

---

## M2 — All three endpoints as schema-valid stubs [M]

**Objective.** All four endpoints exist, return envelopes that validate against the output schemas, and pass all seven API resilience probes. Score will be low but **nothing is errored**.

**Deliverables**
- `POST /triage` — returns default envelope: `category="Not a Mission Signal"`, `priority="P4"`, `assigned_team="None"`, `needs_escalation=false`, `missing_information=[]`, `next_best_action=""`, `remediation_steps=[]`
- `POST /extract` — returns `{document_id, ...json_schema-guided nulls}`; reads `content_format`, base64-decodes but doesn't call vision yet
- `POST /orchestrate` — returns `{task_id, status:"completed", steps_executed:[], constraints_satisfied:[]}`
- Pydantic types with the four T1 vocabularies (8 categories, 7 teams, 4 priorities, 16 missing-info) as `Literal` unions
- ASGI-layer body-size limit (100 KB) — probe 4 defence
- Content-type check in middleware — probe 5 defence

**Acceptance test**
```powershell
cd py; uv run python apps/eval/run_eval.py --endpoint http://localhost:8000
# All 3 tasks: items_scored > 0, items_errored == 0.
# All 7 probes: PASS.
```

**Eval impact**
- Tier 1 · Robustness · API resilience — **all 7 probes pass** (40% of Robustness, so 12 pp of task score, × 3 tasks). This is the single cheapest chunk of points in the whole benchmark.
- Tier 1 · Resolution — non-zero on trivially-classifiable items (e.g. `Not a Mission Signal` on obvious noise)

**Depends on.** M0, M1.

---

## M3 — Deploy skeleton to Azure Container Apps [M]

**Objective.** HTTPS FQDN reachable from public internet with `/health` + stub endpoints. Confirms deployment pipeline, MI RBAC to AOAI, and ACA settings that guard probes 6 & 7.

**Deliverables**
- `Dockerfile` — Python 3.12, uv-installed deps, non-root user, `EXPOSE 8000`, `CMD uvicorn`
- `azure.yaml` (or Bicep in `infra/`) provisioning:
  - Azure Container Registry (Basic)
  - Log Analytics workspace `arufa-logs`
  - Container Apps environment `arufa-env`
  - Container App `arufa` with `minReplicas=1`, `maxReplicas=5`, per-replica concurrency 30, external HTTPS ingress, system MI
  - Role assignment: ACA MI → `Cognitive Services OpenAI User` on `arufa-aoai-shivamarora`
- Env vars in ACA point at the AOAI endpoint + deployments (see [`CLAUDE.md` §7](CLAUDE.md#7-config--secrets) for the canonical list)

**Acceptance test**
```powershell
azd up
# Once complete:
$fqdn = az containerapp show -n arufa -g shivamarora --query properties.configuration.ingress.fqdn -o tsv
curl "https://$fqdn/health"  # -> 200 {"status":"ok"}
curl -X POST "https://$fqdn/triage" -H "Content-Type: application/json" `
     -d (Get-Content py/data/task1/sample.json -Raw)
# -> 200 with a valid stub envelope + X-Model-Name header
```

**Eval impact**
- Submission checklist requirement (deployed via HTTPS)
- Tier 1 · Robustness · Probe 7 (real cold-start test on ACA — `minReplicas=1` matters here)
- Tier 1 · Efficiency · Latency baseline captured from a real cloud instance
- Tier 2 · Engineering Maturity · Deployment (30% of EM), Config & Secrets (25% — MI auth, no keys in env)

**Depends on.** M2. Deploys the stub — full logic follows in M4–M6 (redeployed in M8).

---

## M4 — Task 1 real pipeline [L]

**Objective.** Move T1 Resolution from ~stub baseline to a real macro F1 on all 5 sub-metrics.

**Deliverables**
- `apps/arufa/src/arufa/triage/pipeline.py` — orchestrates LLM call, output parsing, safety rules
- `apps/arufa/src/arufa/triage/safety_rules.py` — deterministic post-LLM overrides:
  - hull breach / atmosphere compromise / restricted-zone access → `needs_escalation=true`, lower-bound `priority=P1`
- `prompts/triage_system.md` — system prompt using:
  - Explicit table of the 8 categories with 1-line descriptions
  - Priority rubric (P1–P4) with the "urgent-everything" and "quiet-emergency" cases called out
  - Walk-the-16-table pattern for `missing_information` with "empty list is a valid answer"
  - Structured-output JSON schema derived from `TriageOutput` Pydantic model
- `models/triage.py` — `TriageRequest`, `TriageOutput` with `Literal` enums (all 4 vocabularies)
- Unit tests: safety rules golden cases; prompt-response parsing; enum validation

**Acceptance test**
```powershell
cd py; uv run python apps/eval/run_eval.py --endpoint http://localhost:8000 --task triage
# Target: category_f1 >= 0.55, priority >= 0.55, routing_f1 >= 0.50, missing_info_f1 >= 0.35, escalation_f1 >= 0.60
# (These are floor targets, not stretch — pipeline is useful past this)
```

**Eval impact**
- Tier 1 · Resolution · T1 (100% of T1 R) — category (24%), priority (24%), routing (24%), missing_info (17%), escalation (11%)
- Tier 1 · Efficiency · Latency (mini/nano-tier text call, single-pass → sub-second P95 target)
- Tier 1 · Robustness · Adversarial (the "quiet emergency" and "urgent-everything" cases the prompt explicitly handles)

**Depends on.** M1 (LLM client), M2 (endpoint shape).

---

## M5 — Task 2 real pipeline [L]

**Objective.** Vision-based extraction driven by the request's `json_schema`. Move T2 Resolution off zero.

**Deliverables**
- `apps/arufa/src/arufa/extract/pipeline.py`:
  - Base64-decode `content` → PNG bytes
  - Build vision message with schema-injected system prompt
  - Call AOAI vision deployment with `response_format={"type":"json_schema", "schema": <request.json_schema>}` if supported by model, else JSON mode + Pydantic parse
- `prompts/extract_system.md` — vision extraction prompt:
  - Explicit "return `null` for unreadable, never guess" rule
  - Table extraction guidance (financial + medical form patterns)
- `apps/arufa/src/arufa/extract/normalizer.py` *(optional; enable if it moves score)* — currency / percent stripping post-processor
- Handling for `content_format="image_base64"` only (per platform contract)

**Acceptance test**
```powershell
cd py; uv run python apps/eval/run_eval.py --endpoint http://localhost:8000 --task extract
# Target: information_accuracy >= 0.45, text_fidelity >= 0.30
# (Vision on cheap models is inherently harder; these are floors)
```

**Eval impact**
- Tier 1 · Resolution · T2 (100% of T2 R) — info accuracy (70%), text fidelity (30%)
- Tier 1 · Robustness · Adversarial (the ~36% photographed/handwritten subset)

**Depends on.** M1, M2. Independent of M4.

---

## M6 — Task 3 real pipeline [L]

**Objective.** Multi-step orchestration that actually calls the tool HTTP endpoints and reports real execution.

**Deliverables**
- `apps/arufa/src/arufa/orchestrate/pipeline.py` — plan → execute → report loop
- `apps/arufa/src/arufa/orchestrate/tool_client.py` — async httpx client:
  - Per-call timeout, one retry on 5xx with backoff, no retry on 4xx (record `skip_reason`)
  - Never crashes the workflow on tool failure
- `apps/arufa/src/arufa/orchestrate/state.py` — immutable `StepResult` list, dependency graph, constraint evaluator producing `constraints_satisfied[]`
- `prompts/orchestrate_planner.md` — planner prompt using AOAI tool-calling:
  - Tool descriptions injected verbatim from `available_tools[]`
  - Constraint list injected verbatim
  - Explicit "small verifiable steps beat one opaque leap" instruction
- Parallel `asyncio.gather` on independent steps, bounded by semaphore

**Acceptance test**
```powershell
# Start the local mock service, then eval:
cd py/apps/eval; Start-Job { uv run python mock_tool_service.py }
uv run python run_eval.py --endpoint http://localhost:8000 --task orchestrate
# Local public T3 will trend near 100% because the mock is the answer key
# (see docs/challenge/task3/README.md#local-testing) — this milestone is
# about the *loop* actually executing, not the score.
# Real check: inspect run_eval output — steps_executed[] has real
# results, constraints_satisfied[] populated, no crashes on synthetic 5xx.
```

**Eval impact**
- Tier 1 · Resolution · T3 — constraint compliance (40%), goal completion (20%), ordering (20%), tool selection (15%), parameter accuracy (5%)
- Tier 1 · Efficiency · Latency (parallel tool calls where independent → keeps P95 under 1500 ms threshold)

**Depends on.** M1, M2. Independent of M4, M5.

---

## M7 — Iteration cycles (a: T1, b: T2, c: T3) [M each]

**Objective.** Push scores past floor targets via prompt tuning, model tier trade-offs, and normalization.

Do these in **any order**, and interleave if one task plateaus. Each iteration is one prompt/config tweak + one eval run. Log every tweak in [`docs/methodology.md`](docs/methodology.md).

**Deliverables (per iteration)**
- Documented hypothesis in `docs/methodology.md`
- Prompt / config change
- Before-and-after `run_eval.py` numbers copied into `docs/evals.md`
- Rollback if no uplift

**Acceptance test (per task, cumulative targets)**
- T1: category_f1 ≥ 0.72, priority ≥ 0.72, routing_f1 ≥ 0.65, missing_info_f1 ≥ 0.45, escalation_f1 ≥ 0.80
- T2: information_accuracy ≥ 0.60, text_fidelity ≥ 0.45
- T3: constraint_compliance ≥ 0.85 (with the caveat that public mock = answer key)

**Eval impact**
- Tier 1 · Resolution (all)
- Tier 1 · Robustness · Adversarial (the harder items respond to prompt work)
- Tier 2 · AI Problem Solving · Prompt Engineering (30%), Iteration Discipline (15%)

**Depends on.** M4 / M5 / M6 respectively.

---

## M8 — Redeploy full pipeline + load test [M]

**Objective.** Real cloud numbers. Latency P95, cold start, concurrent burst — all measured against the deployed FQDN.

**Deliverables**
- `azd deploy` with the full app
- Load-test script (`scripts/loadtest.ps1` or `hey` invocation) hitting 20 concurrent in 500 ms per endpoint
- Latency histogram captured for `docs/evals.md`
- Verify probe 6 (≥18/20 valid) and probe 7 (cold start after 5 s idle) against the FQDN

**Acceptance test**
```powershell
cd py; uv run python apps/eval/run_eval.py --endpoint https://$fqdn --task triage --task extract
# T1 + T2 scores within 5 pp of local; probes all pass; P95 latency captured
# (T3 must stay local per docs/eval/README.md — mock service is unreachable from cloud)
```

**Eval impact**
- Tier 1 · Efficiency · Latency (P95 from real cloud instance)
- Tier 1 · Robustness · Probes 6 & 7 on deployed endpoint
- Tier 2 · Engineering Maturity · Observability (structured logs flowing to Log Analytics), Scalability

**Depends on.** M3, plus whichever of M4–M7 are done.

---

## M9 — Populate submission docs [M]

**Objective.** All three mandated docs contain real numbers, real reasoning, and honest limitations. Missing/placeholder docs cost Tier 2 points.

**Deliverables**
- `docs/architecture.md` — full system design promoted from the working notes: components, per-task pipelines, deployment topology, requirement traceability
- `docs/methodology.md` — approach, time allocation, per-task iteration log (from M7), what worked / didn't
- `docs/evals.md` — actual `run_eval.py` numbers per task and per dimension, error analysis, known limitations

**Acceptance test**
```powershell
Select-String -Path docs/architecture.md,docs/methodology.md,docs/evals.md `
  -Pattern "<!--|TODO|TBD" -SimpleMatch
# Zero matches → docs are substantive.
```

**Eval impact**
- Submission checklist (all three docs mandatory)
- Tier 2 · every dimension is judged partly by the docs

**Depends on.** M7, M8 (need real numbers).

---

## M10 — Submit [S]

**Objective.** Push, verify judge access, submit at [aka.ms/delta/fdebench/hackathon](https://aka.ms/delta/fdebench/hackathon).

**Deliverables**
- Final `git push origin main`
- Deployed FQDN available and stable
- Submission form filled with fork URL + FQDN

**Acceptance test**
- Submission confirmed in the platform UI, leaderboard entry created

**Depends on.** M8, M9.

---

## Requirements → milestone traceability

Every consolidated requirement from the task briefs and
[`docs/eval/fdebench.md`](docs/eval/fdebench.md) lands in a milestone. `R1`–`R20`
are the numbered rows in our internal requirements matrix (they map 1:1 to the
numbered principles and constraints in [`docs/challenge/`](docs/challenge/) and
[`CLAUDE.md`](CLAUDE.md)):

| Req | Milestone |
|---|---|
| R1 (4 endpoints) | M0 (/health), M2 (rest) |
| R2 (T1 enums) | M2 (Literal types), M4 (values) |
| R3 (escalation override) | M4 (safety_rules) |
| R4 (missing-info F1) | M4 (prompt), M7-a (tuning) |
| R5 (dynamic T2 schema) | M5 |
| R6 (no hallucination on T2) | M5 (prompt) |
| R7 (real T3 HTTP) | M6 (tool_client) |
| R8 (T3 constraint compliance 40%) | M6 (state), M7-c (tuning) |
| R9 (200 + errors[] envelope) | M1 (exception_handlers) |
| R10 (Retry-After honouring) | M1 (llm.client) |
| R11 (25 s per-call timeout) | M1 (llm.client) |
| R12 (7 resilience probes) | M1 + M2 (validation, size limit, content-type), M3/M8 (deployed cold start + concurrency) |
| R13 (X-Model-Name header) | M1 (middleware + llm.client) |
| R14 (sub-second, 20–30 concurrent) | M1 (semaphore), M3 (ACA concurrency=30), M7 (model tier tuning) |
| R15 (Pydantic, prompt files, mocked-LLM tests, no secrets) | M0, M1, M4/M5/M6 |
| R16 (join on request_id_key) | M2 (pipelines echo IDs) |
| R17 (arch/methodology/evals docs) | M9 |
| R18 (Dockerfile + .env.example + HTTPS) | M0, M3 |
| R19 (consistency across 3 tasks) | M1 (shared kernel) |
| R20 (intentional model selection) | M1 (config), M7 (tuning), M9 (methodology doc) |

---

## Guardrails during execution

- **Ship discipline** — every milestone commits leave `main` green. If a milestone can't finish this session, land the stub in `main` and open a branch for the rest.
- **No pre-work.** Do not scaffold for features not in the next milestone. If we don't need a database at M2, don't add one at M0.
- **Score before code.** Run `run_eval.py` before each iteration so we have a baseline for the change.
- **Log every model change** in `docs/methodology.md`. Judges look for iteration discipline; if we can't retell the story we lose the AI Problem Solving marks.
- **Never bypass `shared/llm/client.py`.** No task pipeline calls httpx directly. This is the only way retry + `Retry-After` + `X-Model-Name` propagation stays consistent.

---

## Deviations from `docs/architecture.md` (log as they happen)

| # | Deviation | Why | Impact |
|---|---|---|---|
| D1 | Package layout is **flat** (`apps/arufa/arufa/`), not `src/arufa/` as the arch doc suggested | Matches the pattern already established by `apps/sample/` in the upstream repo; keeps hatchling config a single line. Introducing a new layout just for our app would violate CLAUDE.md's "don't introduce a new pattern without saying why." | None. Import paths (`arufa.main:app`) are identical either way. |
| D2 | `RequestContextMiddleware` is a **pure ASGI middleware**, not `BaseHTTPMiddleware` as the arch doc's diagram showed | `BaseHTTPMiddleware` spawns the endpoint in a child task with a **copied** context, silently dropping `ContextVar` writes made by the LLM client. Documented Starlette limitation. Pure ASGI runs in the same async context. | None on behaviour; simpler and correct. Regression test: `test_with_llm_call_headers` in `tests/test_middleware.py`. |
| D3 | **No global `Exception` handler** registered on the app | Starlette's `ServerErrorMiddleware` intercepts unhandled exceptions *outside* our middleware stack, so a global `Exception` handler is silently unreachable. Correct pattern is per-route `try/except → 200 + envelope` (already implemented in `main.py`). | None on the scored path. Any bare `raise` inside a pipeline that escapes the route-level `try` would produce a bare 500 with no headers — pipelines must catch. Called out in `docs/architecture.md §6.2` update. |
| D4 | Validation error handler uses `fastapi.encoders.jsonable_encoder` on `exc.errors()` before serialising | Pydantic's `.errors()` includes raw `bytes` in the `input` field when Content-Type isn't JSON. Naïve `json.dumps` crashes and produces a 500 that fails probe 5. `jsonable_encoder` coerces bytes → str. | Fixed probe 5. Regression test: `test_wrong_content_type_does_not_crash` in `tests/test_exception_handlers.py`. |
| D5 | Docker build uses `az acr build` (cloud-side), not local `docker build` | Colorama on Windows crashes when streaming the log output back through PowerShell (`UnicodeEncodeError: cp1252`). Cloud build works fine; queue with `--no-wait --no-logs` and poll. | ~45 s per build. Not blocking; Docker Desktop still available for local iteration if needed. |
| D6 | Container App deployed with `AOAI_AUTH_MODE=key` and **no** `AOAI_API_KEY` env var | M3 stubs don't call AOAI, so no key is needed. Key/AAD wiring lands with M4 (T1 pipeline). | Any pipeline that tries to call AOAI on the deployed app right now will get `LLMUnavailable("AOAI_API_KEY not set")` — surfaces as `200 + errors[]` per the envelope contract, not a crash. |
| D7 | ACA-side registry pull uses **system-assigned MI + AcrPull** grant on the ACR, not a service principal | Matches the CLAUDE.md principle "no keys in source". `az containerapp create --system-assigned --registry-identity system` auto-grants `AcrPull`. | None. |
| D8 | **T3 uses single-shot planning**, not the iterative agent-loop mentioned in the arch doc | Single-shot keeps us inside the P95 ≤ 1,500 ms budget on nano-tier. Iterative loops need N × (LLM + tool) round-trips, which blows the budget on any workflow > 2 steps. | Local T3 score won't reflect this trade-off (mock = answer key). If hidden T3 lands low, upgrade to iterative agent-loop at M7 — tracked as tech debt T6. |
| D9 | **LLM output parsing is defensive** (unwraps `` ```json fences and prose wrap `` around the JSON) even though the prompts explicitly forbid these | gpt-5-nano/mini occasionally emit fences under adversarial prompts. Cheap to handle, expensive to lose an item for. | None; belt-and-braces. Same helper used in T1, T2, T3 pipelines. |

## Open tech debt (address before submission)

- **T1**: AAD auth (`azure-identity` + `DefaultAzureCredential`) in `shared/llm/client.py` — needed for the deployed container to talk to AOAI without a key. Blocked on M4 (first task that actually calls the model). Deployed image still needs key or AAD switch — M4 deploy pending.
- **T2**: No infra as code yet. All Azure resources created imperatively via `az`. Migrate to Bicep or `azure.yaml` before M8 so redeploys are reproducible. Track in M3 tail-work.
- **T3**: Test coverage on `arufa.main` (the route try/except → 200 + envelope path) is limited to happy-path stub tests. Add an explicit failure-injection test at M4 when we have a real pipeline that can be forced to raise. **Partially addressed:** `test_triage_pipeline`, `test_extract_pipeline`, `test_orchestrate_pipeline` each cover the `LLMUnavailable → 200 + errors[]` path.
- **T4**: Structured logs currently emit at INFO on every request (`request_complete`). Fine at hackathon scale; noisy at 500-item eval scale. Consider sampling or moving to DEBUG once M4+ logs are added.
- **T5**: The `structlog.processors.dict_tracebacks` processor is included but never exercised in M1 tests. Verify at M4 when the first real pipeline error is possible. **Addressed:** the pipeline-failure test paths exercise it; observed working via the pytest run (traceback rendering was visible in the bare-Python smoke test we ran during M4).
- **T6** (new at M6): Orchestrate pipeline is single-shot planning. Iterative agent-loop with the OpenAI tool-calling API would handle adaptive workflows better on the hidden eval; single-shot trades adaptability for the tight P95 ≤ 1,500 ms budget. Upgrade at M7 if hidden T3 numbers demand it.
- **T7** (new at M4): The bare `TestClient(app)` invocation (without `with` context manager) does not run the FastAPI lifespan, so `app.state.llm_client` is not set. Tests currently use `with TestClient(app) as client:` correctly. Document this in `CLAUDE.md §6` alongside the other FastAPI gotchas.
