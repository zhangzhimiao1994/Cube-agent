# Dispatch Instruction Context Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans. Work in the existing feature branch and preserve unrelated files.

**Goal:** Extend run-scoped guidance evidence to actual dispatch and reviewer requests.

**Architecture:** Preserve existing Crew bridges, request ledger and event sequence.
Pass private immutable context to each bridge and inject bounded guidance at the
request boundary; extend safe evidence metadata without changing orchestration.

**Tech Stack:** Python 3.12, CrewAI adapters, Pydantic, asyncio, pytest and Postgres.

**Spec:** `docs/superpowers/specs/2026-09-21-dispatch-instruction-context-design.md`

## Global Constraints

- No changes to harness/tool authorization, role selection, models or concurrency.
- No attachment rules, approved skills, discuss/hybrid or planning gates in this slice.
- Preserve the existing ledger digest format and replay guarantees.
- Content is private; only typed metadata reaches public events.
- Deployed direct behavior and legacy event projection remain compatible.

## Review Focus

- Every real tool continuation and reviewer request includes guidance once.
- Request budgets and cancellation cannot create false injected evidence.
- Recovery cannot replay against changed guidance or a random load identity.
- Concurrent tenants must not share source content or mutable configuration.
- Public stage/actor correlation must be runtime-owned and strictly validated.

## Task 1: Dispatch Bridge Integration

Files: `runs/service.py`, `runtime/crew/adapter.py`,
`runtime/instruction_context.py`, `runtime/direct.py`, and affected unit/contracts tests.

- [x] Add red gateway-capture tests for execution role, tool continuation and reviewer.
- [x] Extend service loading to dispatch and replace Crew's public clone with the
  validated internal clone. Reuse the load-time control and authorization checks.
- [x] Add guidance once to final normalized messages before request construction;
  include the block in current budget validation and ledger digest computation.
- [x] Emit request-bound metadata after gateway submission via existing emitter,
  preserving prepared/running checkpoint and model.started ordering.
- [x] Test replay, changed content, fixture, budget, cancellation, timeout and
  concurrent scopes. Run affected Crew/direct/service suites and static checks.

Invariant asserted against each actual captured request:

```python
assert request_text.count("<PROJECT_GUIDANCE_JSON>") == 1
assert evidence["load_id"] == str(bundle.load_id)
assert evidence["ledger_request_sha256"] == persisted_entry.request_sha256
assert marker not in json.dumps(evidence)
```

## Task 2: Safe Identity Projection

Files: `runs/context_evidence.py`, `tests/unit/runs/test_context_evidence.py`.

- [x] Add failing projections for known dispatch stages and unknown/malformed stages.
- [x] Preserve legacy direct fields and add validated runtime actor, stage, step,
  bounded call/attempt numbers and ledger identifiers matching the adapter format.
- [x] Reject unknown keys, raw prompt text and fabricated stage fallbacks.
- [x] Run public/admin projection regression and all direct-context regressions.

## Task 3: Verification And Release

- [x] Independent review with immediate fixes for blocking findings.
- [x] Full unit/API/contracts, real Crew/Postgres candidate probe, Ruff and strict mypy.
- [ ] Feature-specific server probe and authenticated acceptance after deployment.
- [ ] Push only after production checks, inspect CI until green, clean obsolete
  release/test files and update the local current-state handoff.

Pre-release verification: 3021 unit/API/contract tests passed, 34 skipped;
Ruff and strict mypy (365 files) passed. Isolated server Postgres with actual
RunService/Crew worker and reviewer passed. Full local integration remains
unavailable without Postgres and must be checked in CI. Independent review's
submission cancellation race and repeated-projection identity regression were
reproduced, fixed and rechecked. These results do not imply project capability
acceptance or pressure testing.
