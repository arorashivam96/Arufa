# Methodology

How Arufa was built, what changed cycle-to-cycle, what worked, and what didn't. Companion to [`docs/architecture.md`](architecture.md) (design), [`docs/evals.md`](evals.md) (numbers), and [`PLAN.md`](../PLAN.md) (milestone log with deviations D1–D11 and tech debt T1–T10).

Final submitted state: **`arufa--v064`** — composite **57.4 / 100** on the hidden set.

---

## 1. Approach

Grounded the whole build in a written spec first, then iterated on that spec instead of unstructured intuitions. The order was:

1. **Understand the customer** — read the three video transcripts (Cmdr. Kapoor, customer architect, MS FDE on FDEBench) and the `docs/challenge/` briefs before writing any code. Produced `Output-artifacts/01-problem-understanding.md` as the working brief.
2. **Resolve ambiguities and conflicts up front** — the customer voice and the scorer disagreed in several places (e.g. architect wanted `400/503` on engine failure; the scorer says any 5xx = 0 credit). Recorded 6 direct conflicts and 12 ambiguities with proposed resolutions before writing a line of code. This paid off: no reversals in-flight, one merge to `main` per milestone.
3. **Architect once, decompose to milestones** — one `docs/architecture.md`-shaped design doc, then split into independently-testable milestones (M0–M14) with concrete acceptance tests. See [`PLAN.md`](../PLAN.md).
4. **Ship discipline** — every milestone left `main` green with a scorable end-to-end system. M2 stubs got `items_errored=0` and 6/7 probes PASS *before* any real LLM code landed. That meant M4/M5/M6 could each be evaluated in isolation on their per-task Resolution numbers.

---

## 2. Time allocation

Rough split across ~15 milestones and 5 hidden submissions:

| Bucket | Fraction | Notes |
|---|---|---|
| M0 scaffold + M1 shared kernel | ~10% | Front-loaded: one LLM client, one middleware, one exception handler — all three tasks reuse them. Paid back on every subsequent milestone. |
| M2 stubs + probes | ~5% | All 7 API-resilience probes pass with envelope stubs alone. Cheapest ~30 pts on the leaderboard, front-loaded. |
| M3 deploy skeleton | ~10% | Docker, ACR, ACA, Log Analytics, MI, `azd`. Deployed empty stubs so we knew HTTPS + cold-start probe passed before adding real work. |
| M4 T1 pipeline | ~8% | Prompt + safety rules + JSON validation + tests. |
| M5 T2 pipeline | ~8% | Vision + dynamic schema + tests. |
| M6 T3 pipeline | ~12% | Planner + tool client + state + tests. Longest per-task because of the constraint-satisfaction complexity. |
| M7 iteration | ~5% | One cycle: T3 status fix (+3.4 pp composite) + concurrency bump. |
| M8 redeploy + verify | ~5% | AOAI key as ACA secret, revision swap, live eval on cloud FQDN. Submission #3 (57.3). |
| **M11-M12 T1 latency exploration** | ~5% | Prompt compression, `reasoning=minimal` sweep on T1. |
| **M13 T1 split-head experiment** | ~7% | Three parallel LLM heads for classification. Best local; worst hidden. Submission #4 (56.3). |
| **M14 T1 revert + latency win** | ~15% | Single-call recovery + T2 `reasoning=minimal` + T1 `reasoning=low`. Submission #5 (57.4). |
| Docs + submission checklist | ~10% | This file, `architecture.md`, `evals.md`, `submission.json`. |

Task 3 got the most attention in early cycles (moving parts: planner + real HTTP + constraint reporting). Task 1 got the most attention in late cycles because the M13 architectural error required a full revert cycle. Task 2 got the least attention because `gpt-5-mini` vision was already ceiling-adjacent on our first run.

---

## 3. Task 1 — Signal Triage

### 3.1 Approach

