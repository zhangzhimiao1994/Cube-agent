# P3 Runtime Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the first P3 slice by making Agent Hub's runtime process events concrete enough for a Kimi/Codex-style conversation timeline.

**Architecture:** Keep this slice inside the runtime and conversation observability boundary. Add structured planning and stage events from the configured runtime wrappers, preserve child-runtime action events through hybrid execution, and let the existing UI render those details instead of inventing missing context.

**Tech Stack:** Python 3.12, pytest, Pydantic runtime contracts, FastAPI admin run detail API, React/Vitest conversation timeline.

---

### Guardrails

- [ ] Do not start Vibe Coding or OpenClaw implementation in this slice.
- [ ] Before Vibe Coding or OpenClaw implementation, perform a module-boundary refactor pass.
- [ ] Treat Vibe Coding and OpenClaw as system-level capabilities, not workflow presets.
- [ ] Model OpenClaw as a long-lived controlled session provider because the target is sustained computer operation over time.
- [ ] Expose OpenClaw actions through tool-call style capabilities, but keep the underlying lifecycle as an explicit session/runtime boundary with start, pause, resume, stop, audit, permission, timeout, and emergency-stop controls.
- [ ] Gate OpenClaw behind a system-level feature switch that is off by default and configurable by administrators.
- [ ] OpenClaw feature-switch settings must include allowed operation scope, session timeout, human-confirmation policy, audit level, and emergency-stop behavior.
- [ ] Do not let OpenClaw become main-Agent core logic; the main Agent may request or supervise sessions, while the session provider owns computer-control execution state.
- [ ] Model Vibe Coding as a conversation-integrated capability that depends on Attachments, Runtime, Model Gateway, Trace, Git/code-review capabilities, and permissions.
- [ ] Server-verify changes before pushing to GitHub.
- [ ] After any push, check GitHub Actions and fix failures until green or report a concrete external blocker.

### Task 1: Runtime Planning Events

**Files:**
- Modify: `tests/unit/runtime/test_configured_runtime.py`
- Modify: `src/agent_hub/runtime/defaults.py`

- [ ] **Step 1: Write failing tests**

Add tests proving dispatch and discussion emit a main-Agent planning event before child execution:

```python
assert planning_event.kind == EventKind.STEP_STARTED
assert planning_event.actor == "main_agent"
assert planning_event.step_id == "main_agent_plan"
assert planning_event.payload["mode"] == "dispatch"
assert planning_event.payload["roles"][0]["id"] == "copywriter"
assert planning_event.payload["roles"][0]["logical_model"] == "creative"
```

- [ ] **Step 2: Verify red**

Run:

```powershell
uv run pytest tests/unit/runtime/test_configured_runtime.py -q --tb=short
```

Expected: the new test fails because no `main_agent_plan` event is emitted.

- [ ] **Step 3: Implement minimal code**

Add a small runtime wrapper that yields one `step.started` event with `actor="main_agent"`, `step_id="main_agent_plan"`, `payload.mode`, `payload.roles`, and `payload.steps`, then streams the existing child runtime events with sequence numbers shifted forward.

- [ ] **Step 4: Verify green**

Run:

```powershell
uv run pytest tests/unit/runtime/test_configured_runtime.py -q --tb=short
```

Expected: tests pass.

### Task 2: Hybrid Stage Preservation

**Files:**
- Modify: `tests/unit/runtime/test_hybrid.py`
- Modify: `src/agent_hub/runtime/hybrid.py`

- [ ] **Step 1: Write failing test**

Add a test proving hybrid preserves child `step.started`, `model.started`, `message.created`, and stage `artifact.created` events with parent run IDs and monotonic parent sequences.

- [ ] **Step 2: Verify red**

Run:

```powershell
uv run pytest tests/unit/runtime/test_hybrid.py -q --tb=short
```

Expected: the new test fails because hybrid currently forwards only artifacts and messages from child runtimes.

- [ ] **Step 3: Implement minimal code**

Forward safe, user-observable child events from hybrid, excluding child terminal and checkpoint events. Normalize parent sequence and parent run ID.

- [ ] **Step 4: Verify green**

Run:

```powershell
uv run pytest tests/unit/runtime/test_hybrid.py -q --tb=short
```

Expected: tests pass.

### Task 3: UI Regression

**Files:**
- Modify: `web/src/pages/OperationalPages.test.tsx`
- Modify only if needed: `web/src/pages/RunsPage.tsx`

- [ ] **Step 1: Write failing or confirming UI test**

Add or extend a timeline test to assert the process list shows separate rows for main-Agent planning, child task receipt, model call, child output, discussion opinion, and final decision when events contain that structure.

- [ ] **Step 2: Run targeted frontend test**

Run:

```powershell
npm.cmd test -- src/pages/OperationalPages.test.tsx -- --runInBand
```

Expected: pass after backend event shape is represented in fixtures; modify UI only if it still collapses meaningful rows.

### Task 4: Verification And Handoff

**Files:**
- Modify: `HANDOFF.md`
- Modify: `REFACTOR_HANDOFF.md` if the Vibe/OpenClaw guardrail is not already explicit enough.

- [ ] **Step 1: Run backend checks**

Run:

```powershell
uv run ruff check src tests
uv run mypy src tests
uv run pytest tests/unit/runtime/test_configured_runtime.py tests/unit/runtime/test_hybrid.py -q --tb=short
```

- [ ] **Step 2: Run frontend checks**

Run:

```powershell
npm.cmd test -- src/pages/OperationalPages.test.tsx -- --runInBand
npm.cmd run build
```

- [ ] **Step 3: Update handoff**

Record changed files, verification, remaining P3 tasks, server sync status, and the Vibe/OpenClaw refactor-first system-level guardrail.
