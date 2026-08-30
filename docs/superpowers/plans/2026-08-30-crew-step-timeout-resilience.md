# Crew Step Timeout Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Crew dispatch resilient to large-task reviewer/final timeouts without hiding quality risk.

**Architecture:** Add a small timeout-policy helper in `runtime/defaults.py`, compact reviewer/final source artifacts in `runtime/crew/adapter.py`, classify Crew timeout diagnostics in `runtime/failure_reason.py`, and verify behavior through focused unit/integration tests.

**Tech Stack:** Python 3.12, Pydantic runtime contracts, pytest/pytest-asyncio, existing React diagnostics UI.

---

### Task 1: Crew timeout diagnostic classification

**Files:**
- Modify: `src/agent_hub/runtime/failure_reason.py`
- Test: `tests/unit/runtime/test_failure_reason.py`

- [ ] **Step 1: Write failing tests**

Add assertions that `runtime_failure_diagnostic_from_reason("CrewAI step timed out: step=quality_reviewer_step actor=quality_reviewer")` returns `error_code="crew.step_timeout"`, `error_stage="crew_step"`, `error_category="step_timeout"`, `retryable=True`, and safe `step_id`/`actor`.

- [ ] **Step 2: Run test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\runtime\test_failure_reason.py -q
```

Expected: the new Crew timeout diagnostic assertion fails because the classifier currently falls back to generic timeout.

- [ ] **Step 3: Implement classifier**

Add a regex for `CrewAI step timed out: step=<safe_id> actor=<safe_id>` and return a structured diagnostic with `crew.step_timeout`.

- [ ] **Step 4: Verify GREEN**

Run the same pytest command and expect all tests to pass.

### Task 2: Role-aware timeout policy

**Files:**
- Modify: `src/agent_hub/runtime/defaults.py`
- Test: `tests/unit/runtime/test_configured_runtime.py`

- [ ] **Step 1: Write failing tests**

Add tests proving:

```text
quality_reviewer_step.timeout_seconds > producer_step.timeout_seconds
final_response_step.timeout_seconds >= quality_reviewer_step.timeout_seconds * 0.75
timeouts remain <= 600 seconds
```

- [ ] **Step 2: Run test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\runtime\test_configured_runtime.py -k timeout -q
```

Expected: the quality reviewer timeout assertion fails with current uniform role timeout.

- [ ] **Step 3: Implement timeout helper**

Create `_role_step_timeout(context, selected_roles, role)` and `_final_step_timeout(context, selected_roles)`. Producers keep current behavior; post-product roles use a larger bounded value that increases with role count and request size.

- [ ] **Step 4: Verify GREEN**

Run the same pytest command and expect all matching tests to pass.

### Task 3: Compact review packet for reviewer/final prompts

**Files:**
- Modify: `src/agent_hub/runtime/crew/adapter.py`
- Test: `tests/integration/runtime/test_crew_adapter.py`

- [ ] **Step 1: Write failing tests**

Add a test where a final/reviewer step receives a large artifact and assert the Crew generation user payload contains `artifact_review_packet` with bounded previews instead of full `text`.

- [ ] **Step 2: Run test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\integration\runtime\test_crew_adapter.py -k review_packet -q
```

Expected: test fails because `_complete_agent()` currently uses `_artifact_prompt_payload()` or `_artifact_final_synthesis_payload()` directly.

- [ ] **Step 3: Implement compact packet routing**

Use compact artifact packets when `step.final_synthesizer` is true or when a step has dependencies and is not a producer step. Keep source ids and bounded preview; do not include secrets.

- [ ] **Step 4: Verify GREEN**

Run the focused test and expect it to pass.

### Task 4: Reviewer timeout soft-fail

**Files:**
- Modify: `src/agent_hub/runtime/crew/adapter.py`
- Test: `tests/integration/runtime/test_crew_adapter.py`

- [ ] **Step 1: Write failing tests**

Add a test where producer output exists but `_review()` times out. Assert dispatch emits `review.completed` with `review_status="timeout_skipped"` and final `runtime.completed`.

- [ ] **Step 2: Run test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\integration\runtime\test_crew_adapter.py -k reviewer_timeout -q
```

Expected: current behavior emits `review_status="skipped"` for `RuntimeExecutionError` or fails the run without timeout-specific metadata.

- [ ] **Step 3: Implement soft-fail**

When `_review()` raises timeout-classified `RuntimeExecutionError`, emit a timeout-specific skipped review event and continue. Preserve warning and diagnostics.

- [ ] **Step 4: Verify GREEN**

Run the focused test and expect it to pass.

### Task 5: Regression verification and handoff

**Files:**
- Modify: `REFACTOR_HANDOFF.md`

- [ ] **Step 1: Run targeted backend tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\runtime\test_failure_reason.py tests\unit\runtime\test_configured_runtime.py tests\unit\runtime\crew\test_adapter_failure_reason.py -q
```

- [ ] **Step 2: Run static checks**

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
```

- [ ] **Step 3: Run frontend checks if diagnostics UI changed**

```powershell
npm run lint
npm test -- --run src/pages/OperationalPages.test.tsx
```

- [ ] **Step 4: Update handoff**

Record changed behavior, verification, and any skipped integration tests caused by unavailable local Postgres.