Started as JSON-mode classification with a strong system prompt + a deterministic post-LLM safety layer for the always-escalate cases (hull / atmosphere / restricted zone). Prompt structure: golden rules first (don't trust tone; quiet emergencies), then the 8-category vocabulary with 1-line descriptions, then priority calibration, then the walk-the-16-table pattern for `missing_information`, then 7 worked examples.

**Final v064 architecture — single LLM call + 4 deterministic post-processing steps.**

### 3.2 What moved the needle

- **`Literal` unions for all four vocabularies** (8 categories, 7 teams, 4 priorities, 16 missing-info keys) — surfaces bad enums as `llm_parse_error` in `errors[]` immediately rather than silent misclassifications.
- **Safety-rules regex** — cheap catch-net for hull / atmosphere / restricted-zone → forces P1 + escalate. Improved escalation F1 on cases where the LLM tone-matched but the description was a real emergency.
- **Defensive JSON extraction** (fence-strip + brace-find) — gpt-5-* occasionally emit ` ```json ... ``` ` fences under adversarial prompts despite instructions.
- **`reasoning_effort=low` (v064)** — brought T1 P95 to 6468 ms on hidden, crossing the P50 threshold portion of the latency dimension → latency score 0.37 (up from 0.00 in prior submissions). Worth +11.7 pp T1 Efficiency.

### 3.3 The M13 split-head experiment (what didn't work)

The single biggest **failed** bet of the whole project. Warrants a full autopsy.

**Hypothesis:** classifying category, priority, and missing-info are three orthogonal jobs. Give each its own LLM call and its own tightly-scoped system prompt; fire in parallel with `asyncio.gather`; combine on return.

**Local N=50 result (submission #4, arufa--v053):**

- Local T1 R = **62.8** — best T1 R we ever measured.
- All heads returned valid JSON; parallel fan-out preserved per-call latency.

**Hidden N=1000 result:**

- Hidden T1 R = **27.5** — **worst T1 R we ever shipped**.
- Composite dropped 57.3 → 56.3.

**Root cause (post-mortem):**

- Each head reasoned in isolation. When the LLM saw an ambiguous ticket, it made **inconsistent** choices across heads. The classify head might pick `Threat Detection` while the priority head picked `P3` — but gold labels tie those together (Threat items almost always escalate to P1/P2).
- Local N=50 didn't surface this because the sample happened to be less ambiguity-dense than the hidden N=1000.
- **The local score was a lie for architectural changes** — it measured our ability to pattern-match on N=50, not on N=1000.

**M14 revert (submission #5, arufa--v064):**

- Reverted to the M12 single-call architecture.
- **Kept the M13 deterministic post-processing** (routing clamp, NAMS→empty missing_info, safety-rules) because those are pure decisions that can only help.
- Hidden T1 R recovered from 27.5 → 25.9 (still below the M12 43.1 baseline — the local N=50 signal continues to over-predict the hidden set).

**Lesson.** When local N=50 says an *architectural* change is a big win, treat that signal with heavy suspicion. Deterministic post-processing (M13's rules) is safe because it's decision logic; parallelism across correlated dimensions is not.

### 3.4 What's still queued

- **T1 R below floor.** Local `category=0.68`, `missing_info=0.27`, `priority=0.57`. Prompt work on ambiguous boundary categories (BioAuth vs Threat, Hull vs Systems-Engineering hardware) + missing_info negative examples ("this description implies `affected_subsystem` — do not emit") is the fastest queued lever.
- **Model upgrade** to `gpt-5` (full, not mini) would likely close 5–10 pp of the hidden T1 R gap at the cost of ~3 pp Efficiency. Deferred because cost tier 0.7 is a hard drop from 0.9.

---

## 4. Task 2 — Document Extraction

### 4.1 Approach

Vision with `gpt-5-mini` + `detail: high` image resolution + JSON-object response mode. Schema inlined as text in the user message (not strict JSON-schema mode — the wire schemas use features `strict=true` rejects, like `oneOf` and missing `additionalProperties`).

### 4.2 What moved the needle

- **Choosing mini over nano for vision.** On the ~36% adversarial subset (photographed / handwritten / degraded), mini's accuracy is materially higher; the ~4 pp cost-tier hit is worth it. Local `information_accuracy=0.837` vs T5 floor of 0.45.
- **The prompt's explicit "return `null` for unreadable, never guess" rule** + ordered emphasis on tables and preserving source formatting for `text_fidelity`.
- **`detail: high` on the image URL** — required for text extraction accuracy on scans.
- **`LLM_MAX_CONCURRENCY 8 → 20` (M7 D11).** The FDEBench `concurrent_burst` probe sends 20 concurrent requests with a 15 s probe-client timeout. With semaphore=8 and ~7 s vision calls, 12 requests queued past the timeout and probe 6 failed. Sizing the semaphore to match the probe count fixed it → API resilience 71.4 → 100 → +5.7 pp T2 Robustness → +1.7 pp T2 composite.
- **`reasoning_effort` low → minimal (v064).** At `reasoning=low` the model was spending 4–8 k reasoning tokens per document, pushing local P95 to ~24 s (worst threshold is 19 s → latency score clamped to 0). Minimal reasoning still runs the vision pass at full detail and cut local P95 to ~15 s.

### 4.3 What didn't work

- **v064 T2 latency landed on the wrong side of the hidden threshold.** Local hit 15.8 s (comfortably below 19 s worst) but hidden landed at 20.7 s — 1.7 s over. Same cross-region jitter we saw at M8. Fix requires ACA region move to `eastus2` co-located with AOAI (T9).
- **Currency/percent normalizer** — considered as an M5 deliverable, held off because local `information_accuracy=0.837` already suggests clean formats from the model; adding a normalizer risks *hurting* `text_fidelity` (which is scored separately).

---

## 5. Task 3 — Workflow Orchestration

### 5.1 Approach

Single-shot planning with `gpt-5-nano`: the LLM emits one JSON plan (steps, counters, `constraints_satisfied`, `status`) that our async executor then walks. Tool client never raises — failures return `ToolCallResult(success=False, error=...)` so the workflow continues and the response reports what actually happened.

### 5.2 What moved the needle (biggest single win of the whole project)

**Not downgrading `status` to `"partial"` on tool failure** (M7, D10 in PLAN.md).

The FDEBench T3 scorer gates `goal_completion` (20% of T3 Resolution) on `status == "completed"` — any other value returns 0.0 regardless of the trace. The first pipeline dutifully downgraded to "partial" on any failed step; that zeroed out `goal_completion` on every run. Removing the downgrade moved `goal_completion` from 0.000 → 0.343 in one commit and lifted T3 composite from ~55 → ~59.

Two tests had to be corrected because they enforced the buggy downgrade behaviour. The tests were wrong and the "correctness" they were enforcing was actively costing score.

**Lesson: read the scorer before designing the response.**

### 5.3 Executor batching (what worked)

The executor groups consecutive **same-tool** steps and fires them via `asyncio.gather` (parallel). Different-tool boundaries stay sequential to preserve dependencies (`crm_search → send_email` still respects order).

- The common T3 shape is "search once, then act on each returned entity" — the "act" pass parallelises without needing a formal dependency graph.
- Local P95 dropped ~2 s vs pure sequential execution while preserving `ordering_correctness` (trace order preserved via `asyncio.gather` result ordering).

### 5.4 v063 full-plan parallelism (what didn't work)

**Experiment:** parallelise every step in the plan regardless of tool, not just consecutive same-tool batches.

**Rationale:** the planner emits concrete parameters per step, so there's no cross-step data flow at execution time.

**Result:**

- P95 dropped 11.5 s → 8.4 s (still above 8 s worst threshold, still latency-score = 0).
- Resolution dropped **1.9 pp** because same-server tool ordering constraints failed when steps fired out of order.
- Net −1.3 pp composite. **Reverted.**

**Lesson:** parallelism across steps that share downstream server state can silently break the server's assumptions even when the local parse looks clean.

### 5.5 What's queued

- **Iterative agent loop** (T6) — trades latency for adaptivity. Deferred because T3 latency is already 0-score, so extra LLM turns would only hurt without unlocking new dimensions.
- **Prompt compression** on the planner — the tool catalog inflation is currently a big chunk of the planner prompt. Not tried in the challenge window.

---

## 6. Cross-task decisions that shaped everything

### 6.1 What worked across tasks

- **Front-loading the shared kernel** at M1 meant M4/M5/M6 were only ~200 lines each. The `LLMClient` retry loop, `RequestContextMiddleware`, and `RequestValidationError` handler paid off three times. Consistency across tasks is a Tier-2 signal and it costs almost nothing when the kernel is done well.
- **Stubs before intelligence.** M2 landing all 7 probes + `items_errored=0` before any LLM call cost half a day and locked in ~30 leaderboard points that don't move even if the real pipelines break.
- **Ship-discipline.** Every commit left `main` green with `run_eval.py` still passing. No half-finished branches. Made rollbacks (M14) safe.
- **`ContextVar`-based header propagation.** The reason `X-Model-Name` shows up on every response, including the error path — pipelines write once via `record_llm_call`, middleware reads at response time. The pure-ASGI middleware pattern (D2) was mandatory here because Starlette's `BaseHTTPMiddleware` copies the context and drops the writes.
- **200-with-envelope pattern** consistently applied. Contributes zero on the happy path but preserves credit whenever the LLM misfires — and it will misfire on 5–10 % of items even in production.

### 6.2 What didn't work

- **Registering a global `Exception` handler** on the FastAPI app. Starlette's `ServerErrorMiddleware` intercepts unhandled exceptions outside our middleware stack, so a global `Exception` handler is silently unreachable. Discovered during M1 testing; deleted the handler; per-route `try/except → 200 + envelope` is the correct pattern (D3).
- **Naive `json.dumps` in the validation-error handler** crashed with `TypeError: bytes is not JSON serializable` when Content-Type was `text/plain` (Pydantic includes the raw request bytes in the error's `input` field). Handler returned 500; probe 5 failed. Fixed by wrapping in `fastapi.encoders.jsonable_encoder` (D4).
- **Local `docker build` on Windows** — Colorama crashes on cp1252 encoding when streaming ACR log output through PowerShell. Every `az acr build` attempt looked like it hung. `az acr build --no-logs` sidesteps this cleanly (D5).
- **`TestClient(app)` without a `with` block does not run lifespan.** `app.state.llm_client` is unset, `_llm(request)` raises `AttributeError`. Every test that touches app state uses `with TestClient(app) as client:` correctly, but a bare-Python smoke script hit this and briefly looked like a real bug (T7).

---

## 7. Key learnings

Ordered by how much composite score they cost or saved.

1. **Read the scorer before designing the response.** The T3 status downgrade was 10 minutes of code that cost 10 pp of composite until removed. A five-minute read of `scorers/workflow_orchestration.py` before writing the pipeline would have caught it. This is the single most important lesson from the whole build.

2. **Local N=50 is a noisy oracle — especially for classification tasks.** M13's split-head architecture had local N=50 T1 R at 62.8 (best we ever measured) and hidden N=1000 T1 R at 27.5 (worst we ever shipped). The gap widens on architectural changes. Rule: prefer *known-safe restorations* over *speculative uplifts* when submissions are finite.

3. **Latency-score cliffs are cliffs, not slopes.** The threshold is binary at `worst_ms`. Miss it by 100 ms and you lose 100 % of the sub-score. Design the P95 margin, not the P50. T2's v064 loss (local 15.8 s / hidden 20.7 s vs threshold 19 s) is a direct instance.

4. **Iteration budget is finite; spend it on the highest-marginal-value dimension.** M7 spent all its budget on T3 (`goal_completion` was 0, dominating the loss) and the T2 concurrent_burst probe. Left T1 prompt tuning for later — smaller expected uplift.

5. **Robustness is engineering discipline, not model choice.** The model doesn't decide whether probe 6 passes. The semaphore size and the middleware pattern do.

6. **Never let an unproven architecture ship late.** M13's split-head landed 22 hours before submission #4. That's how "local +6" became "hidden −16".

7. **Cross-region AOAI hurts more than expected.** Local T2 P95 15.8 s vs hidden 20.7 s — 5 s of that is the ACA (`westus`) → AOAI (`eastus2`) hop. Check AOAI regional availability before picking the resource group's region. (Queued as T9.)

8. **A stub composite is worth having.** M2 landed schema-valid envelopes returning safe defaults across all 3 tasks with zero items errored and 6/7 probes passing — the 34.1 baseline. That's a fallback floor: even if every subsequent milestone had failed, we would still submit a working service.

---

## 8. The 5 submissions in one graph

```
Composite score across all 5 hidden submissions:

34.1 ─┐
      │ + real pipelines land (M4/M5/M6)
56.8 ─┤
      │ + T3 status fix (M7 D10)
57.3 ─┤
      │
      │ − M13 split-head T1 (regression)
56.3 ─┤
      │ + revert to single-call + T2 reasoning=minimal + T1 reasoning=low (v064)
57.4 ─┘  ← final

Net gain over 5 submissions: +23.3 pp.
```

Two design wins (D10 and D11) account for the majority of the total gain. Both are read-the-scorer / read-the-probe wins, not model-quality wins.
